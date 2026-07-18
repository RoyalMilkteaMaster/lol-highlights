"""APScheduler 常駐進程：自動觸發 streamlink 錄影 + 啟動恢復 + cleanup
。

設計：
- BlockingScheduler(timezone=UTC)：避免 8 小時時區坑
- coalesce + max_instances + misfire_grace_time
- job_id 固定 + replace_existing=True
- 啟動時掃 -3hr ~ +30min 補錄狀態恢復
- stale lock 多重判斷
- NTP warn-only
- cleanup 用 Asia/Taipei timezone
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler

from automation.infra import ntp_check
from automation.infra.cleanup import cleanup_old_files
from automation.infra.config import load_config as _load_config
from automation.db.connection import mysql_conn
from automation.db.repositories import (
    BroadcastStateRepo,
    RecordingLockRepo,
)
from automation.infra.heartbeat import HeartbeatThread
from automation.infra.control import automation_status
from automation.infra.log_setup import setup_rotating_log
from automation.infra.time_utils import from_db_utc, utc_now

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_LOG_DIR = _PROJECT_ROOT / "_tmp" / "logs"

logger = setup_rotating_log("scheduler", _LOG_DIR / "scheduler.log")
_LAST_AUTOMATION_ALLOWED: bool | None = None


# ─────────────────────────────────────────────────────────────────────────────
# 一鍵暫停 / 恢復系統。雙擊「暫停.bat」建 logs/system_paused.lock，
# 各 cron / worker 看到此 lock 就 skip。「恢復.bat」刪 lock。
# 不影響直播錄影 streamlink（直播不可逆）；既有 main.py / yt-dlp child 等跑完。
def _is_system_paused() -> bool:
    return not automation_status()["allowed"]


def _periodic_policy_refresh(scheduler) -> None:
    """Log policy transitions and recover date jobs when a work window opens."""
    global _LAST_AUTOMATION_ALLOWED

    decision = automation_status()
    allowed = bool(decision["allowed"])
    if allowed == _LAST_AUTOMATION_ALLOWED:
        return

    previous = _LAST_AUTOMATION_ALLOWED
    _LAST_AUTOMATION_ALLOWED = allowed
    logger.info(
        "automation policy: allowed=%s source=%s reason=%s next_change=%s",
        allowed,
        decision.get("source"),
        decision.get("reason"),
        decision.get("next_change_at") or "none",
    )
    if previous is False and allowed:
        _periodic_schedule_scrape(scheduler)
        _daily_find_live(scheduler)


def _pid_alive(pid: int | None) -> bool:
    """Windows / Linux 都能查 pid 是否還活著（要 psutil）。沒有 psutil 時保守視為活著。"""
    if pid is None or pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        # 沒裝 psutil → 保守視為活著（不誤殺）
        return True
    except Exception as e:
        # 原本只 return True，吞例外不留 trace。加 warning 方便回查。
        logger.warning("_pid_alive(%s) 檢查例外，保守視為活著：%r", pid, e)
        return True


def release_stale_locks_safely() -> int:
    """多重判斷後才清 stale lock（避免誤殺正常 waiting_stream/recording）。

    清 lock 條件（必須**全部**符合）：
      條件 1: status 已是終態（recorded/failed/None）
      或：status 是 active 但 last_heartbeat_at 超過 5 分鐘 + pid 不存在
    """
    cleaned = 0
    with mysql_conn() as conn:
        repo = BroadcastStateRepo(conn)
        lock_repo = RecordingLockRepo(conn)
        for lock in lock_repo.find_all():
            broadcast = repo.get_by_id(lock["broadcast_id"])
            if not broadcast:
                lock_repo.force_release(lock["broadcast_id"])
                cleaned += 1
                continue

            status = broadcast.get("recording_status_v2")
            # 條件 1：已是終態（含 None）→ lock 可清
            if status in (None, "recorded", "failed"):
                lock_repo.force_release(lock["broadcast_id"])
                cleaned += 1
                continue

            # 條件 2：active 狀態 → 看 heartbeat 是否真的卡住
            last_hb = from_db_utc(broadcast.get("last_heartbeat_at"))
            if last_hb is None:
                # 沒 heartbeat 紀錄 → 不敢動
                continue
            if utc_now() - last_hb < timedelta(minutes=5):
                # heartbeat 還新 → 是活的，不動
                continue
            # heartbeat 卡住 → 看 pid 是否還在
            if not _pid_alive(lock.get("pid")):
                lock_repo.force_release(lock["broadcast_id"])
                repo.update_status(
                    lock["broadcast_id"], "failed",
                    error_message="stale_recovery: heartbeat timeout + pid not alive",
                )
                cleaned += 1
    return cleaned


# ─────────────────────────────────────────────────────────────────────────────
def _spawn_recorder_subprocess(broadcast_id: int) -> None:
    """APScheduler 觸發時用 subprocess 啟 recorder。"""
    if _is_system_paused():
        logger.debug("[_spawn_recorder_subprocess] automation policy blocked; skip bid=%s", broadcast_id)
        return
    python = sys.executable
    cmd = [python, "-m", "automation.run", "--record", str(broadcast_id)]
    env = {**os.environ, "KMP_DUPLICATE_LIB_OK": "TRUE", "PYTHONIOENCODING": "utf-8"}
    logger.info("觸發 recorder：broadcast_id=%s", broadcast_id)
    # 不等待（fire-and-forget），避免 scheduler thread 卡住
    # 移除 CREATE_NEW_CONSOLE。pythonw 模式下 log_setup 的 patch 會自動加
    # CREATE_NO_WINDOW；console 模式則 recorder 共用 scheduler console（也不跳視窗）。
    subprocess.Popen(cmd, env=env)


def _schedule_recording(scheduler, broadcast: dict) -> None:
    """固定 job_id + replace_existing=True，避免重複 schedule。

    改動：
    - 提前時間從 60s → config 設定 lead_minutes（預設 3 分鐘）
    - GPT review 2：misfire_grace_time 顯式設 1 hour（避免 scheduler 晚醒判定過期不跑）
    """
    cfg = _load_config().get("scheduler", {})
    lead_min = float(cfg.get("schedule_recording_lead_minutes", 3))
    grace = int(cfg.get("schedule_recording_misfire_grace_sec", 3600))

    bid = broadcast["broadcast_id"]
    sched_start = from_db_utc(broadcast.get("scheduled_start_utc"))
    if not sched_start:
        return

    run_date = sched_start - timedelta(minutes=lead_min)
    if run_date < utc_now():
        run_date = utc_now() + timedelta(seconds=5)

    job_id = f"record_broadcast_{bid}"
    scheduler.add_job(
        _spawn_recorder_subprocess,
        "date",
        run_date=run_date,
        id=job_id,
        replace_existing=True,
        max_instances=1,
        args=[bid],
        misfire_grace_time=grace,
        coalesce=True,
    )
    logger.info(
        "schedule recorder job_id=%s 在 %s（broadcast %s, lead=%.1fmin, grace=%ds）",
        job_id, run_date.isoformat(), bid, lead_min, grace,
    )


def _schedule_all_upcoming_recordings(scheduler) -> int:
    """find_live 結束 / 啟動恢復時呼叫，把 upcoming + 剛剛過去但還沒錄的 broadcasts 排上錄影 job。

    GPT review 2：hours_back=6（不是 0）— scheduler 中途關掉重開不會漏排剛開播的 match。
    system_paused.lock 存在則 skip（不再新排 recorder job 進 APScheduler）。
    """
    if _is_system_paused():
        logger.debug("[_schedule_all_upcoming_recordings] automation policy blocked; skip")
        return 0
    cfg = _load_config().get("scheduler", {})
    hours_back = int(cfg.get("schedule_recording_hours_back", 6))
    lookahead_h = float(cfg.get("schedule_recording_lookahead_hours", 48))
    with mysql_conn() as conn:
        rows = BroadcastStateRepo(conn).find_recent_and_upcoming(
            hours_back=hours_back,
            minutes_ahead=int(lookahead_h * 60),
        )
    n = 0
    n_skip_vod = 0
    for r in rows:
        status = r.get("recording_status_v2")
        if status in ("recorded", "failed", "waiting_stream", "recording", "merging"):
            continue
        # bilibili_vod 走 lpl_downloader 路徑（match_time + 80min cron 觸發），
        # 不該排 streamlink_recorder job。之前忘了過濾 → broadcast 79/80 16:57
        # 觸發了 recorder Popen，子進程 silent exit 浪費資源 + 弄髒 schedule。
        if r.get("platform") == "bilibili_vod":
            n_skip_vod += 1
            continue
        _schedule_recording(scheduler, r)
        n += 1
    logger.info(
        "已 schedule %d 個 recorder job (hours_back=%d, lookahead=%.0fh, skip_bilibili_vod=%d)",
        n, hours_back, lookahead_h, n_skip_vod,
    )
    return n


# ─────────────────────────────────────────────────────────────────────────────
def _on_startup(scheduler) -> None:
    """啟動恢復 + stale lock 清理。

    用 `_schedule_all_upcoming_recordings`（GPT review 2 採納 hours_back=6）。
    """
    logger.info("=== Scheduler 啟動 ===")
    ntp_check.check_time_drift(threshold_sec=30.0)
    n = release_stale_locks_safely()
    if n:
        logger.warning("清掉 %d 個 stale lock", n)
    _schedule_all_upcoming_recordings(scheduler)


# ─────────────────────────────────────────────────────────────────────────────
# /3c：定期爬 lolesports 抓最新賽程 + 找直播 URL
# ─────────────────────────────────────────────────────────────────────────────
def _periodic_schedule_scrape(scheduler=None) -> None:
    """每天 09:00 Asia/Taipei 跑 pipeline.run（lolesports JSON API）。

    跑完後 inline:
    - _ensure_lpl_broadcasts_exist：對 LPL series 建 bilibili_vod broadcast row
    - _schedule_lpl_downloads（如果 scheduler 有給）：排 LPL download job
    """
    if _is_system_paused():
        logger.debug("[periodic_schedule_scrape] automation policy blocked; skip")
        return
    cfg = _load_config().get("scheduler", {})
    leagues_str = cfg.get("schedule_scrape_leagues", "LCK,LCP,LPL,LEC,LCS")
    days_ahead = int(cfg.get("schedule_scrape_days_ahead", 14))
    days_back = int(cfg.get("schedule_scrape_days_back", 1))
    leagues = [c.strip().upper() for c in leagues_str.split(",") if c.strip()]
    logger.info(
        "[periodic_schedule_scrape] 跑 pipeline.run leagues=%s days_ahead=%s days_back=%s",
        leagues, days_ahead, days_back,
    )
    try:
        from automation.pipeline import ScraperPipeline
        ScraperPipeline().run(
            league_codes=leagues,
            days_ahead=days_ahead,
            days_back=days_back,
        )
    except Exception:
        logger.exception("[periodic_schedule_scrape] 失敗（不影響其他 job）")

    # LPL hooks
    try:
        _ensure_lpl_broadcasts_exist()
        if scheduler is not None:
            _schedule_lpl_downloads(scheduler)
    except Exception:
        logger.exception("[periodic_schedule_scrape] LPL hook 失敗")


def _run_find_live(leagues: list[str] | None = None) -> None:
    """執行 pipeline.find_live。回傳 None；錯誤吞掉並 log。

    `leagues` 給 None 表示用 config 裡 find_live_leagues 全跑（daily 用）。
    給 list 表示只跑那些（per-match retry 用，省 API quota）。
    """
    if _is_system_paused():
        logger.debug("[find_live] automation policy blocked; skip")
        return
    cfg = _load_config().get("scheduler", {})
    if leagues is None:
        leagues_str = cfg.get("find_live_leagues", "LCK,LCP,LPL")
        leagues = [c.strip().upper() for c in leagues_str.split(",") if c.strip()]
    days_ahead = int(cfg.get("schedule_scrape_days_ahead", 14))
    logger.info("[find_live] 跑 pipeline.find_live leagues=%s", leagues)
    try:
        from automation.pipeline import ScraperPipeline
        ScraperPipeline().find_live(leagues=leagues, days_ahead=days_ahead)
    except Exception:
        logger.exception("[find_live] 失敗（不影響其他 job）")


def _daily_find_live(scheduler) -> None:
    """每天 09:30 Asia/Taipei 跑一次 — 全 leagues 大規模 find_live。

    跑完後：
    1. _schedule_all_upcoming_recordings 把找到的 broadcast 排錄影 job
    2. _schedule_per_match_url_retries 為「未來 24h 內仍無 URL」的 series 排重試 job (-3h, -30min)
    3. _schedule_lpl_downloads 也順便排 LPL download job
    """
    _run_find_live()
    _schedule_all_upcoming_recordings(scheduler)
    _schedule_per_match_url_retries(scheduler)
    try:
        _schedule_lpl_downloads(scheduler)
    except Exception:
        logger.exception("[_daily_find_live] LPL schedule 失敗")


def _find_upcoming_series_without_url(hours_ahead: int) -> list[dict]:
    """找未來 N 小時內 upcoming series 但沒對到 broadcast.stream_url 的清單。

    排除已走 bilibili_vod 流程的 series（LPL 預設 stream_url 空白
    是正常狀態，要等 +80min 後 lpl_downloader 從 Bilibili 找到 BV 才填上，
    不該排 retry / alert）。
    """
    sql = """
        SELECT s.series_id, s.match_datetime_utc, s.match_date,
               l.code AS league_code, l.league_id
        FROM series s
        JOIN leagues l ON l.league_id = s.league_id
        LEFT JOIN broadcast_series bs ON bs.series_id = s.series_id
        LEFT JOIN broadcasts b ON b.broadcast_id = bs.broadcast_id
        WHERE s.match_datetime_utc IS NOT NULL
          AND s.match_datetime_utc > UTC_TIMESTAMP()
          AND s.match_datetime_utc <= DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s HOUR)
          AND (s.status IS NULL OR s.status NOT IN ('completed', 'cancelled'))
          AND NOT EXISTS (
              SELECT 1 FROM broadcasts b2
              JOIN broadcast_series bs2 ON bs2.broadcast_id = b2.broadcast_id
              WHERE bs2.series_id = s.series_id
                AND b2.platform = 'bilibili_vod'
          )
        GROUP BY s.series_id, s.match_datetime_utc, s.match_date, l.code, l.league_id
        HAVING SUM(IF(b.stream_url IS NOT NULL AND b.stream_url != '', 1, 0)) = 0
        ORDER BY s.match_datetime_utc
    """
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (hours_ahead,))
            return list(cur.fetchall())


def _schedule_per_match_url_retries(scheduler) -> None:
    """檢查未來 24h upcoming series，沒 URL 的排 -3h / -30min retry + 30min 後 alert。"""
    cfg = _load_config().get("scheduler", {})
    thresholds_h = cfg.get("find_live_pre_match_retry_hours", [3, 0.5])
    check_ahead_h = int(cfg.get("find_live_check_hours_ahead", 24))
    now = utc_now()

    series_list = _find_upcoming_series_without_url(check_ahead_h)
    if not series_list:
        logger.info("未來 %dh 所有 upcoming series 都已有 URL，無 retry 需排程", check_ahead_h)
        return

    logger.info("未來 %dh 發現 %d 場 series 缺 URL -> 排 retry job",
                check_ahead_h, len(series_list))
    for s in series_list:
        match_t = from_db_utc(s["match_datetime_utc"])
        if match_t is None or match_t <= now:
            continue
        sid = s["series_id"]
        league_code = s["league_code"]

        # 排 retry jobs
        for th in thresholds_h:
            run_at = match_t - timedelta(hours=float(th))
            if run_at <= now:
                continue
            label = f"{int(float(th)*60)}min" if float(th) < 1 else f"{int(float(th))}h"
            scheduler.add_job(
                _retry_find_live_for_league,
                "date", run_date=run_at,
                id=f"retry_find_live_{sid}_{label}",
                replace_existing=True,
                args=[scheduler, sid, league_code],
                misfire_grace_time=600,
                coalesce=True,
            )
        # 排 alert job：T - 30min + 60s（最後一次 retry 完才 alert）
        last_retry_h = float(thresholds_h[-1]) if thresholds_h else 0.5
        alert_at = match_t - timedelta(hours=last_retry_h) + timedelta(seconds=60)
        if alert_at > now:
            scheduler.add_job(
                _alert_url_missing,
                "date", run_date=alert_at,
                id=f"alert_url_missing_{sid}",
                replace_existing=True,
                args=[sid],
                misfire_grace_time=300,
                coalesce=True,
            )


def _retry_find_live_for_league(scheduler, series_id: int, league_code: str) -> None:
    """比賽前 retry：為這 league 重跑 find_live + 排錄影。"""
    logger.info("[retry] series_id=%s league=%s 比賽前重跑 find_live",
                series_id, league_code)
    _run_find_live(leagues=[league_code])
    _schedule_all_upcoming_recordings(scheduler)


# ─────────────────────────────────────────────────────────────────────────────
# LPL 流程（Bilibili 官方剪好的場次 VOD）
# ─────────────────────────────────────────────────────────────────────────────
def _ensure_lpl_broadcasts_exist() -> int:
    """對未來 N 天 LPL series 確保有對應 platform='bilibili_vod' broadcast row。

    回傳新建 row 數。 system_paused.lock 存在則 skip（不新建）。
    """
    if _is_system_paused():
        logger.debug("[ensure_lpl_broadcasts] automation policy blocked; skip")
        return 0
    cfg = _load_config().get("scheduler", {})
    days_ahead = int(cfg.get("lpl_check_days_ahead", 7))
    sql = """
        SELECT s.series_id, s.match_date, s.match_datetime_utc,
               l.code AS league_code, l.league_id,
               ta.code AS team_a, tb.code AS team_b
        FROM series s
        JOIN leagues l ON l.league_id = s.league_id
        LEFT JOIN teams ta ON ta.team_id = s.team_a_id
        LEFT JOIN teams tb ON tb.team_id = s.team_b_id
        LEFT JOIN broadcast_series bs ON bs.series_id = s.series_id
        LEFT JOIN broadcasts b
            ON b.broadcast_id = bs.broadcast_id AND b.platform = 'bilibili_vod'
        WHERE l.code = 'LPL'
          AND s.match_date BETWEEN CURDATE()
              AND DATE_ADD(CURDATE(), INTERVAL %s DAY)
        GROUP BY s.series_id, s.match_date, s.match_datetime_utc,
                 l.code, l.league_id, ta.code, tb.code
        HAVING COUNT(b.broadcast_id) = 0
    """
    n_new = 0
    from automation.db.repositories import BroadcastRepo, BroadcastSeriesRepo
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (days_ahead,))
            rows = list(cur.fetchall())
        if not rows:
            logger.info("[ensure_lpl_broadcasts] 未來 %dd 無缺 LPL bilibili_vod broadcast", days_ahead)
            return 0

        repo = BroadcastRepo(conn)
        bs_repo = BroadcastSeriesRepo(conn)
        for r in rows:
            ext_id = f"lpl_series_{r['series_id']}"
            ta = r.get("team_a") or "?"
            tb = r.get("team_b") or "?"
            title = f"[LPL] {ta} vs {tb} @ {r['match_date']}"
            try:
                bid, is_new, _ = repo.upsert_by_external(
                    platform="bilibili_vod",
                    external_id=ext_id,
                    broadcast_date=r["match_date"],
                    league_id=r["league_id"],
                    league_code="LPL",
                    league_timezone="Asia/Shanghai",
                    url="",
                    title=title,
                    scheduled_start_utc=r["match_datetime_utc"],
                    source_status="upcoming",
                    confidence="high",
                )
                bs_repo.add_mapping(
                    broadcast_id=bid, series_id=r["series_id"],
                    series_order=1, mapping_confidence="high",
                    mapping_source="schedule_scrape_lpl",
                    mapping_reason="LPL bilibili_vod auto",
                )
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE broadcasts SET auto_clip=1 WHERE broadcast_id=%s",
                        (bid,),
                    )
                if is_new:
                    n_new += 1
            except Exception:
                logger.exception("LPL broadcast row 建立失敗 series_id=%s", r['series_id'])
        conn.commit()
    logger.info("[ensure_lpl_broadcasts] LPL bilibili_vod broadcast 新建 %d 筆", n_new)
    return n_new


def _schedule_lpl_downloads(scheduler) -> int:
    """Schedule first attempts for recent or upcoming LPL broadcasts only."""
    if _is_system_paused():
        logger.debug("[schedule_lpl_downloads] automation policy blocked; skip")
        return 0
    cfg = _load_config().get("scheduler", {})
    offset_min = int(cfg.get("lpl_post_match_offset_minutes", 80))
    lookback_days = int(cfg.get("lpl_schedule_lookback_days", 2))
    lookahead_days = int(cfg.get("lpl_check_days_ahead", 7))
    sql = """
        SELECT broadcast_id, scheduled_start_utc
        FROM broadcasts
        WHERE platform = 'bilibili_vod'
          AND recording_status_v2 IS NULL
          AND COALESCE(retry_count, 0) = 0
          AND scheduled_start_utc IS NOT NULL
          AND scheduled_start_utc BETWEEN
              DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s DAY)
              AND DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s DAY)
    """
    n = 0
    now = utc_now()
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (lookback_days, lookahead_days))
            rows = list(cur.fetchall())
    for r in rows:
        sched_t = from_db_utc(r["scheduled_start_utc"])
        if sched_t is None:
            continue
        run_at = sched_t + timedelta(minutes=offset_min)
        if run_at <= now:
            run_at = now + timedelta(seconds=10)
        scheduler.add_job(
            _spawn_lpl_downloader, "date",
            run_date=run_at, id=f"lpl_download_{r['broadcast_id']}",
            replace_existing=True, args=[r["broadcast_id"]],
            misfire_grace_time=3600, coalesce=True,
        )
        n += 1
    logger.info("[schedule_lpl_downloads] 排 %d 個 LPL download job", n)
    return n


def _periodic_naming_retry() -> None:
    """每 5 min 重試未確認命名的 game。

    撈 broadcast_games WHERE naming_provisional=TRUE AND naming_attempts < 4，
    對每個 game 呼叫 try_finalize_naming（refetch lolesports → 推算 series → rename）。
    達 attempts=4 後 cron 不再撈到，接受暫命名。
    """
    if _is_system_paused():
        logger.debug("[periodic_naming_retry] automation policy blocked; skip")
        return
    try:
        from automation.services.naming_finalizer import run_periodic_retry
        run_periodic_retry(max_attempts=4)
    except Exception:
        logger.exception("[periodic_naming_retry] 失敗（不影響其他 cron）")


def _periodic_lpl_progressive_download() -> None:
    """每 5 min cron：用「上一場 detected_at + 80min 為錨點」逐場補抓 LPL series 的 game 2/3。

    解決 WBG vs WE 只抓到 game 1 的 bug。
    BO3/BO5 整 series 沒打完前，Bilibili BV 還沒上傳完所有 parts。
    每 80 min 重 fetch 一次 BV → lpl_downloader 補抓新 parts。

    抓滿 series（cut_count >= score_a+score_b 或 best_of）就停（lpl_downloader 自己會標 recorded）。

    user 可用 logs/lpl_paused.lock 暫停 LPL 處理（lock 存在就 skip cron）。
    """
    if _is_system_paused():
        logger.debug("[periodic_lpl_progressive_download] automation policy blocked; skip")
        return
    if (_LOG_DIR / "lpl_paused.lock").exists():
        logger.info("[periodic_lpl_progressive_download] logs/lpl_paused.lock 存在 -> 暫停 LPL")
        return
    try:
        cfg = _load_config().get("scheduler", {})
        anchor_min = int(cfg.get("lpl_progressive_anchor_minutes", 80))
        first_offset_min = int(cfg.get("lpl_post_match_offset_minutes", 80))
        # 防重複 spawn — cut_count=0 期間 cron 每 5 min 都會撈到此 broadcast，
        # 若上次 spawn 還在跑 → 多個 yt-dlp 同時下同 BV → 全部失敗。
        # 用 b.updated_at 過濾：lpl_downloader 跑時會 UPDATE broadcasts（stream_url/title）→ 更新 updated_at
        # → 10 分鐘內不重 spawn。
        respawn_cooldown_min = max(10, int(cfg.get("lpl_progressive_check_interval_min", 5)) * 2)
        # v2：DB 內 datetime column 混用 timezone（schema 歷史遺留）：
        #   broadcasts.updated_at   → server TZ (Taipei)：ON UPDATE CURRENT_TIMESTAMP()
        #   broadcast_games.detected_at → UTC：顯式 UTC_TIMESTAMP() 寫入
        #   broadcasts.scheduled_start_utc → UTC：顯式 UTC 寫入
        # 裸 DATETIME 比較必須用同 TZ 的對象，否則 timezone 偏移會讓條件永遠 TRUE/FALSE。
        sql = """
            SELECT b.broadcast_id,
                   COUNT(g.game_id) AS cut_count,
                   MAX(g.detected_at) AS last_cut_at,
                   s.best_of, s.score_a, s.score_b, b.scheduled_start_utc
            FROM broadcasts b
            JOIN broadcast_series bs ON bs.broadcast_id = b.broadcast_id
            JOIN series s ON s.series_id = bs.series_id
            LEFT JOIN broadcast_games g
              ON g.broadcast_id = b.broadcast_id AND g.status = 'cut'
            WHERE b.platform = 'bilibili_vod'
              AND b.broadcast_date >= CURDATE() - INTERVAL 1 DAY
              AND (b.recording_status_v2 IS NULL OR b.recording_status_v2 <> 'recorded')
              AND b.updated_at + INTERVAL %s MINUTE < NOW()  -- updated_at 是 server TZ
            GROUP BY b.broadcast_id, s.best_of, s.score_a, s.score_b, b.scheduled_start_utc, b.updated_at
            HAVING (
                COUNT(g.game_id) < COALESCE(NULLIF(s.score_a + s.score_b, 0), s.best_of)
            )
            AND (
                (COUNT(g.game_id) = 0
                 AND scheduled_start_utc + INTERVAL %s MINUTE < UTC_TIMESTAMP())  -- 都 UTC
                OR
                (COUNT(g.game_id) > 0
                 AND MAX(g.detected_at) + INTERVAL %s MINUTE < UTC_TIMESTAMP())  -- detected_at 是 UTC
            )
        """
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (respawn_cooldown_min, first_offset_min, anchor_min))
                rows = list(cur.fetchall())
        for r in rows:
            _spawn_lpl_downloader(r["broadcast_id"])
        if rows:
            logger.info(
                "[periodic_lpl_progressive_download] spawn %d 個 LPL 補抓 "
                "(broadcast_ids=%s, anchor=%d min)",
                len(rows), [r["broadcast_id"] for r in rows], anchor_min,
            )
    except Exception:
        logger.exception("[periodic_lpl_progressive_download] 失敗（不影響其他 cron）")


def _periodic_lpl_retry() -> None:
    """每 5 min cron：對 retry_count BETWEEN 1 AND max-1 的 LPL bilibili_vod broadcast，
    若距離上次 retry 已 ≥ retry_interval_minutes，spawn lpl_downloader 再試。


    原本 retry 只在 _daily_schedule_scrape / _daily_find_live cron 跑完後 _schedule_lpl_downloads
    重排，等於每天 1-2 次。改成每 5 min cron 主動掃 → 1 小時內可完成 max=12 次 retry。

    user 可用 logs/lpl_paused.lock 暫停 LPL 處理（lock 存在就 skip cron）。
    """
    if _is_system_paused():
        logger.debug("[periodic_lpl_retry] automation policy blocked; skip")
        return
    if (_LOG_DIR / "lpl_paused.lock").exists():
        logger.info("[periodic_lpl_retry] logs/lpl_paused.lock 存在 -> 暫停 LPL retry")
        return
    try:
        cfg = _load_config().get("scheduler", {})
        max_retries = int(cfg.get("lpl_max_retries", 12))
        retry_min = int(cfg.get("lpl_retry_interval_minutes", 5))
        lookback_days = int(cfg.get("lpl_retry_lookback_days", 2))
        # 同上，updated_at 是 server timezone，比較對象用 NOW() 不用 UTC_TIMESTAMP()
        sql = """
            SELECT broadcast_id, retry_count
            FROM broadcasts
            WHERE platform = 'bilibili_vod'
              AND recording_status_v2 IS NULL
              AND retry_count BETWEEN 1 AND %s
              AND TIMESTAMPDIFF(MINUTE, updated_at, NOW()) >= %s
              AND scheduled_start_utc >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s DAY)
              AND scheduled_start_utc <= UTC_TIMESTAMP()
        """
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (max_retries - 1, retry_min, lookback_days))
                rows = list(cur.fetchall())
        for r in rows:
            _spawn_lpl_downloader(r["broadcast_id"])
        if rows:
            logger.info(
                "[periodic_lpl_retry] spawn %d 個 LPL retry（broadcast_ids=%s）",
                len(rows), [r["broadcast_id"] for r in rows],
            )
    except Exception:
        logger.exception("[periodic_lpl_retry] 失敗（不影響其他 cron）")


def _spawn_vod_recovery(broadcast_id: int, game_index: int, *, force: bool = False) -> bool:
    """spawn subprocess 跑 automation.tools.recover_broadcast_game --auto。

    Lock file 防重複 spawn：recovery 通常 30-60 min 跑完（partial 1080p ~1.7 GB
    + main.py YOLO ~10 min + clip 處理 ~10 min）。Lock 2 hr 自動過期。

    force=True：中斷救援用，加 --force flag → recover script 砍 mp4 + reset clip_job
    讓 clip_worker 重撈跑 main.py 出新 highlight。

    27：回 bool — True = 真 spawn，False = lock 擋掉 skip。
    呼叫端可據此決定是否 mark recovered=1（避免被擋掉但被誤標已處理）。
    加入 system_paused.lock 全域檢查。
    """
    if _is_system_paused():
        logger.debug("[_spawn_vod_recovery] automation policy blocked; skip bid=%s g%s",
                    broadcast_id, game_index)
        return False
    lock_suffix = "_force" if force else ""
    lock_path = _LOG_DIR / f"vod_recovery_{broadcast_id}_g{game_index}{lock_suffix}.lock"
    if lock_path.exists():
        import time as _t
        age_sec = _t.time() - lock_path.stat().st_mtime
        if age_sec < 2 * 3600:
            logger.debug("[vod_recovery] bid=%s g%s%s lock 存在 (%.0f min) -> skip",
                         broadcast_id, game_index, lock_suffix, age_sec / 60)
            return False
        logger.info("[vod_recovery] bid=%s g%s%s lock 過期清掉",
                    broadcast_id, game_index, lock_suffix)
        try:
            lock_path.unlink()
        except OSError:
            pass

    env = {**os.environ, "KMP_DUPLICATE_LIB_OK": "TRUE", "PYTHONIOENCODING": "utf-8"}
    cmd = [
        sys.executable, "-m", "automation.tools.recover_broadcast_game",
        "--broadcast-id", str(broadcast_id),
        "--game-index", str(game_index),
        "--auto",
    ]
    if force:
        cmd.append("--force")
    subprocess.Popen(cmd, env=env)
    lock_path.touch()
    logger.info("[vod_recovery] spawn broadcast_id=%s game_index=%s force=%s",
                broadcast_id, game_index, force)
    return True


def _periodic_interruption_recovery() -> None:
    """每 30 min 掃 recording_interruptions 表，對 broadcast 結束 + 有未補錄中斷的場次重 recover。

    流程（system_paused.lock 存在則 skip）：
      1. 撈 unrecovered interruptions WHERE broadcast.recording_status_v2='recorded'（直播結束）
      2. 對每筆 interruption：找 broadcast_games 中 start_offset_sec ≤ interrupt ≤ end_offset_sec 的 game
      3. spawn recover_broadcast_game --auto --force（覆蓋既有 mp4 + reset clip_job）
      4. mark recording_interruptions.recovered=1（避免重複觸發）

    不直播中跑（archive VOD 直播時拿不到）。
    """
    if _is_system_paused():
        logger.debug("[periodic_interruption_recovery] automation policy blocked; skip")
        return
    # user 訴求「不要自動補錄，我自己選」→ 預設關閉，dashboard 提供手動按鈕
    cfg = _load_config().get("scheduler", {})
    if not cfg.get("auto_recovery_enabled", False):
        logger.debug("[periodic_interruption_recovery] auto_recovery_enabled=false -> skip（dashboard 手動觸發）")
        return
    try:
        sql = """
            SELECT i.id, i.broadcast_id, i.interrupted_at_sec, i.resumed_at_sec,
                   b.actual_end_utc, b.recording_ended_at
            FROM recording_interruptions i
            JOIN broadcasts b ON b.broadcast_id = i.broadcast_id
            WHERE i.recovered = 0
              AND b.recording_status_v2 = 'recorded'
              AND COALESCE(b.actual_end_utc, b.recording_ended_at) + INTERVAL 30 MINUTE
                   < UTC_TIMESTAMP()
        """
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                interruptions = list(cur.fetchall())
                if not interruptions:
                    logger.debug("[interruption_recovery] 沒未補錄的中斷")
                    return

                triggered_keys = set()     # (broadcast_id, game_index) 防同 game 多次 spawn
                spawn_blocked_keys = set()  # 被 lock 擋掉的（不該 mark recovered）
                for it in interruptions:
                    bid = it["broadcast_id"]
                    interrupt_sec = float(it["interrupted_at_sec"])

                    cur.execute(
                        "SELECT game_id, game_index, start_offset_sec, end_offset_sec "
                        "FROM broadcast_games "
                        "WHERE broadcast_id=%s AND status='cut' "
                        "  AND start_offset_sec <= %s "
                        "  AND (end_offset_sec IS NULL OR end_offset_sec >= %s)",
                        (bid, interrupt_sec, interrupt_sec),
                    )
                    games = list(cur.fetchall())
                    if not games:
                        logger.warning(
                            "[interruption_recovery] broadcast %s 中斷 @ %.0fs "
                            "找不到涵蓋的 game -> mark recovered（無 game 可補）",
                            bid, interrupt_sec,
                        )
                        cur.execute(
                            "UPDATE recording_interruptions SET recovered=1, "
                            "  recovered_at=CURRENT_TIMESTAMP() WHERE id=%s",
                            (it["id"],),
                        )
                        continue

                    # track 真正 spawn / 被擋掉的 game，決定是否 mark recovered
                    any_spawned = False
                    all_blocked = True
                    for g in games:
                        key = (bid, g["game_index"])
                        if key in triggered_keys:
                            continue
                        triggered_keys.add(key)
                        logger.info(
                            "[interruption_recovery] broadcast %s g%d 涵蓋中斷 @ %.0fs "
                            "-> spawn force recover",
                            bid, g["game_index"], interrupt_sec,
                        )
                        spawned = _spawn_vod_recovery(bid, g["game_index"], force=True)
                        if spawned:
                            any_spawned = True
                            all_blocked = False
                        else:
                            spawn_blocked_keys.add(key)

                    # 只有「真 spawn」或「無 game」才 mark recovered
                    # 若所有 game 都被 lock 擋掉 → 保留 recovered=0 讓下一輪 cron 重試
                    if any_spawned:
                        cur.execute(
                            "UPDATE recording_interruptions SET recovered=1, "
                            "  recovered_at=CURRENT_TIMESTAMP() WHERE id=%s",
                            (it["id"],),
                        )
                    elif all_blocked:
                        logger.info(
                            "[interruption_recovery] broadcast %s 中斷 @ %.0fs 所有 game 被 lock 擋掉 "
                            "-> 保留 recovered=0 等下次 cron 重試",
                            bid, interrupt_sec,
                        )
                conn.commit()
        if triggered_keys:
            logger.info(
                "[interruption_recovery] %d 個 spawn 成功 / %d 個被 lock 擋",
                len(triggered_keys) - len(spawn_blocked_keys), len(spawn_blocked_keys),
            )
    except Exception:
        logger.exception("[periodic_interruption_recovery] 失敗（不影響其他 cron）")


def _periodic_vod_fallback_check() -> None:
    """每 30 min 比對 LCK/LCP/LEC/LCS broadcast 已切 game 數 vs lolesports series 比分總和。

    漏的 game_index 自動 spawn recover_broadcast_game.py --auto（call planner 推範圍 → yt-dlp
    切 partial → INSERT broadcast_games + enqueue clip_job → clip_worker 跑 main.py 出 highlight）。

    篩選條件：
      - platform='youtube'（LPL bilibili_vod 有自己的 _periodic_lpl_progressive_download 流程）
      - league_code IN ('LCK','LCP','LEC','LCS','WCS','MSI')
      - recording_status_v2 IN ('recorded','post_recording')
      - broadcast_date >= CURDATE() - 7 DAY（只看最近 7 天）
      - cut_count < expected_games (sum of series.score_a + score_b)
      - 距離 broadcast 結束 ≥ 30 min（拒絕還在直播 / 剛結束未上 archive）

    防重複 spawn：file lock + planner 推算現有 game_index 排除已 cut 的。
    system_paused.lock 存在則 skip。
    """
    if _is_system_paused():
        logger.debug("[periodic_vod_fallback_check] automation policy blocked; skip")
        return
    # user 訴求「不要自動補錄」→ 預設關閉，dashboard 提供手動按鈕
    cfg = _load_config().get("scheduler", {})
    if not cfg.get("auto_recovery_enabled", False):
        logger.debug("[periodic_vod_fallback_check] auto_recovery_enabled=false -> skip（dashboard 手動觸發）")
        return
    try:
        # 用 subquery 各自算 cut_count / expected_games，避免 JOIN 笛卡兒積
        sql = """
            SELECT b.broadcast_id, b.league_code, b.broadcast_date,
                   (SELECT COUNT(*) FROM broadcast_games g
                    WHERE g.broadcast_id = b.broadcast_id AND g.status = 'cut') AS cut_count,
                   (SELECT SUM(COALESCE(NULLIF(s.score_a + s.score_b, 0), 0))
                    FROM broadcast_series bs
                    JOIN series s ON s.series_id = bs.series_id
                    WHERE bs.broadcast_id = b.broadcast_id) AS expected_games
            FROM broadcasts b
            WHERE b.platform = 'youtube'
              AND b.league_code IN ('LCK','LCP','LEC','LCS','WCS','MSI')
              AND b.recording_status_v2 IN ('recorded','post_recording')
              AND b.broadcast_date >= CURDATE() - INTERVAL 7 DAY
              AND b.broadcast_date <= CURDATE()
              AND (b.actual_end_utc IS NULL
                   OR b.actual_end_utc + INTERVAL 30 MINUTE < UTC_TIMESTAMP())
            HAVING expected_games > 0 AND cut_count < expected_games
        """
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                broadcasts = list(cur.fetchall())
                if not broadcasts:
                    logger.debug("[vod_fallback_check] 沒漏場 broadcast")
                    return
                spawn_count = 0
                for b in broadcasts:
                    bid = b["broadcast_id"]
                    expected = int(b["expected_games"])
                    cur.execute(
                        "SELECT game_index FROM broadcast_games "
                        "WHERE broadcast_id=%s AND status IN ('cut','cutting','detecting')",
                        (bid,),
                    )
                    existing = {row["game_index"] for row in cur.fetchall()}
                    missing = [i for i in range(1, expected + 1) if i not in existing]
                    if not missing:
                        continue
                    logger.info(
                        "[vod_fallback_check] broadcast %s (%s %s) cut=%s expected=%s missing=%s",
                        bid, b["league_code"], b["broadcast_date"],
                        b["cut_count"], expected, missing,
                    )
                    for game_index in missing:
                        _spawn_vod_recovery(bid, game_index)
                        spawn_count += 1
        if spawn_count:
            logger.info("[vod_fallback_check] spawn %d 個 VOD recovery", spawn_count)
    except Exception:
        logger.exception("[periodic_vod_fallback_check] 失敗（不影響其他 cron）")


def _periodic_cookie_check() -> None:
    """每天 10:00 Asia/Taipei 檢查 Bilibili cookies 過期。

    過期前 7 天 → Windows toast 提醒 user 開 Firefox 重登。
    > 7 天靜默 OK。
    """
    if _is_system_paused():
        logger.debug("[cookie_check] automation policy blocked; skip")
        return
    try:
        from automation.sources.bilibili_cookie_check import check_and_alert
        cfg = _load_config().get("scheduler", {})
        threshold = int(cfg.get("cookie_alert_threshold_days", 7))
        result = check_and_alert(alert_threshold_days=threshold)
        logger.info("[cookie_check] %s", result)
    except Exception:
        logger.exception("[periodic_cookie_check] 失敗（不影響其他 job）")


def _spawn_lpl_downloader(broadcast_id: int) -> None:
    """spawn subprocess 跑 automation.run --lpl-download <bid>。

    先檢查 file lock（lpl_downloader_<bid>.lock）— 若已有 instance 在跑，
    直接 skip subprocess.Popen，避免 log 被「lock 擋掉」的 WARNING 灌爆。
    加入 system_paused.lock / lpl_paused.lock 全域檢查（不管誰 call 都擋）。
    """
    if _is_system_paused():
        logger.debug("[_spawn_lpl_downloader] automation policy blocked; skip bid=%s", broadcast_id)
        return
    if (_LOG_DIR / "lpl_paused.lock").exists():
        logger.info("[_spawn_lpl_downloader] lpl_paused.lock -> skip bid=%s", broadcast_id)
        return
    lock_path = _LOG_DIR / f"lpl_downloader_{broadcast_id}.lock"
    if lock_path.exists():
        try:
            other_pid = int(lock_path.read_text().strip())
        except (ValueError, OSError):
            other_pid = -1
        if _pid_alive(other_pid):
            logger.debug("lpl_downloader bid=%s 已有 pid=%s 在跑 -> skip spawn",
                         broadcast_id, other_pid)
            return
        # stale lock → 留給 lpl_downloader 自己接管（它有 stale check）

    env = {**os.environ, "KMP_DUPLICATE_LIB_OK": "TRUE", "PYTHONIOENCODING": "utf-8"}
    cmd = [sys.executable, "-m", "automation.run", "--lpl-download", str(broadcast_id)]
    subprocess.Popen(cmd, env=env)
    logger.info("spawn lpl_downloader broadcast_id=%s", broadcast_id)


def _alert_url_missing(series_id: int) -> None:
    """比賽前 30 分鐘 + 60s 還沒 URL → log ERROR。"""
    sql = """
        SELECT s.series_id, s.match_datetime_utc, s.match_date,
               l.code AS league_code, ta.code AS team_a, tb.code AS team_b
        FROM series s
        JOIN leagues l ON l.league_id = s.league_id
        LEFT JOIN teams ta ON ta.team_id = s.team_a_id
        LEFT JOIN teams tb ON tb.team_id = s.team_b_id
        LEFT JOIN broadcast_series bs ON bs.series_id = s.series_id
        LEFT JOIN broadcasts b ON b.broadcast_id = bs.broadcast_id
        WHERE s.series_id = %s
        GROUP BY s.series_id
        HAVING SUM(IF(b.stream_url IS NOT NULL AND b.stream_url != '', 1, 0)) = 0
    """
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (series_id,))
            row = cur.fetchone()
    if row is None:
        # URL 已經有了 → 不 alert
        logger.info("[alert_url_missing] series %s URL 已找到，不 alert", series_id)
        return
    logger.error(
        "[ALERT] 找不到直播 URL：series_id=%d %s %s vs %s @ %s （比賽前 30 分鐘 + retry 都失敗）",
        row["series_id"], row["league_code"],
        row.get("team_a") or "?", row.get("team_b") or "?",
        row["match_datetime_utc"],
    )


# ─────────────────────────────────────────────────────────────────────────────
def run_scheduler() -> None:
    """啟動 BlockingScheduler 常駐進程。"""
    config = _load_config()
    sched_cfg = config.get("scheduler", {})

    scheduler = BlockingScheduler(
        timezone=ZoneInfo("UTC"),
        job_defaults={
            "coalesce": True,             # 評審 #10
            "max_instances": 1,
            "misfire_grace_time": 1800,   # 30 分鐘
        },
    )

    _periodic_policy_refresh(scheduler)
    _on_startup(scheduler)

    scheduler.add_job(
        _periodic_policy_refresh,
        "interval",
        seconds=60,
        id="automation_policy_refresh",
        replace_existing=True,
        args=[scheduler],
        misfire_grace_time=60,
        coalesce=True,
    )

    # daily schedule_scrape (cron 09:00 Asia/Taipei)
    scrape_at = sched_cfg.get("schedule_scrape_at", "09:00")
    scrape_tz = sched_cfg.get("schedule_scrape_timezone", "Asia/Taipei")
    h, m = (int(x) for x in scrape_at.split(":"))
    scheduler.add_job(
        _periodic_schedule_scrape,
        "cron",
        hour=h, minute=m, timezone=ZoneInfo(scrape_tz),
        id="daily_schedule_scrape",
        replace_existing=True,
        next_run_time=utc_now() + timedelta(seconds=30),  # 啟動後 30 秒先跑一次補資料
        misfire_grace_time=3600,
        coalesce=True,
        args=[scheduler],   # scrape 跑完接 LPL ensure + schedule
    )
    logger.info("已註冊 daily_schedule_scrape：每天 %s %s", scrape_at, scrape_tz)

    # daily Bilibili cookie expiry check（10:00 Asia/Taipei）
    cookie_check_at = sched_cfg.get("cookie_check_at", "10:00")
    cookie_check_tz = sched_cfg.get("cookie_check_timezone", "Asia/Taipei")
    ch, cm = (int(x) for x in cookie_check_at.split(":"))
    scheduler.add_job(
        _periodic_cookie_check,
        "cron",
        hour=ch, minute=cm, timezone=ZoneInfo(cookie_check_tz),
        id="daily_cookie_check",
        replace_existing=True,
        next_run_time=utc_now() + timedelta(seconds=45),
        misfire_grace_time=3600,
        coalesce=True,
    )
    logger.info("已註冊 daily_cookie_check：每天 %s %s（Bilibili SESSDATA 過期前 7 天 toast）",
                cookie_check_at, cookie_check_tz)

    # 每 5 min 跑 LPL retry（解決 LPL #78 找不到 BV 後沒持續 retry 的 bug）
    lpl_retry_min = int(sched_cfg.get("lpl_retry_interval_minutes", 5))
    scheduler.add_job(
        _periodic_lpl_retry,
        "interval",
        minutes=lpl_retry_min,
        id="periodic_lpl_retry",
        replace_existing=True,
        next_run_time=utc_now() + timedelta(seconds=90),  # 啟動 90s 後第一次跑
        misfire_grace_time=300,
        coalesce=True,
    )
    logger.info("已註冊 periodic_lpl_retry：每 %d 分鐘掃 LPL retry_count > 0 的 broadcast", lpl_retry_min)

    # 每 5 min 用「上一場錨點 + 80min」逐場補抓 BO3/BO5 後續 game
    progressive_check_min = int(sched_cfg.get("lpl_progressive_check_interval_min", 5))
    scheduler.add_job(
        _periodic_lpl_progressive_download,
        "interval",
        minutes=progressive_check_min,
        id="periodic_lpl_progressive_download",
        replace_existing=True,
        next_run_time=utc_now() + timedelta(seconds=180),
        misfire_grace_time=300,
        coalesce=True,
    )
    logger.info("已註冊 periodic_lpl_progressive_download：每 %d 分鐘用上一場錨點補抓 LPL game 2/3",
                progressive_check_min)

    # hupu_sync 改成 on-demand — naming_finalizer 進入 apply_hupu_fallback
    # 前自動呼叫 sync_if_stale（5 min cache throttle）。沒卡住的 game 時 cache 不需更新 → 省 API 流量。

    # 每 5 min 重試多 series broadcast 的命名確認（refetch lolesports + rename）
    scheduler.add_job(
        _periodic_naming_retry,
        "interval",
        minutes=5,
        id="periodic_naming_retry",
        replace_existing=True,
        next_run_time=utc_now() + timedelta(seconds=120),
        misfire_grace_time=300,
        coalesce=True,
    )
    logger.info("已註冊 periodic_naming_retry：每 5 分鐘重試多 series broadcast 的 naming_provisional game")

    # 每 30 min 比對 broadcast 已切 game 數 vs lolesports series 比分，
    # 漏場自動觸發 VOD 補錄（planner 推範圍 → yt-dlp 切 partial → main.py 出 highlight）
    vod_fallback_min = int(sched_cfg.get("vod_fallback_check_interval_min", 30))
    scheduler.add_job(
        _periodic_vod_fallback_check,
        "interval",
        minutes=vod_fallback_min,
        id="periodic_vod_fallback_check",
        replace_existing=True,
        next_run_time=utc_now() + timedelta(seconds=240),  # 啟動 4 min 後第一次跑
        misfire_grace_time=600,
        coalesce=True,
    )
    logger.info("已註冊 periodic_vod_fallback_check：每 %d 分鐘掃 LCK/LCP 漏場 -> 自動 VOD 補錄",
                vod_fallback_min)

    # 每 30 min 掃 recording_interruptions → broadcast 結束後從 archive VOD 補回中斷段
    interruption_check_min = int(sched_cfg.get("interruption_recovery_check_interval_min", 30))
    scheduler.add_job(
        _periodic_interruption_recovery,
        "interval",
        minutes=interruption_check_min,
        id="periodic_interruption_recovery",
        replace_existing=True,
        next_run_time=utc_now() + timedelta(seconds=300),
        misfire_grace_time=600,
        coalesce=True,
    )
    logger.info("已註冊 periodic_interruption_recovery：每 %d 分鐘掃 streamlink 中斷 -> 自動 VOD 補錄",
                interruption_check_min)

    # daily find_live (cron) + per-match URL retry on demand
    fl_at = sched_cfg.get("find_live_at", "09:30")
    fl_tz = sched_cfg.get("find_live_timezone", "Asia/Taipei")
    fh, fm = (int(x) for x in fl_at.split(":"))
    scheduler.add_job(
        _daily_find_live,
        "cron",
        hour=fh, minute=fm, timezone=ZoneInfo(fl_tz),
        id="daily_find_live",
        replace_existing=True,
        args=[scheduler],
        next_run_time=utc_now() + timedelta(seconds=60),  # 啟動後 60 秒先跑一次補資料
        misfire_grace_time=3600,
        coalesce=True,
    )
    logger.info("已註冊 daily_find_live：每天 %s %s（+ per-match URL retry on-demand）",
                fl_at, fl_tz)

    # cleanup job
    cleanup_cfg = config.get("cleanup", {}) or {}
    if cleanup_cfg.get("enabled", False):
        tz = ZoneInfo(cleanup_cfg.get("timezone", "Asia/Taipei"))
        h, m = map(int, cleanup_cfg.get("daily_at", "03:00").split(":"))
        scheduler.add_job(
            cleanup_old_files,
            "cron",
            hour=h, minute=m, timezone=tz,
            id="cleanup_daily",
            replace_existing=True,
        )
        logger.info("cleanup job 已註冊（每天 %s:%02d %s）", h, m, tz.key)
    else:
        logger.info("cleanup.enabled=false，未註冊 cleanup job（user 用 --cleanup --dry-run 手動觸發）")

    logger.info("=== Scheduler 進入 blocking 等待模式 ===")
    # 心跳
    hb = HeartbeatThread("scheduler")
    hb.start()
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler 收到中斷信號，shutting down")
        scheduler.shutdown()
    finally:
        hb.stop()


if __name__ == "__main__":
    run_scheduler()

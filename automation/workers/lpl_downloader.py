"""LPL downloader：從 Bilibili 官方號 yt-dlp 下載每場 game VOD → 寫 broadcast_games → enqueue clip_jobs。

跟 LCK / LCP 不一樣：LPL 不錄直播，scheduler 在 match_time + 80 min 觸發本 worker 找官方剪好的場次。

主流程（run_for_broadcast）：
  1. 從 broadcast_id 拉 series + match_date + team codes
  2. 用 bilibili_vod_finder.find_bv_for_series 找對應 BV
  3. 找不到 → increment retry_count；< max 由 scheduler 5min 後再 spawn；≥ max → log ERROR
  4. 找到 → list BV parts → filter duration > 30 min → 視為 valid games
  5. 對每個 valid part：yt-dlp 下載 + INSERT broadcast_games(status='cut') + enqueue clip_jobs
  6. UPDATE broadcasts: stream_url=BV_URL, recording_status_v2='recorded', title=BV_title
"""

from __future__ import annotations

import os
import random
import subprocess
import time
from pathlib import Path

from automation.db.connection import mysql_conn
from automation.db.repositories import (
    BroadcastGameRepo,
    BroadcastSeriesRepo,
    BroadcastStateRepo,
    ClipJobRepo,
)
from automation.infra.log_setup import setup_rotating_log
from automation.infra.config import load_config as _load_config
from automation.sources import bilibili_vod_finder

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = _PROJECT_ROOT / "_tmp" / "logs"

logger = setup_rotating_log("lpl_downloader", _LOG_DIR / "lpl_downloader.log")


def _load_series_full(series_id: int) -> dict:
    """讀 series + 對應 team codes。"""
    sql = """
        SELECT s.series_id, s.match_date, s.match_datetime_utc, s.best_of, s.status,
               l.code AS league_code,
               ta.code AS team_a_code, tb.code AS team_b_code
        FROM series s
        JOIN leagues l ON l.league_id = s.league_id
        LEFT JOIN teams ta ON ta.team_id = s.team_a_id
        LEFT JOIN teams tb ON tb.team_id = s.team_b_id
        WHERE s.series_id = %s
    """
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (series_id,))
            row = cur.fetchone()
    if not row:
        raise ValueError(f"series_id={series_id} not found")
    return row


def _make_output_dir(broadcast: dict, series: dict) -> tuple[Path, str]:
    """命名格式（user 要求）：<LEAGUE>_<YYYYMMDD>_<TA>vs<TB>

    例：LPL_20260507_NIPvsAL（取代舊格式 LPL_20260507_bilibili_lpl_series_2026050702002）
    隊伍 code 從 series 撈；缺資料退回 TBD（極罕見）。
    """
    cfg = _load_config().get("scheduler", {})
    # lpl_download_dir 沒設就讀 highlight.utils.paths（VIDEO_DIR env override）
    if cfg.get("lpl_download_dir"):
        root = Path(cfg["lpl_download_dir"])
    else:
        from highlight.utils import paths as _paths
        root = _paths.lol_games_vods_dir()
    league = broadcast["league_code"]
    date_str = broadcast["broadcast_date"].strftime("%Y%m%d")
    team_a = (series.get("team_a_code") or "TBD").upper()
    team_b = (series.get("team_b_code") or "TBD").upper()
    prefix = f"{league}_{date_str}_{team_a}vs{team_b}"
    out = root / prefix
    out.mkdir(parents=True, exist_ok=True)
    return out, prefix


def _ytdlp_download(
    part_url: str,
    output_path: Path,
    *,
    cookies_from_browser: str | None = None,
    cookies_file: str | None = None,
) -> None:
    """yt-dlp 下載單個 BV part 到 output_path。

    bug fix（NIP vs AL part 1 下載失敗事故根因）：
    舊版 cmd 完全沒帶 cookies / fake-UA / sleep / retries，Bilibili 偵測到頻繁存取
    給 0 bytes 回應，yt-dlp 預設 retry 10 次後放棄。

    新版用跟 bilibili_vod_finder 同樣的 _ytdlp_cmd_base（含 cookies + UA + sleep）+
    再加 --retries 30 + --fragment-retries 30 提高耐心。
    """
    # 從 finder import 共用 cmd base（避免複製貼上）
    from automation.sources.bilibili_vod_finder import _ytdlp_cmd_base

    cmd = _ytdlp_cmd_base(cookies_from_browser, cookies_file, sleep_requests=2.0)
    cmd.extend([
        "--no-playlist",
        #
        # 鐵則背景：LPL UP/LNG g2 case — Bilibili 對同一 BV 不同 part 上傳多個 1080p
        # stream（avc1/H.264 4 Mbps、hev1/H.265 1.5 Mbps、av01/AV1 1.4 Mbps），yt-dlp 預設
        # 偏好 av01（高效率新編碼）→ 結果拿到 0.58 Mbps 偏低 bit_rate 版本（畫面細節壓爛）
        # → YOLO 偵測 kill_feed/end_graph 完全失常 → highlight 失敗。
        #
        # 規則：1080p 解析度內，優先 H.264 (avc1) 高 bit_rate 版本；fallback 到其他 1080p
        #      sort by tbr（拿 res 最高且 tbr 最高的）。沒 1080p 直接 fail（不接受 720p）。
        #
        # selector 鏈：
        #   bv*[height>=1080][vcodec~='avc1']+ba   ← 優先：1080p H.264 avc1（Bilibili 上 bit_rate 最高）
        #   / bv*[height>=1080]+ba                 ← 退而求其次：任何 1080p（用 --format-sort 挑 tbr）
        # --format-sort res,tbr：同 group 內 res 高優先，再 tbr 高優先。
        #
        # 限速應對：--limit-rate 2M + 加長 sleep。Bilibili 還擋 → 走 WARP / 等限速解。
        "-f", "bv*[height>=1080][vcodec~='avc1']+ba/bv*[height>=1080]+ba",
        "--format-sort", "res,tbr",
        "--merge-output-format", "mp4",
        "--limit-rate", "2M",
        "--sleep-requests", "5",
        "--sleep-interval", "2",
        "--max-sleep-interval", "8",
        "-o", str(output_path),
        "--no-progress",
        "--retries", "30",
        "--fragment-retries", "30",
        part_url,
    ])
    logger.info("yt-dlp 下載 %s -> %s（1080p high-tbr first + 2M limit + cookies=%s）",
                part_url, output_path.name, cookies_from_browser or cookies_file or "none")
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(
            f"yt-dlp download 失敗 rc={r.returncode}: {r.stderr[-500:]}"
        )
    if not output_path.is_file() or output_path.stat().st_size < 100 * 1024 * 1024:
        # 不到 100MB 視為下載沒完成（LPL 一場通常 1-2GB）
        raise RuntimeError(
            f"下載輸出疑似不完整：{output_path} ({output_path.stat().st_size if output_path.exists() else 0} bytes)"
        )


def _handle_failure(broadcast_id: int, message: str) -> None:
    """Persist every failed attempt so scheduler retries always make progress."""
    cfg = _load_config().get("scheduler", {})
    max_retries = int(cfg.get("lpl_max_retries", 12))
    with mysql_conn() as conn:
        n = BroadcastStateRepo(conn).record_retry_failure(broadcast_id, message)
    if n >= max_retries:
        logger.error(
            "[FAIL] LPL broadcast %s failed %d/%d times; stop retrying: %s",
            broadcast_id, n, max_retries, message,
        )
        with mysql_conn() as conn:
            BroadcastStateRepo(conn).update_status(
                broadcast_id, "failed",
                error_message=f"LPL failed after {n} retries: {message}",
            )
    else:
        logger.info(
            "LPL broadcast %s retry recorded at %d/%d: %s",
            broadcast_id, n, max_retries, message,
        )


def _handle_not_found(broadcast_id: int) -> None:
    _handle_failure(broadcast_id, "BV not found")


def run_for_broadcast(broadcast_id: int) -> None:
    """主入口（scheduler trigger 時 spawn 跑這個）。"""
    cfg_root = _load_config()
    cfg = cfg_root.get("scheduler", {})
    uid = int(cfg.get("lpl_bilibili_user_uid", 50329118))
    part_min_dur = int(cfg.get("lpl_part_min_duration_sec", 1800))
    max_videos = int(cfg.get("lpl_finder_max_videos", 20))
    cookies_browser = cfg.get("lpl_cookies_from_browser") or None
    cookies_file = cfg.get("lpl_cookies_file") or None

    # file-lock 防雙重 spawn。spawn + scheduler 重啟自動補 spawn
    # → 4 個 yt-dlp 同時下同 BV → 互相覆蓋 .part → 230 MB partial 截斷檔通過 100MB
    # threshold 被當「完成」。lock 用 O_EXCL 開檔 + 寫 pid，conflict 時檢查 pid 是否還活著。
    lock_path = _LOG_DIR / f"lpl_downloader_{broadcast_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        try:
            other_pid = int(lock_path.read_text().strip())
        except (ValueError, OSError):
            other_pid = -1
        if _pid_alive(other_pid):
            logger.warning(
                "LPL broadcast %s 已有 lpl_downloader (pid=%s) 在跑 -> 跳過本輪",
                broadcast_id, other_pid,
            )
            return
        logger.info("發現 stale lock (pid=%s 已死) -> 接管", other_pid)
        lock_path.unlink(missing_ok=True)
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(lock_fd, str(os.getpid()).encode())
        os.close(lock_fd)
    except FileExistsError:
        # race condition：剛剛檢查時沒有，但寫前被別人搶了
        logger.warning("LPL broadcast %s lock 競賽輸了 -> 跳過本輪", broadcast_id)
        return

    try:
        _run_for_broadcast_impl(
            broadcast_id, cfg, uid, part_min_dur, max_videos,
            cookies_browser, cookies_file,
        )
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("LPL broadcast %s unhandled failure", broadcast_id)
        try:
            _handle_failure(broadcast_id, message)
        except Exception:
            logger.exception("LPL broadcast %s could not persist retry failure", broadcast_id)
    finally:
        lock_path.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    """簡化版 pid alive 檢查（跟 scheduler.py:_pid_alive 一樣邏輯，避免 cyclic import）。"""
    if pid is None or pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        return True  # 沒 psutil → 保守視為活著


def _run_for_broadcast_impl(broadcast_id, cfg, uid, part_min_dur,
                             max_videos, cookies_browser, cookies_file):
    """原 run_for_broadcast 主邏輯（被 lock wrapper 包裝）。"""
    logger.info("=" * 60)
    logger.info("LPL downloader 開始 broadcast_id=%s", broadcast_id)
    logger.info("=" * 60)

    # 1. Load broadcast + series
    with mysql_conn() as conn:
        broadcast = BroadcastStateRepo(conn).get_by_id(broadcast_id)
        if not broadcast:
            raise RuntimeError(f"broadcast_id={broadcast_id} does not exist")
        series_list = BroadcastSeriesRepo(conn).get_series_for_broadcast(broadcast_id)
    if not series_list:
        raise RuntimeError(f"broadcast {broadcast_id} has no series mapping")
    series = _load_series_full(series_list[0]["series_id"])
    logger.info(
        "series_id=%s %s vs %s @ %s",
        series["series_id"], series["team_a_code"], series["team_b_code"],
        series["match_date"],
    )

    # 2. Find BV
    bv = bilibili_vod_finder.find_bv_for_series(
        series["team_a_code"], series["team_b_code"], series["match_date"],
        uid=uid, max_videos=max_videos,
        cookies_from_browser=cookies_browser, cookies_file=cookies_file,
    )
    if bv is None:
        _handle_not_found(broadcast_id)
        return

    logger.info("找到 BV：%s（title=%s, duration=%ds）",
                bv["bvid"], bv["title"], bv["duration_sec"])

    # 3. List parts + filter
    try:
        # 重用 find_bv_for_series 已 fetch 的 _full_info，避免再 fetch 一次
        parts = bilibili_vod_finder.list_bv_parts(
            bv["bvid"],
            cookies_from_browser=cookies_browser, cookies_file=cookies_file,
            cached_info=bv.get("_full_info"),
        )
    except RuntimeError as e:
        logger.error("list_bv_parts 失敗：%s — 算 not found 重試", e)
        _handle_failure(broadcast_id, f"list_bv_parts failed: {e}")
        return

    valid_parts = [p for p in parts if p["duration_sec"] > part_min_dur]
    logger.info("BV %s 共 %d parts，duration > %ds 的 valid parts: %d",
                bv["bvid"], len(parts), part_min_dur, len(valid_parts))
    if not valid_parts:
        logger.error("BV %s 沒有 valid parts (duration > %ds)，視為錯誤 BV",
                     bv["bvid"], part_min_dur)
        _handle_failure(broadcast_id, f"BV {bv['bvid']} has no valid parts")
        return

    # 4. Download + INSERT broadcast_games + enqueue clip_jobs
    # 用 series 的 team codes 命名（LPL_20260507_NIPvsAL_g1.mp4）
    output_dir, prefix = _make_output_dir(broadcast, series)
    bv_url = f"https://www.bilibili.com/video/{bv['bvid']}"

    # 先撈該 broadcast 已 cut 的 game_index 集合，跳過已下載的
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT game_index FROM broadcast_games "
                "WHERE broadcast_id=%s AND status='cut'",
                (broadcast_id,),
            )
            already_cut: set[int] = {r["game_index"] for r in cur.fetchall()}
    if already_cut:
        logger.info("已 cut 過的 game_index：%s（跳過不重下）", sorted(already_cut))

    # part 之間隨機 sleep 10-40s 模擬人工瀏覽
    # 避免「同 BV 連續多 part 機械化請求」觸發 Bilibili 滑動驗證碼風控
    # 實驗驗證：JDG vs TES BO3 第一輪 part 1/3 失敗（連續打同 BV），等 30+ min 後第二輪
    # 連續無間隔 part 1+3 都成功 → 失敗主因是風控被觸發後 30 min 軟黑名單
    inter_part_min = int(cfg.get("lpl_inter_part_sleep_min_sec", 10))
    inter_part_max = int(cfg.get("lpl_inter_part_sleep_max_sec", 40))
    consecutive_fail_limit = int(cfg.get("lpl_consecutive_fail_limit", 2))

    n_success = 0
    n_already = 0
    consecutive_fails = 0
    is_first_download = True
    for i, part in enumerate(valid_parts, start=1):
        # 跳過已下載的 part（補抓缺少 parts 邏輯）
        if i in already_cut:
            n_already += 1
            logger.info("part %d (game_index=%d) 已下載，跳過", i, i)
            continue
        # 第一場不 sleep；之後 part 間隨機 10-40s
        if not is_first_download:
            wait = random.uniform(inter_part_min, inter_part_max)
            logger.info("等 %.1f 秒再抓 part %d（避免機械化觸發風控）", wait, i)
            time.sleep(wait)
        is_first_download = False

        local_path = output_dir / f"{prefix}_g{i}.mp4"
        try:
            _ytdlp_download(
                part["url"], local_path,
                cookies_from_browser=cookies_browser,
                cookies_file=cookies_file,
            )
            consecutive_fails = 0
        except RuntimeError as e:
            logger.error("part %d 下載失敗：%s", i, e)
            consecutive_fails += 1
            # 連 N 次失敗 → 立刻退出讓 progressive cron 排隊（>30 min 後）再試
            # 不要一直在已被風控的 IP 上重打
            if consecutive_fails >= consecutive_fail_limit:
                logger.warning(
                    "連續 %d 個 part 失敗 -> 疑似 IP 被 Bilibili 軟黑名單，停止本輪。"
                    "等 progressive cron（updated_at + 10 min）下輪再試",
                    consecutive_fails,
                )
                break
            continue

        with mysql_conn() as conn:
            game_repo = BroadcastGameRepo(conn)
            existing = game_repo.get_by_index(broadcast_id, i)
            if existing is None:
                game_id = game_repo.insert(
                    broadcast_id=broadcast_id, game_index=i,
                    start_offset_sec=0.0,
                    end_offset_sec=float(part["duration_sec"]),
                    start_source="lpl_official", end_source="lpl_official",
                    status="cut", confidence=1.0,
                    series_id=series["series_id"],
                    team_a_code=series.get("team_a_code"),
                    team_b_code=series.get("team_b_code"),
                    commit=False,
                )
            else:
                game_id = existing["game_id"]
            game_repo.set_cut(game_id, str(local_path.resolve()), commit=False)
            if broadcast.get("auto_clip"):
                ClipJobRepo(conn).enqueue_or_reset_failed(game_id, commit=False)
            conn.commit()
        logger.info("part %d 完成（game_id=%s, %.0f MB）",
                    i, game_id, local_path.stat().st_size / 1024 / 1024)
        n_success += 1

    if n_success == 0 and n_already == 0:
        logger.error("LPL broadcast %s 所有 part 下載都失敗", broadcast_id)
        _handle_failure(broadcast_id, "all BV part downloads failed")
        return
    if n_success == 0 and n_already > 0:
        # BV 沒新 parts（可能 Bilibili 還沒上傳 game 2/3）→ 不算失敗
        # 由 scheduler 的 _periodic_lpl_progressive_download 等下一場錨點時間再試
        logger.info(
            "LPL broadcast %s 已 cut %d 場 game，BV 沒新 parts -> 等下次錨點時再試",
            broadcast_id, n_already,
        )
        # 不標 recorded（讓 scheduler 知道還沒抓完）
        return
    logger.info(
        "LPL broadcast %s 本次新抓 %d 場（先前已 %d 場）",
        broadcast_id, n_success, n_already,
    )

    # 5. UPDATE stream_url + title（永遠 update）
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcasts SET stream_url=%s, title=%s, recording_path=%s "
                "WHERE broadcast_id=%s",
                (bv_url, (bv["title"] or "")[:200], bv_url, broadcast_id),
            )
        conn.commit()

    # 只有「抓滿 best_of」或「達到 series 比分總和」才標 recorded
    # 否則保持 NULL/inProgress，讓 _periodic_lpl_progressive_download 繼續排下一場
    total_cut = n_success + n_already
    expected = int(series.get("score_a") or 0) + int(series.get("score_b") or 0)
    if expected == 0:
        # series 還沒結束（lolesports 沒 score）→ 用 best_of 當保守上限
        expected = int(series.get("best_of") or 5)

    if total_cut >= expected:
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE broadcasts SET recording_ended_at=UTC_TIMESTAMP() "
                    "WHERE broadcast_id=%s",
                    (broadcast_id,),
                )
            conn.commit()
            BroadcastStateRepo(conn).update_status(broadcast_id, "recorded")
        logger.info("[OK] LPL broadcast %s 抓滿（%d/%d parts，BV=%s） -> 標 recorded",
                    broadcast_id, total_cut, expected, bv["bvid"])
    else:
        logger.info(
            "[WAIT] LPL broadcast %s 已抓 %d/%d 場（BV=%s）-> 等下次錨點繼續抓",
            broadcast_id, total_cut, expected, bv["bvid"],
        )

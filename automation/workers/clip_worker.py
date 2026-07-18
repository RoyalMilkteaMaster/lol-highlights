"""clip_worker：撈 clip_jobs pending → 啟 main.py → 寫狀態。

設計：
- 唯一 owner — main.py 完全不碰 clip_jobs
- MySQL GET_LOCK 防雙 worker 同時搶
- has_running + acquire_next_pending 同 transaction（SELECT FOR UPDATE）
- log 寫檔（不用 capture_output 避免 buffer 卡死）
- timeout config 化（不寫死 3 小時）
- log mkdir
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import psutil

from automation.db.connection import mysql_conn
from automation.db.repositories import ClipJobRepo, WorkerLockRepo, BroadcastGameRepo
from automation.infra.heartbeat import HeartbeatThread
from automation.infra.control import automation_status
from automation.infra.config import load_config as _load_config
from automation.infra.log_setup import setup_rotating_log
from automation.infra.process_utils import is_pid_alive_python

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = _PROJECT_ROOT / "_tmp" / "logs"
_MAIN_PY = _PROJECT_ROOT / "highlight" / "main.py"
_PYTHON = sys.executable

logger = setup_rotating_log("clip_worker", _LOG_DIR / "clip_worker.log")

# graceful shutdown + zombie 防護用全局 state
# signal handler 拿來找當前在處理哪個 job + 哪個子程序
_current_job_id: int | None = None
_current_game_id: int | None = None
_current_proc: subprocess.Popen | None = None
_shutting_down: bool = False


@contextmanager
def _held_worker_lock(name: str):
    """在同一條 MySQL connection 上持有命名鎖直到工作結束。"""
    with mysql_conn() as conn:
        lock = WorkerLockRepo(conn)
        acquired = lock.acquire(name, timeout_sec=0)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release(name)


def _tail_file(path: Path, n_chars: int = 500) -> str:
    """讀檔案最後 N 字元（給 error_message 用）。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n_chars * 2))
            return f.read().decode("utf-8", errors="replace")[-n_chars:]
    except OSError:
        return ""


# ──────────────────────────────────────────────────────────────────────────
# clip_worker kill 防護 + zombie 自動偵測
#
# 主防線：_check_and_clear_zombies — 每 poll loop 跑一次
# 輔助防線：_shutdown_handler — Ctrl+C / SIGTERM 觸發
#
# Stop-Process -Force 殺 parent clip_worker 後，child main.py 變孤兒，
# DB 留 status='running' 卡 4 hr，擋住整個 queue。本機制保證自動恢復。
# ──────────────────────────────────────────────────────────────────────────


def _kill_process_tree(pid) -> bool:
    """Kill 整顆 process tree（main.py 底下的 ffmpeg / YOLO subprocess 一起殺）。
    先 terminate（5s 等），還沒死的再 kill；先殺 children 再殺 parent。
    """
    try:
        parent = psutil.Process(int(pid))
    except (psutil.NoSuchProcess, ValueError, TypeError):
        return True

    try:
        children = parent.children(recursive=True)
        for c in children:
            try:
                c.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _gone, alive = psutil.wait_procs(children, timeout=5)
        for c in alive:
            try:
                c.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        try:
            parent.terminate()
            parent.wait(timeout=5)
        except psutil.TimeoutExpired:
            parent.kill()
            try:
                parent.wait(timeout=5)
            except Exception:
                pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return True
    except Exception:
        logger.exception("[ALERT] kill process tree pid=%s 失敗", pid)
        return False


def _interruptable_sleep(total_sec: float, chunk_sec: float = 30.0) -> None:
    """sleep 用 chunks 讓 signal handler 能 sys.exit 早點生效。
    遇 _shutting_down 立刻 return。
    """
    remaining = total_sec
    while remaining > 0 and not _shutting_down:
        s = min(chunk_sec, remaining)
        time.sleep(s)
        remaining -= s


def _is_job_progressing(job_id: int, idle_min_cap: int = 30) -> bool:
    """檢查 clip_job_<id>.stdout.log mtime 是否在 idle_min_cap 分鐘內。
    True = 還在動；False = 卡死。
    """
    log_path = _LOG_DIR / f"clip_job_{job_id}.stdout.log"
    if not log_path.is_file():
        return False
    age_min = (time.time() - log_path.stat().st_mtime) / 60
    return age_min < idle_min_cap


def _verify_highlight_completed(game_id: int, min_size_mb: int = 100) -> tuple[bool, str, float]:
    """檢查 game 對應的 highlight 檔是否完成且 size 合理。

    用途：pid 死時，先看 highlight 有沒有正常出爐 → 有 = 孤兒成功 = mark_done；
         沒有 = 真 zombie = mark_failed + needs_recut=1。

    Returns: (completed, filename_or_reason, size_mb)
    """
    try:
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT game_path FROM broadcast_games WHERE game_id=%s",
                    (game_id,),
                )
                row = cur.fetchone()
    except Exception as e:
        return False, f"DB query failed: {e}", 0

    if not row or not row.get("game_path"):
        return False, "no game_path in DB", 0

    game_path = Path(row["game_path"])
    from highlight.utils import paths as _paths
    finals_dir = _paths.finals_dir()

    hp = finals_dir / f"{game_path.stem}_highlights.mp4"
    if not hp.is_file():
        return False, f"highlight 不存在: {hp.name}", 0
    size_mb = hp.stat().st_size / 1024 / 1024
    if size_mb < min_size_mb:
        return False, f"highlight 太小 {size_mb:.1f}MB < {min_size_mb}MB", size_mb
    return True, hp.name, size_mb


def _check_and_clear_zombies() -> int:
    """掃 zombie clip_jobs，3 種判斷任一觸發 → kill tree + mark_zombie + ALERT。

      1. pid 死了 / pid 重用（is_pid_alive_python = False）
      2. stdout.log > 30 min 沒更新 → 卡 deadlock，kill tree
      3. run_min > 90 min hard cap → kill tree
    find_zombie_candidates 只撈 pid IS NOT NULL 避免誤殺 D wait loop。
    另外處理 pid NULL 但 started > 90 min 的 stuck wait loop（罕見）。
    """
    cleared = 0
    try:
        import pymysql.cursors as _cur_module
        with mysql_conn() as conn:
            candidates = ClipJobRepo(conn).find_zombie_candidates(stale_sec=60)
        # 額外抓 pid NULL 但跑超過 90 min（D wait loop 卡住）
        with mysql_conn() as conn:
            with conn.cursor(_cur_module.DictCursor) as cur:
                cur.execute(
                    "SELECT job_id, game_id, started_at, "
                    "TIMESTAMPDIFF(MINUTE, started_at, UTC_TIMESTAMP()) AS run_min "
                    "FROM clip_jobs "
                    "WHERE status='running' AND pid IS NULL "
                    "  AND started_at IS NOT NULL "
                    "  AND started_at < UTC_TIMESTAMP() - INTERVAL 90 MINUTE"
                )
                stuck_wait = list(cur.fetchall())
        for j in stuck_wait:
            reason = f"pid=NULL (D wait loop) but ran {j['run_min']}min > 90min hard cap"
            logger.warning(
                "[ALERT] D-wait stuck job=%s g=%s — %s",
                j["job_id"], j["game_id"], reason,
            )
            with mysql_conn() as conn2:
                ClipJobRepo(conn2).mark_zombie(
                    j["job_id"], reason, game_id=j["game_id"],
                )
                BroadcastGameRepo(conn2).set_manual_skip(j["game_id"], reason)
            cleared += 1

        for c in candidates:
            alive = is_pid_alive_python(c["pid"], c["started_at"])
            run_min = c["run_min"] or 0
            progressing = _is_job_progressing(c["job_id"], idle_min_cap=30)

            if alive and progressing and run_min <= 90:
                continue

            # pid 死分流 — 先看 highlight 是否其實已完成（孤兒 main.py 成功）
            #   - highlight OK → mark_done（不標 zombie，不亂設 needs_recut）
            #   - highlight 缺 / 太小 → 真 zombie，繼續走 mark_zombie 路徑
            if not alive:
                ok, info, size_mb = _verify_highlight_completed(
                    c["game_id"], min_size_mb=100,
                )
                if ok:
                    logger.warning(
                        "[orphan-recover] job=%s g=%s pid=%s 死但 highlight 完成 "
                        "(%s, %.0fMB) -> mark_done",
                        c["job_id"], c["game_id"], c["pid"], info, size_mb,
                    )
                    with mysql_conn() as conn2:
                        with conn2.cursor() as cur2:
                            cur2.execute(
                                "UPDATE clip_jobs SET status='done', "
                                "  ended_at=UTC_TIMESTAMP(), pid=NULL, "
                                "  error_message=%s "
                                "WHERE job_id=%s AND status='running'",
                                (f"[ORPHAN-OK] highlight 完成 ({info}, {size_mb:.0f}MB) "
                                 f"— parent worker 死了但 main.py 跑完", c["job_id"]),
                            )
                        conn2.commit()
                    cleared += 1
                    continue
                # highlight 沒完成 → 真 zombie
                reason = (
                    f"pid={c['pid']} dead + highlight 缺/不全 ({info}) "
                    f"(started_at={c['started_at']}, ran {run_min}min)"
                )
                need_kill = False
            elif not progressing:
                reason = (
                    f"pid={c['pid']} stdout.log > 30min 沒更新 — 卡 deadlock "
                    f"(ran {run_min}min)"
                )
                need_kill = True
            else:
                reason = (
                    f"pid={c['pid']} 跑超過 {run_min}min > 90min hard cap"
                )
                need_kill = True

            if need_kill:
                logger.warning(
                    "[ALERT] kill process tree pid=%s for job %s",
                    c["pid"], c["job_id"],
                )
                _kill_process_tree(c["pid"])
                time.sleep(3)
                if is_pid_alive_python(c["pid"], c["started_at"]):
                    logger.warning(
                        "[ALERT] kill 後 pid=%s 仍活著，仍 mark zombie",
                        c["pid"],
                    )

            logger.warning(
                "[ALERT] zombie clip_job: job=%s g=%s pid=%s — %s",
                c["job_id"], c["game_id"], c["pid"], reason,
            )
            with mysql_conn() as conn2:
                ClipJobRepo(conn2).mark_zombie(
                    c["job_id"], reason, game_id=c["game_id"],
                )
                BroadcastGameRepo(conn2).set_manual_skip(c["game_id"], reason)
            cleared += 1
    except Exception:
        logger.exception("[ALERT] zombie check 失敗")
    return cleared


def _shutdown_handler(signum, frame):
    """SIGINT (Ctrl+C) / SIGTERM → kill process tree + mark zombie + exit。

    輔助防線（pythonw hidden + Force kill 場景靠 _check_and_clear_zombies 主防線）。
    """
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    logger.warning("[shutdown] 收到 signal %s，graceful 清理...", signum)

    if _current_proc and _current_proc.poll() is None:
        logger.warning(
            "[shutdown] kill child process tree pid=%s",
            _current_proc.pid,
        )
        _kill_process_tree(_current_proc.pid)

    if _current_job_id:
        try:
            with mysql_conn() as conn:
                ClipJobRepo(conn).mark_zombie(
                    _current_job_id,
                    f"killed by signal {signum} during clip_worker shutdown",
                    game_id=_current_game_id,
                )
                if _current_game_id:
                    BroadcastGameRepo(conn).set_manual_skip(
                        _current_game_id,
                        f"killed by signal {signum} during shutdown",
                    )
            logger.warning(
                "[shutdown] job %s mark zombie (failed + needs_recut=1)",
                _current_job_id,
            )
        except Exception:
            logger.exception("[shutdown] mark job failed 失敗")

    sys.exit(0)


def run_worker(
    poll_interval_sec: float = 30.0,
    *,
    once: bool = False,
) -> None:
    """主迴圈。

    Args:
        poll_interval_sec: 沒 job 時 sleep 多久
        once             : True 跑一輪就退（測試用）
    """
    config = _load_config()
    cw_cfg = config.get("clip_worker", {})
    timeout_h = float(cw_cfg.get("main_timeout_hours", 3.0))
    env = {**os.environ, "KMP_DUPLICATE_LIB_OK": "TRUE", "PYTHONIOENCODING": "utf-8"}

    logger.info("clip_worker 啟動（poll=%ss, timeout=%sh）", poll_interval_sec, timeout_h)

    # signal handler 註冊（Ctrl+C / SIGTERM 觸發 graceful shutdown）
    signal.signal(signal.SIGINT, _shutdown_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown_handler)

    # 心跳
    hb = HeartbeatThread("clip_worker")
    hb.start()
    _was_paused = False  # 只在 paused→resumed transition 印 log，避免 30s 刷屏
    try:
      while True:
        # Policy blocked: do not acquire new work; an active main.py may finish.
        decision = automation_status()
        if not decision["allowed"]:
            if not _was_paused:
                logger.info(
                    "clip_worker paused: source=%s reason=%s next_change=%s",
                    decision.get("source"),
                    decision.get("reason"),
                    decision.get("next_change_at") or "none",
                )
                _was_paused = True
            time.sleep(poll_interval_sec)
            if once: return
            continue
        elif _was_paused:
            logger.info("clip_worker resumed by automation policy")
            _was_paused = False

        # 每 poll 偵測 zombie（在 GET_LOCK 前，避免被 has_running 擋住）
        _check_and_clear_zombies()
        # GET_LOCK 是 connection-scoped；保留同一條連線直到本輪工作結束。
        lock_stack = ExitStack()
        lock_acquired = lock_stack.enter_context(_held_worker_lock("clip_worker"))
        if not lock_acquired:
            lock_stack.close()
            logger.debug("已有另一個 clip_worker 在跑，sleep 後重試")
            time.sleep(poll_interval_sec)
            if once: return
            continue

        try:
            # has_running + acquire 同 transaction
            with mysql_conn() as conn:
                job = ClipJobRepo(conn).acquire_next_pending_atomic()

            if job is None:
                logger.debug("沒 pending job 或已有 running，sleep")
                time.sleep(poll_interval_sec)
                if once: return
                continue

            logger.info(
                "撈到 job_id=%s game_id=%s（retry=%s）",
                job["job_id"], job["game_id"], job["retry_count"],
            )

            # 設全局（給 _shutdown_handler 用）
            global _current_job_id, _current_game_id, _current_proc
            _current_job_id = job["job_id"]
            _current_game_id = job["game_id"]

            # log 寫檔不用 capture_output
            stdout_path = _LOG_DIR / f"clip_job_{job['job_id']}.stdout.log"
            stderr_path = _LOG_DIR / f"clip_job_{job['job_id']}.stderr.log"
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_f = open(stdout_path, "ab")
            stderr_f = open(stderr_path, "ab")

            # ────────────────────────────────────────────────────────────
            # D 策略 — 等 cut_at + 15 min 才開始 fetch
            #
            # 實測（KRX-BRO g1, 5/17）：cut_at +16 min RPGI 填上、+17 min timeline page 可拓。
            # <15 min 抓必白抓（Leaguepedia indexing 還沒），會浪費 API call。
            #
            # 流程：
            #   1) sleep 到 cut_at + 15 min（PRE_FETCH_WAIT）
            #   2) 每 10 min retry，直到 cut_at + 55 min（CAP_AFTER_CUT）
            #   3) 共 5 次 attempts: cut+15, +25, +35, +45, +55
            #   4) 拓到 → break；cap 達 → fallback 純 YOLO
            #   5) series_id NULL（多 series first run）→ 每 retry 重查，等 naming_finalizer link
            # ────────────────────────────────────────────────────────────
            PRE_FETCH_WAIT_SEC = 15 * 60   # cut_at + 15 min 才第一次 fetch
            CAP_AFTER_CUT_SEC  = 55 * 60   # cut_at + 55 min 為最終 deadline（5/21 拉長 35→55）
            RETRY_SEC          = 10 * 60   # 10 min between retries（5/21 拉長 5→10，給 Leaguepedia indexing 更多時間）

            try:
                with mysql_conn() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        "SELECT series_id, timeline_external_id, cut_at "
                        "FROM broadcast_games WHERE game_id=%s",
                        (job["game_id"],),
                    )
                    bg_row = cur.fetchone()

                should_wait = (
                    bg_row and not bg_row.get("timeline_external_id")
                )
                if not should_wait:
                    logger.info(
                        "[timeline] skip g%s (已 fetch ext=%s)",
                        job["game_id"], bg_row.get("timeline_external_id") if bg_row else None,
                    )
                elif not bg_row.get("cut_at"):
                    logger.warning(
                        "[timeline] g%s cut_at IS NULL（detecting / 異常）— skip timeline fetch",
                        job["game_id"],
                    )
                else:
                    from automation.services.timeline_anchor import fetch_timeline_metadata
                    cut_at: datetime = bg_row["cut_at"]   # naive UTC
                    first_fetch_at = cut_at + timedelta(seconds=PRE_FETCH_WAIT_SEC)
                    deadline = cut_at + timedelta(seconds=CAP_AFTER_CUT_SEC)

                    # Step 1: 等到 cut+15 min（除非已經過了）
                    # sleep 用 30s chunks，讓 signal handler 能即時中斷
                    # （signal handler 設 _shutting_down + sys.exit；本 loop 跳出讓 exit 傳遞）
                    now_utc = datetime.utcnow()
                    if now_utc < first_fetch_at:
                        wait_sec = (first_fetch_at - now_utc).total_seconds()
                        logger.info(
                            "[timeline] g%s cut_at=%s, 距 first_fetch (cut+15min) 還剩 %.0fs，先 sleep",
                            job["game_id"], cut_at, wait_sec,
                        )
                        chunk_remaining = wait_sec
                        while chunk_remaining > 0 and not _shutting_down:
                            chunk = min(30.0, chunk_remaining)
                            time.sleep(chunk)
                            chunk_remaining -= chunk
                        # signal handler 會 sys.exit；這只是讓 sleep 早點 unblock

                    # Step 2: fetch loop until cap
                    attempt = 0
                    timeline_done = False
                    while True:
                        attempt += 1
                        now_utc = datetime.utcnow()
                        remain_sec = (deadline - now_utc).total_seconds()
                        elapsed_min = (now_utc - cut_at).total_seconds() / 60

                        # 重查 series_id + ext_id
                        with mysql_conn() as _c:
                            _cur = _c.cursor()
                            _cur.execute(
                                "SELECT series_id, timeline_external_id "
                                "FROM broadcast_games WHERE game_id=%s",
                                (job["game_id"],),
                            )
                            cur_row = _cur.fetchone()

                        if cur_row and cur_row.get("timeline_external_id"):
                            logger.info(
                                "[timeline] g%s 已 fetch (ext=%s) — break",
                                job["game_id"], cur_row.get("timeline_external_id"),
                            )
                            timeline_done = True
                            break

                        # series_id 還沒 link → 不打 API，等 naming_finalizer
                        if not (cur_row and cur_row.get("series_id")):
                            if remain_sec <= 0:
                                logger.warning(
                                    "[timeline] g%s attempt %d (cut+%.0fmin): "
                                    "series_id 還 None 且超過 cap -> fallback YOLO",
                                    job["game_id"], attempt, elapsed_min,
                                )
                                break
                            logger.info(
                                "[timeline] g%s attempt %d (cut+%.0fmin): "
                                "series_id 還 None（等 naming_finalizer link，剩 %.0fs cap）",
                                job["game_id"], attempt, elapsed_min, remain_sec,
                            )
                            _interruptable_sleep(RETRY_SEC)
                            continue

                        # series 已 link → fetch timeline
                        logger.info(
                            "[timeline] g%s attempt %d (cut+%.0fmin, series=%s, 剩 %.0fs cap): fetch_metadata",
                            job["game_id"], attempt, elapsed_min,
                            cur_row.get("series_id"), remain_sec,
                        )
                        tl_result = fetch_timeline_metadata(job["game_id"])
                        action = tl_result.get("action")
                        if action == "ok":
                            logger.info(
                                "[timeline] g%s [OK] (cut+%.0fmin) ext=%s source=%s",
                                job["game_id"], elapsed_min,
                                tl_result.get("external_id"), tl_result.get("source"),
                            )
                            timeline_done = True
                            break

                        # no_timeline → 看 cap
                        if remain_sec <= 0:
                            logger.warning(
                                "[timeline] g%s attempt %d (cut+%.0fmin) %s — cap 達 -> fallback YOLO",
                                job["game_id"], attempt, elapsed_min, action,
                            )
                            break
                        logger.info(
                            "[timeline] g%s %s — sleep %ds retry (剩 %.0fs cap)",
                            job["game_id"], action, RETRY_SEC, remain_sec,
                        )
                        _interruptable_sleep(RETRY_SEC)

                    if not timeline_done:
                        logger.warning(
                            "[timeline] g%s D 策略 cap (cut+35min) 達 -> fallback 純 YOLO",
                            job["game_id"],
                        )
            except Exception:
                logger.exception("[timeline] 拓失敗（不影響 clip.py 主流程）")

            try:
                cmd = [
                    _PYTHON, str(_MAIN_PY),
                    "--from-game", str(job["game_id"]),
                ]
                logger.info("啟 main.py：%s", " ".join(cmd))
                # Popen + 寫 pid 到 DB（dashboard emergency kill 用）
                # 必須完整保留原 subprocess.run 的 timeout / returncode / stdout/stderr / 例外行為
                proc = subprocess.Popen(
                    cmd, stdout=stdout_f, stderr=stderr_f, env=env,
                )
                _current_proc = proc   # 5/18 給 _shutdown_handler 用
                # 立刻寫 pid → dashboard kill 時用 WHERE pid=X 精準 reset
                try:
                    with mysql_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE clip_jobs SET pid=%s WHERE job_id=%s",
                                (proc.pid, job["job_id"]),
                            )
                        conn.commit()
                except Exception:
                    logger.exception("寫 pid 失敗（不影響 main.py 跑）")
                # timeout config（補回原 subprocess.run 的 timeout 行為）
                try:
                    rc = proc.wait(timeout=3600 * timeout_h)
                except subprocess.TimeoutExpired:
                    proc.kill()       # 補回原 run 的 timeout 後 kill 行為
                    proc.wait()       # 確保 child 死乾淨
                    raise
                # 構造類似 CompletedProcess 介面，rc 邏輯下面不動
                class _R:
                    def __init__(self, rc): self.returncode = rc
                r = _R(rc)

                # 清 pid（main.py 跑完不管成功失敗都清）
                try:
                    with mysql_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE clip_jobs SET pid=NULL WHERE job_id=%s",
                                (job["job_id"],),
                            )
                        conn.commit()
                except Exception:
                    logger.exception("清 pid 失敗（不影響 mark_done/failed）")

                if r.returncode == 0:
                    # highlight 出爐後嘗試確認最終命名（refetch lolesports）
                    # 單 series broadcast → 直接清旗標；多 series → 嘗試 rename，失敗等 cron retry
                    try:
                        from automation.services.naming_finalizer import try_finalize_naming
                        finalized = try_finalize_naming(job["game_id"])
                        if not finalized:
                            logger.info("game %s 命名仍 provisional，cron 會 5 min 後重試",
                                        job["game_id"])
                    except Exception:
                        logger.exception("try_finalize_naming 例外（不影響 job mark_done）")

                    # inbox 觸發的 manual game → highlight 完成後自動刪 inbox 原檔釋出硬碟
                    try:
                        with mysql_conn() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    "SELECT g.game_path FROM broadcast_games g "
                                    "JOIN broadcasts b ON b.broadcast_id = g.broadcast_id "
                                    "WHERE g.game_id=%s "
                                    "  AND b.league_code='MANUAL' "
                                    "  AND g.start_source='manual'",
                                    (job["game_id"],)
                                )
                                row = cur.fetchone()
                        if row and row.get("game_path"):
                            from pathlib import Path as _Path
                            from highlight.utils import paths as _paths
                            mp4 = _Path(row["game_path"])
                            inbox = _paths.manual_inbox_dir().resolve()
                            try:
                                # 防呆：只刪 inbox 內檔，不刪其他位置的 game.mp4
                                if mp4.is_file() and inbox in mp4.resolve().parents:
                                    mp4.unlink()
                                    logger.info("[manual inbox] highlight 完成自動刪原檔：%s", mp4)
                            except OSError as e:
                                logger.warning("[manual inbox] 刪原檔失敗：%s", e)
                    except Exception:
                        logger.exception("[manual inbox] check / 刪原檔例外（不影響 mark_done）")

                    with mysql_conn() as conn:
                        ClipJobRepo(conn).mark_done(job["job_id"])
                    logger.info("[OK] job %s 完成", job["job_id"])
                else:
                    # 先 check emergency kill marker — dashboard 殺 main.py 時 touch
                    # 看到 marker → reset pending 不 mark_failed（讓恢復後 clip_worker 自動補撈）
                    emergency_flag = _LOG_DIR / f"emergency_kill_game_{job['game_id']}.flag"
                    if emergency_flag.exists():
                        try:
                            emergency_flag.unlink()
                        except OSError:
                            pass
                        try:
                            with mysql_conn() as conn:
                                with conn.cursor() as cur:
                                    cur.execute(
                                        "UPDATE clip_jobs SET status='pending', started_at=NULL, "
                                        "  pid=NULL, error_message='emergency_killed_by_dashboard' "
                                        "WHERE job_id=%s",
                                        (job["job_id"],),
                                    )
                                conn.commit()
                            logger.warning(
                                "[emergency kill] job %s reset 'pending' "
                                "（dashboard 殺的，下一輪自動重撈）", job["job_id"]
                            )
                        except Exception:
                            logger.exception("[emergency kill] reset pending 失敗")
                        # 跳過下面 mark_failed 邏輯
                        continue
                    # clip.py exit 2/3/4 是 fatal（鐵則違反），
                    # 不要 retry，直接 mark failed，下一輪自然撈下一 pending job（跨場隔離）。
                    err = _tail_file(stderr_path, 500)
                    if r.returncode == 2:
                        reason = f"FATAL_NO_BP（鐵則 R1：BP_UI 未偵測，本片放棄）: {err}"
                        logger.error("[X] job %s FATAL_NO_BP（不 retry，撈下一場）", job["job_id"])
                    elif r.returncode == 3:
                        reason = f"FATAL_NO_END（鐵則 R7：游戲結尾三層訊號皆失，本片放棄）: {err}"
                        logger.error("[X] job %s FATAL_NO_END（不 retry，撈下一場）", job["job_id"])
                    elif r.returncode == 4:
                        reason = f"FATAL_NO_NEXUS: {err}"
                        logger.error("[X] job %s FATAL_NO_NEXUS（不 retry，撈下一場）", job["job_id"])
                    else:
                        reason = f"main.py rc={r.returncode}: {err}"
                        logger.error("job %s 失敗 rc=%s", job["job_id"], r.returncode)
                    with mysql_conn() as conn:
                        ClipJobRepo(conn).mark_failed(job["job_id"], reason)
                        BroadcastGameRepo(conn).set_manual_skip(job["game_id"], reason)

            except subprocess.TimeoutExpired:
                tout_reason = f"main.py 超過 {timeout_h} 小時 timeout"
                with mysql_conn() as conn:
                    ClipJobRepo(conn).mark_failed(job["job_id"], tout_reason)
                    BroadcastGameRepo(conn).set_manual_skip(job["game_id"], tout_reason)
                logger.error("job %s timeout（%sh）", job["job_id"], timeout_h)
            except Exception as e:
                ex_reason = f"worker 例外: {type(e).__name__}: {e}"
                with mysql_conn() as conn:
                    ClipJobRepo(conn).mark_failed(job["job_id"], ex_reason)
                    BroadcastGameRepo(conn).set_manual_skip(job["game_id"], ex_reason)
                logger.exception("job %s 處理例外", job["job_id"])
            finally:
                # 保險再清一次 pid（timeout / 例外 case 沒走到正常 clear）
                try:
                    with mysql_conn() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE clip_jobs SET pid=NULL WHERE job_id=%s",
                                (job["job_id"],),
                            )
                        conn.commit()
                except Exception:
                    pass
                stdout_f.close()
                stderr_f.close()
                # 清全局（_shutdown_handler 看到空就不亂動）
                _current_proc = None
                _current_job_id = None
                _current_game_id = None

        finally:
            lock_stack.close()

        if once:
            return
    finally:
        hb.stop()


if __name__ == "__main__":
    run_worker()

"""Streamlink + ffmpeg pipe 錄影主流程。

設計（吸收 ChatGPT 四輪共 49 條評審）：
- streamlink stdout → ffmpeg segment muxer（不能用 streamlink --output part_%03d.ts，不支援）
- ffmpeg `-f segment -segment_time 600 -reset_timestamps 1`：每 10 分鐘一段 .ts
- 重啟用 `-segment_start_number` 接續編號（不覆蓋舊 .ts）
- stderr 寫 log file（不留 PIPE 避免 buffer 卡死）
- waiting_stream 期間有 heartbeat thread 持續更新
- min_valid_duration_sec 檢查（< 300s 直接 fail）
- watchdog 重試 ≤ 2 次後 raise，不 merge partial
- Ctrl+C 在 finally 清 lock + status='failed'
- recording_path = 合併後 mp4；raw_segments_dir = ts 目錄
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path
from subprocess import PIPE, Popen

from automation.db.connection import mysql_conn
from automation.db.repositories import (
    BroadcastSeriesRepo,
    BroadcastStateRepo,
    RecordingLockRepo,
)
from automation.infra.log_setup import setup_rotating_log
from automation.infra.config import load_config as _load_config
from automation.recorders import (
    disk_check,
    duration_check,
    stop_policy,
    stream_probe,
    ts_concat,
)
from automation.recorders.watchdog import Watchdog
from automation.infra.time_utils import utc_now

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = _PROJECT_ROOT / "_tmp" / "logs"

logger = setup_rotating_log("recorder", _LOG_DIR / "recorder.log")


class StreamNotAvailable(Exception): ...
class WatchdogFailed(Exception): ...
class DiskSpaceLow(Exception): ...
class TooShort(Exception): ...
class MergeFailed(Exception): ...


# ─────────────────────────────────────────────────────────────────────────────
class _HeartbeatThread(threading.Thread):
    """背景 thread 持續寫 last_heartbeat_at。"""

    def __init__(self, broadcast_id: int, interval_s: float = 30.0) -> None:
        super().__init__(daemon=True)
        self.broadcast_id = broadcast_id
        self.interval_s = interval_s
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                with mysql_conn() as conn:
                    BroadcastStateRepo(conn).update_heartbeat(self.broadcast_id)
            except Exception as e:
                logger.warning("heartbeat 更新失敗：%s", e)
            self._stop.wait(self.interval_s)

    def stop(self) -> None:
        self._stop.set()


# ─────────────────────────────────────────────────────────────────────────────
def _max_part_number(out_dir: Path, prefix: str) -> int:
    """掃 <prefix>_part_*.ts 找最大編號。"""
    files = list(out_dir.glob(f"{prefix}_part_*.ts"))
    if not files:
        return -1
    nums = []
    for f in files:
        try:
            n = int(f.stem.rsplit("_", 1)[-1])
            nums.append(n)
        except ValueError:
            pass
    return max(nums) if nums else -1


def _total_ts_size(raw_dir: Path, prefix: str) -> int:
    """目錄內所有 .ts 累積總大小（bytes）— 給「重啟後是否有新資料寫入」判定用。"""
    total = 0
    for p in raw_dir.glob(f"{prefix}_part_*.ts"):
        try:
            total += p.stat().st_size
        except OSError:
            pass
    return total


def _terminal_status(raw_dir: Path | None) -> str:
    """錄影結束時決定 broadcasts.recording_status_v2：

    - 完全沒產生有效 .ts → 'failed'（純災難，沒東西可救）
    - 有 ≥ 1 個 .ts → 'post_recording'（live_split 必須繼續掃完 cumulative 剩餘部分，
      掃完後 live_split 自己會把 status 改 'recorded'）

    背景：LCK 事故 — watchdog 5/5 重啟失敗 mark 'failed' → live_split 從 active list
    排除 → 41 min cumulative 沒掃 → KRX/GEN g2 整場丟失。新規則 'failed' 只用在純災難。
    """
    if raw_dir is None:
        return "failed"
    try:
        if not Path(raw_dir).is_dir():
            return "failed"
        ts_files = list(Path(raw_dir).glob("*_part_*.ts"))
        if not ts_files:
            return "failed"
        # 至少要有一個 .ts 大小 > 0（避免 0 byte 檔案誤判）
        if not any(f.stat().st_size > 0 for f in ts_files):
            return "failed"
        return "post_recording"
    except OSError:
        return "failed"


def _kill_process(proc: Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            proc.terminate()
        else:
            proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as e:
        logger.warning("kill_process 失敗：%s", e)


# ─────────────────────────────────────────────────────────────────────────────
def record_broadcast(
    broadcast_id: int,
    output_dir: Path,
    *,
    test_mode: bool = False,
) -> Path | None:
    """錄影主流程。

    Args:
        broadcast_id: broadcasts.broadcast_id
        output_dir  : 輸出目錄（會建立子目錄存 .ts + 合併 mp4）
        test_mode   : True 跳過 min_valid_duration 檢查（給短測 30 秒用）

    Returns: 合併後 mp4 路徑（成功時）；失敗 raise 對應 Exception
    """
    config = _load_config()
    rec_cfg = config.get("recording", {})
    min_valid = float(rec_cfg.get("min_valid_duration_sec", 300))
    pid = os.getpid()

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── 撈 broadcast row ───────────────────────────────────────────────
    with mysql_conn() as conn:
        repo = BroadcastStateRepo(conn)
        bs_repo = BroadcastSeriesRepo(conn)
        broadcast = repo.get_by_id(broadcast_id)
        if not broadcast:
            raise ValueError(f"broadcast_id={broadcast_id} 找不到")
        n_series = len(bs_repo.get_series_for_broadcast(broadcast_id))

    league = broadcast["league_code"]
    date_str = broadcast["broadcast_date"].strftime("%Y%m%d")
    platform = broadcast["platform"]
    short_id = broadcast["external_id"][:8]
    prefix = f"{league}_{date_str}_{platform}_{short_id}"
    stream_url = broadcast["stream_url"]

    if not stream_url:
        raise ValueError(f"broadcast {broadcast_id} 沒有 stream_url")

    # per-broadcast 子資料夾，避免多場錄影 .ts / cumulative.mp4 / cache.json 互相覆蓋
    raw_dir = output_dir / prefix
    raw_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("開始錄影 broadcast_id=%s", broadcast_id)
    logger.info("  league=%s  date=%s  platform=%s", league, date_str, platform)
    logger.info("  url=%s", stream_url)
    logger.info("  prefix=%s", prefix)
    logger.info("  raw_dir=%s", raw_dir)
    logger.info("=" * 60)

    sl_proc: Popen | None = None
    ff_proc: Popen | None = None
    sl_stderr_f = None
    ff_stderr_f = None
    heartbeat_thread: _HeartbeatThread | None = None

    # ── lock 帶 pid ─────────────────────────────────────────────
    with mysql_conn() as conn:
        lock_repo = RecordingLockRepo(conn)
        if not lock_repo.acquire(broadcast_id, pid=pid):
            logger.warning("broadcast %s 已有 lock，放棄錄影", broadcast_id)
            return None

    try:
        with mysql_conn() as conn:
            repo = BroadcastStateRepo(conn)
            # waiting_stream 也要 heartbeat
            repo.update_status(broadcast_id, "waiting_stream")
            repo.update_heartbeat(broadcast_id)

        heartbeat_thread = _HeartbeatThread(broadcast_id, interval_s=30)
        heartbeat_thread.start()

        # ── 輪詢 stream 可用 ───────────────────────────────
        if not stream_probe.wait_for_stream_available(
            stream_url, max_grace_min=20.0, poll_interval_s=30.0,
        ):
            raise StreamNotAvailable(f"stream {stream_url} 20 分鐘內仍不可用")

        with mysql_conn() as conn:
            repo = BroadcastStateRepo(conn)
            repo.update_status(broadcast_id, "recording")
            repo.set_recording_started_at(broadcast_id)
            # raw_segments_dir 在錄影開始就設好（不用等錄完），
            # 讓 dashboard / live_split_worker 即時看得到 .ts 累積進度
            # 用 as_posix 避免 Windows 反斜線在 SQL 路上被誤解
            repo.set_paths(broadcast_id, raw_segments_dir=raw_dir.resolve().as_posix())

        stop_at = stop_policy.compute_stop_time(
            broadcast, n_series=n_series,
            estimated_hours_per_series=float(rec_cfg.get("estimated_hours_per_series", 2.0)),
            extra_buffer_hours=float(rec_cfg.get("extra_buffer_hours", 1.0)),
            max_duration_hr=float(rec_cfg.get("max_duration_hr", 6.0)),
        )
        logger.info("錄影預定停止於 %s（UTC）", stop_at.isoformat())

        # stderr 寫 log file
        sl_stderr_path = _LOG_DIR / f"recorder_{broadcast_id}.streamlink.log"
        ff_stderr_path = _LOG_DIR / f"recorder_{broadcast_id}.ffmpeg.log"
        sl_stderr_f = open(sl_stderr_path, "ab")
        ff_stderr_f = open(ff_stderr_path, "ab")

        # max_restarts + recovery_grace 從 config 讀
        max_restarts = int(rec_cfg.get("max_restarts", 5))
        recovery_grace_sec = float(rec_cfg.get("restart_recovery_grace_sec", 30))

        restart_count = 0
        finished_by_stop_policy = False

        while True:
            start_part = _max_part_number(raw_dir, prefix) + 1
            logger.info("啟動 streamlink + ffmpeg pipe（segment_start=%d）", start_part)

            # （KRX/GEN g2 漏錄事故）：streamlink 內部 retry flags
            # 短抖動（DNS blip / YouTube 短暫 manifest 不可達 < 30s）由 streamlink 自己救，
            # 不靠外層 process 重 spawn 浪費時間 + 拿 stale "live event has ended"。
            # 配合策略 C：救不回時上層會 retry 1 次後進 monitor mode 守下一場 BP。
            sl_cmd = [
                sys.executable, "-m", "streamlink", "--stdout",
                "--retry-streams", "30",                  # 串流不可用 30 秒重試
                "--retry-max", "5",                       # 最多 5 次（2.5 min budget）
                "--hls-playlist-reload-attempts", "5",    # playlist reload 失敗 retry 5 次
                "--stream-segment-attempts", "3",         # segment 下載失敗 retry 3 次
                "--hls-live-restart",                     # 從 live edge 接續，不重抓歷史
                stream_url, "best",
            ]
            ff_cmd = [
                "ffmpeg", "-i", "pipe:0", "-c", "copy",
                # 90 秒一段 .ts（從 600 改），讓 live_split_worker 能更頻繁偵測
                "-f", "segment", "-segment_time", "90",
                "-reset_timestamps", "1",
                "-segment_start_number", str(start_part),
                # %03d 對 6hr / 90s = 240 段足夠（999 上限）
                str(raw_dir / f"{prefix}_part_%03d.ts"),
            ]
            sl_proc = Popen(sl_cmd, stdout=PIPE, stderr=sl_stderr_f)
            ff_proc = Popen(ff_cmd, stdin=sl_proc.stdout, stderr=ff_stderr_f)
            sl_proc.stdout.close()  # ffmpeg 接管

            wd = Watchdog(raw_dir, prefix, no_growth_timeout=180.0)

            # 紀錄重啟前的 total_size，inner loop 第一次 sleep 後比較
            pre_loop_total = _total_ts_size(raw_dir, prefix)
            recovery_check_done = False
            loop_started_at = time.time()

            inner_natural_end = False
            while ff_proc.poll() is None:
                time.sleep(30)

                # 重啟後 grace 內看 total_size 是否成長
                # → reset restart_count（這次重啟成功，不算 fatal）
                if not recovery_check_done and restart_count > 0 \
                        and time.time() - loop_started_at >= recovery_grace_sec:
                    current_total = _total_ts_size(raw_dir, prefix)
                    if current_total > pre_loop_total + 100 * 1024:  # > 100 KB
                        logger.info(
                            "重啟後 %.0fs 內 total_size 從 %d → %d bytes，"
                            "視為 recovered → reset restart_count",
                            recovery_grace_sec, pre_loop_total, current_total,
                        )
                        restart_count = 0
                    recovery_check_done = True

                # 到停止時間
                if utc_now() >= stop_at:
                    finished_by_stop_policy = True
                    logger.info("到達 stop_at，停止錄影")
                    _kill_process(ff_proc)
                    _kill_process(sl_proc)
                    break

                # 磁碟空間
                if not disk_check.check_disk_space(output_dir, min_gb=5.0):
                    raise DiskSpaceLow(
                        f"剩餘磁碟 {disk_check.free_gb(output_dir):.1f} GB < 5 GB"
                    )

                # watchdog
                if not wd.healthy(sl_proc, ff_proc):
                    logger.warning("watchdog 不健康：%s", wd.diagnosis)
                    _kill_process(ff_proc)
                    _kill_process(sl_proc)
                    break
            else:
                # ff_proc 自然結束
                inner_natural_end = True

            # finished_by_stop_policy 或自然結束 → 結束
            if finished_by_stop_policy or inner_natural_end:
                break

            # watchdog 失敗 → 試重啟
            restart_count += 1
            if restart_count > max_restarts:
                # 評審 B + λ：超過重試 raise，不 merge partial
                raise WatchdogFailed(
                    f"failed_watchdog_ts_preserved (restart {max_restarts} 次後 "
                    f"diagnosis={wd.diagnosis})"
                )
            logger.warning("watchdog 失敗 → 第 %d 次重啟（max=%d）", restart_count, max_restarts)

            # 紀錄中斷區間 → 階段 3 cron 結束後從 archive VOD 自動補錄該段
            try:
                last_part = _max_part_number(raw_dir, prefix)  # 最後一個 .ts segment 號
                # broadcast offset：每段 90s（segment_time=90），part 是 0-indexed
                interrupt_offset = (last_part + 1) * 90.0
                resume_offset = interrupt_offset + 60.0  # 保守估 1 min 中斷
                with mysql_conn() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO recording_interruptions "
                            "(broadcast_id, interrupted_at_sec, resumed_at_sec, duration_sec, recovered) "
                            "VALUES (%s, %s, %s, %s, 0)",
                            (broadcast_id, interrupt_offset, resume_offset, 60.0),
                        )
                    conn.commit()
                logger.info("[interruption] 已記錄 broadcast %s 中斷 @ offset %.0fs（recover cron 之後會處理）",
                            broadcast_id, interrupt_offset)
            except Exception:
                logger.exception("[interruption] 紀錄失敗（不影響重啟流程）")

            time.sleep(2)

        # ── 合併 ────────────────────────────────────────────────────
        with mysql_conn() as conn:
            repo = BroadcastStateRepo(conn)
            repo.update_status(broadcast_id, "merging")

        ts_files = sorted(output_dir.glob(f"{prefix}_part_*.ts"))
        # 空檢查由 ts_concat raise NoSegmentsRecorded
        mp4_path = ts_concat.concat_to_mp4(
            ts_files, output_dir / f"{prefix}.mp4",
        )

        # min_valid_duration 檢查
        if not test_mode:
            valid, duration = duration_check.is_valid_recording(mp4_path, min_seconds=min_valid)
            if not valid:
                raise TooShort(
                    f"錄影時長 {duration:.0f}s < min_valid {min_valid:.0f}s"
                )
            logger.info("錄影 duration 驗證通過：%.0fs", duration)
        else:
            logger.info("test_mode=True，跳過 min_valid_duration 檢查")

        # ── 終態 ────────────────────────────────────────────────────
        with mysql_conn() as conn:
            repo = BroadcastStateRepo(conn)
            repo.set_paths(
                broadcast_id,
                recording_path=str(mp4_path.resolve()),
                raw_segments_dir=str(output_dir.resolve()),
            )
            repo.set_recording_ended_at(broadcast_id)
            repo.update_status(broadcast_id, "recorded")

            # 不再由 recorder 直接 enqueue clip_jobs。
            # live_split_worker 在每場 game 切完後才 enqueue 該 game_id（game-level）。
            # 錄影中 worker 會偵測到 BP/結束訊號邊錄邊切。
            # 錄影結束後 worker 看到 recording_status_v2='recorded' 會跑最後一輪確保切完。

        logger.info("[OK] 錄影成功：%s", mp4_path)
        return mp4_path

    except KeyboardInterrupt:
        logger.warning("Ctrl+C 中斷，保留 .ts 段")
        with mysql_conn() as conn:
            BroadcastStateRepo(conn).update_status(
                broadcast_id, _terminal_status(raw_dir),
                error_message="interrupted_by_user_ts_preserved",
            )
        raise
    except (StreamNotAvailable, WatchdogFailed, DiskSpaceLow, TooShort,
            MergeFailed, ts_concat.NoSegmentsRecorded, ts_concat.MergeFailed) as e:
        logger.error("錄影失敗（%s）：%s", type(e).__name__, e)
        with mysql_conn() as conn:
            BroadcastStateRepo(conn).update_status(
                broadcast_id, _terminal_status(raw_dir),
                error_message=f"{type(e).__name__}: {str(e)[:400]}",
            )
        raise
    except Exception as e:
        logger.exception("錄影異常")
        with mysql_conn() as conn:
            BroadcastStateRepo(conn).update_status(
                broadcast_id, _terminal_status(raw_dir),
                error_message=f"{type(e).__name__}: {str(e)[:400]}",
            )
        raise
    finally:
        if heartbeat_thread is not None:
            heartbeat_thread.stop()
        _kill_process(ff_proc)
        _kill_process(sl_proc)
        with suppress(Exception):
            if sl_stderr_f: sl_stderr_f.close()
        with suppress(Exception):
            if ff_stderr_f: ff_stderr_f.close()
        # release 帶 pid 防誤刪
        with mysql_conn() as conn:
            RecordingLockRepo(conn).release(broadcast_id, pid=pid)

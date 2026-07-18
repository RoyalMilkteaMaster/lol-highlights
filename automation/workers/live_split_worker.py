"""live_split_worker：邊錄邊切（狀態機重寫）。

狀態機（取代原本「不管狀態都掃 4 訊號」的浪費）：
  IDLE          → 只掃 bp_ui (stride=7.5)
                  bp_ui 連續命中 ≥ bp_confirm_duration_sec → BP_PHASE
  BP_PHASE      → 不掃任何（BP 階段 + 早期 game 不可能結束）
                  anchor + bp_phase_min_duration_sec → SEARCH_END
  SEARCH_END    → 掃 end_graph + nexus + game_end_screen (stride=7.5)
                  超過 search_end_overdue_sec 後加掃 bp_ui (stride=15) 當 fallback
                  end signals confirmed (hit grouping max_gap=15s + duration ≥ 15s)
                  → 切片 → COOLDOWN
                  bp_ui confirmed (45min+ fallback) → 用 first_bp - 60s 切，IDLE 找新場
  COOLDOWN      → 不掃任何（場間休息）
                  anchor + cooldown_duration_sec → IDLE, current_game_index += 1

GPU 用量降 60-70%（IDLE/BP_PHASE/COOLDOWN 階段省下大量掃描），取代 pause_when_clip_running。

# MIRROR FROM detectors/yolo_detector.py 的 BP 聚合 / merge_gap 邏輯不再 mirror —
# 狀態機隱含「進入 BP_PHASE 已確認 BP 連續 30s 命中」=既有 min_bp_duration / merge_gap 的等價實作。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from automation.db.connection import mysql_conn
from automation.db.repositories import (
    BroadcastGameRepo,
    ClipJobRepo,
    DetectorStateRepo,
    WorkerLockRepo,
)
from automation.infra.heartbeat import HeartbeatThread
from automation.infra.control import automation_status
from automation.infra.config import load_config as _load_config
from automation.infra.log_setup import setup_rotating_log
from automation.recorders import ffmpeg_utils

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = _PROJECT_ROOT / "_tmp" / "logs"

logger = setup_rotating_log("live_split_worker", _LOG_DIR / "live_split_worker.log")


# ── 預設參數 ─────────────────────────────────────────────────────────────
DEFAULT_POLL_INTERVAL_SEC   = 300.0
DEFAULT_MIN_TS_DURATION_SEC = 720.0
DEFAULT_MIN_NEW_RANGE_SEC   = 60.0
DEFAULT_TS_STABLE_AGE_SEC   = 2.0
DEFAULT_STRIDE_SEC          = 7.5
DEFAULT_LEAD_IN_SEC         = 30.0
# DEFAULT_GAMES_OUTPUT_ROOT 改 lazy 解析（在 __init__ + load_config 內呼叫 paths）
# 不在這裡寫死，因為 .env 可能還沒 load_dotenv
DEFAULT_GAMES_OUTPUT_ROOT   = None
DEFAULT_DEVICE              = None

# 狀態機參數
DEFAULT_BP_CONFIRM_SEC      = 30.0
DEFAULT_BP_PHASE_MIN_SEC    = 1200.0   # 20 min
DEFAULT_SEARCH_END_OVERDUE  = 2700.0   # 45 min
DEFAULT_END_CONFIRM_SEC     = 15.0
DEFAULT_COOLDOWN_SEC        = 600.0    # 10 min
DEFAULT_END_BUFFER_SEC      = 15.0


# ─────────────────────────────────────────────────────────────────────────────
# Cache helpers（atomic write，從原版保留）
# ─────────────────────────────────────────────────────────────────────────────
def _save_cache_atomic(cache_path: Path, cache: dict) -> None:
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, cache_path)


def _load_cache(cache_path: Path, broadcast_id: int) -> dict:
    if not cache_path.is_file():
        return _empty_cache(broadcast_id)
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        events = data.setdefault("events", {})
        for k in ("bp_ui", "game_end_screen", "nexus_explosion", "end_graph"):
            events.setdefault(k, [])
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("cache 損毀（%s），重置：%s", e, cache_path)
        return _empty_cache(broadcast_id)


def _empty_cache(broadcast_id: int) -> dict:
    return {
        "broadcast_id": broadcast_id,
        "scanned_until_offset_sec": 0.0,
        "events": {"bp_ui": [], "game_end_screen": [], "nexus_explosion": [], "end_graph": []},
        "last_updated_at": None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Hit grouping helper（GPT review 5 採納）
# ─────────────────────────────────────────────────────────────────────────────
def _confirmed_signal_offset(
    hits, *, min_duration: float, max_gap: float, min_offset: float = 0.0
) -> float | None:
    """把 sorted hits 用 max_gap 分組，找「整組 (last - first) >= min_duration」的 confirmed group，
    回傳「最晚 confirmed group 的 last hit offset」；無則 None。

    （跨場隔離）：加 min_offset 參數先過濾舊紀錄。
    上一場切完進 COOLDOWN→IDLE 後，雖然 cache.events 會清，但若呼叫端傳 min_offset
    （= scan_from 或 anchor），仍多一道保險避免拿到 anchor 之前的舊 hit。

    例：[100, 105, 110, 115, 200, 205] with max_gap=15, min_duration=15
        → groups: [[100,105,110,115], [200,205]]
        → first 持續 15s 通過、second 持續 5s 不過
        → confirmed: [100,105,110,115] → 回 115

    防誤判單一 frame 命中即觸發切片。
    """
    if not hits:
        return None
    sorted_h = sorted(set(t for t in hits if t >= min_offset))
    if not sorted_h:
        return None
    groups = []
    cur = [sorted_h[0]]
    for t in sorted_h[1:]:
        if t - cur[-1] <= max_gap:
            cur.append(t)
        else:
            groups.append(cur)
            cur = [t]
    groups.append(cur)
    confirmed = [g for g in groups if g[-1] - g[0] >= min_duration]
    return max(g[-1] for g in confirmed) if confirmed else None


def _confirmed_group_bounds(
    hits, *, min_duration: float, max_gap: float, min_offset: float = 0.0
) -> tuple[float, float] | None:
    """同 _confirmed_signal_offset，但回傳「最晚 confirmed group 的 (first_hit, last_hit)」。

    用於 IDLE→BP_PHASE：anchor 要落在 BP 起點（first_hit），不能用 last_hit-30s
    （BP 通常持續 300-400s，last-30 會把 anchor 推到 BP 末段，剪出的 game.mp4
    BP 段太短 → main.py FATAL_NO_BP）。
    """
    if not hits:
        return None
    sorted_h = sorted(set(t for t in hits if t >= min_offset))
    if not sorted_h:
        return None
    groups = []
    cur = [sorted_h[0]]
    for t in sorted_h[1:]:
        if t - cur[-1] <= max_gap:
            cur.append(t)
        else:
            groups.append(cur)
            cur = [t]
    groups.append(cur)
    confirmed = [g for g in groups if g[-1] - g[0] >= min_duration]
    if not confirmed:
        return None
    latest = max(confirmed, key=lambda g: g[-1])
    return (latest[0], latest[-1])


# ─────────────────────────────────────────────────────────────────────────────
# Filesystem helpers
# ─────────────────────────────────────────────────────────────────────────────
def _find_stable_ts(raw_dir: Path, prefix: str, *, min_age_sec: float) -> list[Path]:
    """raw_dir 內 mtime ≥ min_age_sec 的 .ts，按 part 編號排序。"""
    now = time.time()
    out: list[Path] = []
    for f in sorted(raw_dir.glob(f"{prefix}_part_*.ts")):
        try:
            age = now - f.stat().st_mtime
        except OSError:
            continue
        if age >= min_age_sec:
            out.append(f)
    return out


def _ensure_cumulative_fresh(
    cumulative_path: Path, ts_files: list[Path], required_end_sec: float
) -> None:
    needs_reconcat = True
    if cumulative_path.is_file():
        try:
            dur = ffmpeg_utils.ffprobe_duration(cumulative_path)
            if dur >= required_end_sec - 1.0:
                needs_reconcat = False
        except ffmpeg_utils.FFmpegError:
            needs_reconcat = True
    if needs_reconcat:
        logger.info("重新 concat cumulative.mp4（need >= %.0fs）", required_end_sec)
        ffmpeg_utils.concat_to_mp4(ts_files, cumulative_path)


# ─────────────────────────────────────────────────────────────────────────────
# LiveSplitWorker
# ─────────────────────────────────────────────────────────────────────────────
class LiveSplitWorker:
    """主迴圈包裝（狀態機版）。"""

    def __init__(
        self,
        *,
        poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC,
        min_ts_duration_sec: float = DEFAULT_MIN_TS_DURATION_SEC,
        min_new_range_sec: float = DEFAULT_MIN_NEW_RANGE_SEC,
        ts_stable_age_sec: float = DEFAULT_TS_STABLE_AGE_SEC,
        stride_sec: float = DEFAULT_STRIDE_SEC,
        lead_in_sec: float = DEFAULT_LEAD_IN_SEC,
        games_output_root: Path | None = None,  # None → lazy resolve via highlight.utils.paths in __init__
        device: int | str | None = DEFAULT_DEVICE,
        # 狀態機參數
        bp_confirm_sec: float = DEFAULT_BP_CONFIRM_SEC,
        bp_phase_min_sec: float = DEFAULT_BP_PHASE_MIN_SEC,
        search_end_overdue_sec: float = DEFAULT_SEARCH_END_OVERDUE,
        end_confirm_sec: float = DEFAULT_END_CONFIRM_SEC,
        cooldown_sec: float = DEFAULT_COOLDOWN_SEC,
        end_buffer_sec: float = DEFAULT_END_BUFFER_SEC,
        dry_run: bool = False,
        no_cut: bool = False,
        no_enqueue: bool = False,
    ) -> None:
        self.poll_interval_sec   = poll_interval_sec
        self.min_ts_duration_sec = min_ts_duration_sec
        self.min_new_range_sec   = min_new_range_sec
        self.ts_stable_age_sec   = ts_stable_age_sec
        self.stride_sec          = stride_sec
        self.lead_in_sec         = lead_in_sec
        if games_output_root is None:
            # 跨機器搬遷支援 — 沒指定就讀 highlight.utils.paths（VIDEO_DIR env override）
            from highlight.utils import paths as _paths
            games_output_root = _paths.lol_games_vods_dir()
        self.games_output_root   = Path(games_output_root)
        self.device              = device
        self.bp_confirm_sec      = bp_confirm_sec
        self.bp_phase_min_sec    = bp_phase_min_sec
        self.search_end_overdue_sec = search_end_overdue_sec
        self.end_confirm_sec     = end_confirm_sec
        self.cooldown_sec        = cooldown_sec
        self.end_buffer_sec      = end_buffer_sec
        self.dry_run             = dry_run
        self.no_cut              = no_cut
        self.no_enqueue          = no_enqueue
        # 模型 lazy load
        self._lol_det_cache: dict = {}
        self._eg_det = None
        self._eg_det_dense = None  # 第二輪細掃版（低 conf + 密 sample）
        self._duration_cache: dict[Path, tuple[int, int, float]] = {}

    def _duration_for(self, path: Path) -> float:
        """Probe a segment once, then reuse it until size or mtime changes."""
        stat = path.stat()
        cached = self._duration_cache.get(path)
        fingerprint = (stat.st_size, stat.st_mtime_ns)
        if cached and cached[:2] == fingerprint:
            return cached[2]
        duration = ffmpeg_utils.ffprobe_duration(path)
        self._duration_cache[path] = (*fingerprint, duration)
        return duration

    # ── 模型 lazy load ───────────────────────────────────────────────────
    def _get_lol_detector(self, video_path: Path):
        from highlight import create_yolo_detector
        key = str(Path(video_path).resolve())
        det = self._lol_det_cache.get(key)
        if det is None:
            det = create_yolo_detector(video_path, device=self.device)
            self._lol_det_cache[key] = det
        return det

    def _get_end_graph_detector(self):
        if self._eg_det is None:
            from highlight import create_end_graph_detector
            self._eg_det = create_end_graph_detector(
                sample_interval=self.stride_sec,
                persist_required=1,
                device=self.device if self.device is not None else 0,
            )
        return self._eg_det

    def _get_end_graph_detector_dense(self):
        """第二輪細掃版 — sample_interval=1.0 + conf=0.20 更敏感。
        只在 standard 第一輪（stride=7.5, conf=0.35）抓不到 confirmed_end 且
        broadcast 已 post_recording（5 min 沒新 .ts）時觸發。GPU 用量高，省著用。
        """
        if self._eg_det_dense is None:
            from highlight import create_end_graph_detector
            self._eg_det_dense = create_end_graph_detector(
                conf=0.20,                  # 從 0.35 → 0.20 更敏感
                sample_interval=1.0,        # 從 7.5 → 1.0 更密
                persist_required=2,
                device=self.device if self.device is not None else 0,
            )
        return self._eg_det_dense

    # ── 主迴圈 ────────────────────────────────────────────────────────────
    def run(self, *, once: bool = False) -> None:
        logger.info(
            "live_split_worker 啟動（狀態機版，poll=%.0fs, stride=%.1fs, "
            "bp_confirm=%.0fs, bp_phase_min=%.0fs, search_end_overdue=%.0fs, "
            "end_confirm=%.0fs, cooldown=%.0fs）",
            self.poll_interval_sec, self.stride_sec, self.bp_confirm_sec,
            self.bp_phase_min_sec, self.search_end_overdue_sec,
            self.end_confirm_sec, self.cooldown_sec,
        )
        hb = HeartbeatThread("live_split_worker")
        hb.start()
        _was_paused = False  # 只在 paused→resumed transition 印 log
        try:
            while True:
                decision = automation_status()
                if not decision["allowed"]:
                    if not _was_paused:
                        logger.info(
                            "live_split paused: source=%s reason=%s next_change=%s",
                            decision.get("source"),
                            decision.get("reason"),
                            decision.get("next_change_at") or "none",
                        )
                        _was_paused = True
                    if once:
                        return
                    time.sleep(self.poll_interval_sec)
                    continue
                elif _was_paused:
                    logger.info("live_split resumed by automation policy")
                    _was_paused = False
                try:
                    self._tick()
                except Exception:
                    logger.exception("live_split_worker tick 例外")
                if once:
                    return
                time.sleep(self.poll_interval_sec)
        finally:
            hb.stop()

    def _tick(self) -> None:
        with mysql_conn() as conn:
            wl = WorkerLockRepo(conn)
            if not wl.acquire("live_split_worker", timeout_sec=0):
                logger.debug("已有另一個 live_split_worker 在跑，等下一輪")
                return
            try:
                broadcasts = DetectorStateRepo(conn).find_active_broadcasts()
                logger.debug("active broadcasts: %d", len(broadcasts))
                for b in broadcasts:
                    try:
                        self._tick_one_broadcast(conn, b)
                    except Exception:
                        logger.exception("broadcast %s 處理例外", b["broadcast_id"])
                        try:
                            DetectorStateRepo(conn).set_failed(
                                b["broadcast_id"], "tick exception",
                            )
                        except Exception:
                            pass
            finally:
                wl.release("live_split_worker")

    # ── 單 broadcast 狀態機驅動 ─────────────────────────────────────────
    def _tick_one_broadcast(self, conn, broadcast: dict) -> None:
        bid = broadcast["broadcast_id"]
        raw_dir = Path(broadcast["raw_segments_dir"])
        if not raw_dir.is_dir():
            message = f"raw_segments_dir missing: {raw_dir}"
            DetectorStateRepo(conn).set_failed(bid, message)
            logger.warning("broadcast %s %s; detector marked failed", bid, message)
            return

        prefix = self._infer_prefix(broadcast, raw_dir)
        if prefix is None:
            return

        ts_files = _find_stable_ts(raw_dir, prefix, min_age_sec=self.ts_stable_age_sec)
        if not ts_files:
            return

        try:
            durations = [self._duration_for(t) for t in ts_files]
        except ffmpeg_utils.FFmpegError as e:
            logger.warning("broadcast %s ffprobe 失敗：%s", bid, e)
            return
        latest_offset = sum(durations)

        if latest_offset < self.min_ts_duration_sec:
            logger.info("broadcast %s 累積僅 %.0fs < min_ts，跳過", bid, latest_offset)
            return

        # 載入 / 初始化 detector_state
        state_repo = DetectorStateRepo(conn)
        state = state_repo.get_or_init(bid)
        phase = state["detector_phase"]
        anchor = float(state.get("phase_anchor_offset_sec") or 0.0)

        logger.info(
            "broadcast %s phase=%s anchor=%.0fs latest=%.0fs game_index=%d",
            bid, phase, anchor, latest_offset, state.get("current_game_index", 1),
        )

        # BP_PHASE 卡死 → 強制進 SEARCH_END（end_graph 必抓邏輯，user 鐵則）
        # BRO/NS g2 case：live_split 偵測到 g2 BP 進 BP_PHASE，但 broadcast 端
        # 20:09 真結束（LCK 關直播），後續沒新 .ts → cumulative 永遠不長到 anchor+1200s
        # → live_split 永遠卡 BP_PHASE。修法：post_recording + .ts 5 min 沒新增 → 強制
        # refresh cumulative + 強制進 SEARCH_END，讓 _scan_search_end 跑第一輪 standard 掃
        # + 第二輪細掃 (stride=1.0) 試圖抓 end_graph。
        # 「不能沒抓到 end_graph 就強制切」（user 鐵則）— 細掃還抓不到 → mark game failed。
        if (phase == "bp_phase"
                and broadcast.get("recording_status_v2") == "post_recording"
                and ts_files):
            latest_mtime = max(t.stat().st_mtime for t in ts_files)
            age_min = (time.time() - latest_mtime) / 60
            if age_min >= 5.0:
                cumulative = raw_dir / "_cumulative.mp4"
                _ensure_cumulative_fresh(cumulative, ts_files, latest_offset)
                logger.warning(
                    "broadcast %s BP_PHASE 卡死：post_recording + .ts %.1fmin 沒更新 "
                    "-> 強制進 SEARCH_END (anchor=%.0fs unchanged) 讓細掃 end_graph",
                    bid, age_min, anchor,
                )
                DetectorStateRepo(conn).update_phase(bid, "search_end", anchor)
                phase = "search_end"  # fall through 到下面 _scan_and_decide

        # ── 狀態機路由 ──
        if phase == "bp_phase":
            self._handle_bp_phase(conn, bid, anchor, latest_offset)
            return
        if phase == "cooldown":
            self._handle_cooldown(conn, bid, anchor, latest_offset, raw_dir)
            return

        # phase == 'idle' 或 'search_end' → 真的要掃
        self._scan_and_decide(conn, broadcast, prefix, raw_dir, ts_files,
                              phase, anchor, latest_offset, state)

        # finalization — 若 broadcast 是 post_recording 且 cumulative 已掃完 +
        # 沒新 .ts 進來，自動 mark 'recorded'。讓 watchdog 不需要等 user 介入。
        self._maybe_finalize_post_recording(conn, broadcast, raw_dir, ts_files,
                                             latest_offset)

    def _maybe_finalize_post_recording(self, conn, broadcast: dict,
                                        raw_dir: Path, ts_files: list[Path],
                                        latest_offset: float) -> None:
        """若 broadcast.status='post_recording' AND 已掃完 AND 無新 .ts → mark 'recorded'。

        條件：
        1. recording_status_v2 == 'post_recording'
        2. detector_state.last_scan_until_sec 已 ≥ latest_offset - tolerance
        3. 最新 .ts 的 mtime 距今 ≥ no_new_ts_min（預設 5 min，確保 streamlink/ffmpeg 真的不會再寫）
        """
        if broadcast.get("recording_status_v2") != "post_recording":
            return
        bid = broadcast["broadcast_id"]
        state = DetectorStateRepo(conn).get_or_init(bid)
        last_scan = float(state.get("last_scan_until_sec") or 0.0)
        # 容忍 5s gap（最新 .ts 可能還沒被 ffprobe 出完整 duration）
        if last_scan < latest_offset - 5.0:
            return
        # 最新 .ts 的 mtime 必須舊於 no_new_ts_min（確認 streamlink 真的死了不會再寫）
        no_new_ts_min = 5.0
        latest_mtime = max((t.stat().st_mtime for t in ts_files), default=0)
        age_min = (time.time() - latest_mtime) / 60
        if age_min < no_new_ts_min:
            logger.info(
                "broadcast %s post_recording 已掃完但最新 .ts 才 %.1fmin -> 等下輪",
                bid, age_min,
            )
            return
        # 通通滿足 → finalize
        from automation.db.repositories import BroadcastStateRepo
        BroadcastStateRepo(conn).update_status(bid, "recorded")
        # set recording_ended_at（如果還沒設）
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcasts SET recording_ended_at=COALESCE(recording_ended_at, NOW()) "
                "WHERE broadcast_id=%s",
                (bid,),
            )
        conn.commit()
        logger.info(
            "[OK] broadcast %s post_recording -> recorded（cumulative 掃完 %.0fs，最新 .ts %.1fmin 沒更新）",
            bid, last_scan, age_min,
        )

    def _handle_bp_phase(self, conn, bid: int, anchor: float,
                          latest_offset: float) -> None:
        if latest_offset >= anchor + self.bp_phase_min_sec:
            # 進 SEARCH_END，anchor = bp_phase 結束時的 offset
            new_anchor = anchor + self.bp_phase_min_sec
            DetectorStateRepo(conn).update_phase(bid, "search_end", new_anchor)
            logger.info("broadcast %s phase IDLE->BP_PHASE 過 %.0fs -> SEARCH_END", bid, self.bp_phase_min_sec)

    def _handle_cooldown(self, conn, bid: int, anchor: float,
                          latest_offset: float, raw_dir: Path) -> None:
        """COOLDOWN → IDLE 切換點。

        清 cache.events 全部，避免上一場的 hits 污染下一場偵測。
        保留 scanned_until_offset_sec，避免重新掃描浪費 GPU。
        """
        if latest_offset >= anchor + self.cooldown_sec:
            # 清 cache.events（核心隔離點）
            cache_path = raw_dir / "_yolo_events_cache.json"
            cache = _load_cache(cache_path, bid)
            cache["events"] = {
                "bp_ui": [],
                "game_end_screen": [],
                "nexus_explosion": [],
                "end_graph": [],
            }
            cache["last_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _save_cache_atomic(cache_path, cache)
            logger.info("broadcast %s COOLDOWN->IDLE：清 cache.events 全部（跨場隔離）", bid)

            DetectorStateRepo(conn).update_phase(
                bid, "idle", latest_offset, increment_game_index=True,
            )
            logger.info("broadcast %s COOLDOWN %.0fs 過 -> IDLE 找下一場", bid, self.cooldown_sec)

    # ── 真正掃描 + 狀態轉移判斷 ─────────────────────────────────────────
    def _scan_and_decide(
        self, conn, broadcast: dict, prefix: str, raw_dir: Path,
        ts_files: list[Path], phase: str, anchor: float, latest_offset: float,
        state: dict,
    ) -> None:
        bid = broadcast["broadcast_id"]
        cache_path = raw_dir / "_yolo_events_cache.json"
        cache = _load_cache(cache_path, bid)
        # scan_from：max(cache 已掃, anchor)
        scan_from = max(float(cache.get("scanned_until_offset_sec", 0.0)), anchor)
        new_range = latest_offset - scan_from
        if new_range < self.min_new_range_sec:
            logger.info("broadcast %s 新範圍 %.0fs < %.0fs，等", bid, new_range, self.min_new_range_sec)
            return

        cumulative = raw_dir / "_cumulative.mp4"
        _ensure_cumulative_fresh(cumulative, ts_files, latest_offset)

        if phase == "idle":
            self._scan_idle(conn, broadcast, cumulative, cache, cache_path,
                            scan_from, latest_offset, anchor)
        else:
            # search_end
            self._scan_search_end(conn, broadcast, prefix, raw_dir, ts_files,
                                  cumulative, cache, cache_path,
                                  scan_from, latest_offset, anchor, state)

    def _scan_idle(self, conn, broadcast: dict, cumulative: Path, cache: dict,
                    cache_path: Path, scan_from: float, latest_offset: float,
                    anchor: float) -> None:
        """IDLE：只掃 bp_ui，confirmed → BP_PHASE。

        confirmed 判斷時用 min_offset=anchor 過濾，
        確保不會撿到上一場 cooldown 之前的 BP hits（雖然 COOLDOWN→IDLE 應已清 cache.events，
        但這裡多一道保險）。
        """
        bid = broadcast["broadcast_id"]
        logger.info("broadcast %s IDLE 掃描 bp_ui [%.0f, %.0f]", bid, scan_from, latest_offset)
        det = self._get_lol_detector(cumulative)
        new_hits = det._detect_multi_classes(["bp_ui"], scan_from, latest_offset, self.stride_sec)

        cache["events"]["bp_ui"].extend(new_hits.get("bp_ui", []))
        cache["events"]["bp_ui"] = sorted(set(cache["events"]["bp_ui"]))
        cache["scanned_until_offset_sec"] = latest_offset
        cache["last_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_cache_atomic(cache_path, cache)

        # 從 cache 全部 bp_ui 找 confirmed group（過濾 < anchor 的舊紀錄）
        bounds = _confirmed_group_bounds(
            cache["events"]["bp_ui"],
            min_duration=self.bp_confirm_sec, max_gap=15.0,
            min_offset=anchor,
        )
        if bounds is not None:
            # anchor = BP 起點（first hit of confirmed group），剪出的 game.mp4 完整含 BP
            new_anchor = bounds[0]
            DetectorStateRepo(conn).update_phase(bid, "bp_phase", new_anchor,
                                                  last_scan_until_sec=latest_offset)
            logger.info("broadcast %s IDLE -> BP_PHASE @ anchor=%.0fs (BP first_hit, last_hit=%.0fs)",
                        bid, new_anchor, bounds[1])

    def _scan_search_end(
        self, conn, broadcast: dict, prefix: str, raw_dir: Path,
        ts_files: list[Path], cumulative: Path, cache: dict, cache_path: Path,
        scan_from: float, latest_offset: float, anchor: float, state: dict,
    ) -> None:
        """SEARCH_END：掃 end_graph + nexus + game_end_screen；超過 45 min 加掃 bp_ui fallback。"""
        bid = broadcast["broadcast_id"]
        time_in_phase = latest_offset - anchor
        logger.info(
            "broadcast %s SEARCH_END 掃 end signals [%.0f, %.0f] (in phase %.0fs)",
            bid, scan_from, latest_offset, time_in_phase,
        )

        # 掃 lol_detector 三類 + end_graph
        det = self._get_lol_detector(cumulative)
        lol_hits = det._detect_multi_classes(
            ["game_end_screen", "nexus_explosion"], scan_from, latest_offset, self.stride_sec,
        )
        eg_det = self._get_end_graph_detector()
        eg_result = eg_det.detect(cumulative, scan_start=scan_from, scan_end=latest_offset)

        for cls, ts_list in lol_hits.items():
            cache["events"].setdefault(cls, []).extend(ts_list)
            cache["events"][cls] = sorted(set(cache["events"][cls]))
        cache["events"].setdefault("end_graph", []).extend(eg_result.raw_hits)
        cache["events"]["end_graph"] = sorted(set(cache["events"]["end_graph"]))

        # ：永遠掃 bp_ui (stride=15 省 GPU) — 不再等 45 min overdue
        # 配合 fallback「下一場 BP confirmed → 切上一場」邏輯立刻可用，避免兩場合併。
        # filter `bp_hits_for_next >= anchor + bp_phase_min_sec`（line ~558）保證不誤抓本場 BP。
        bp_hits = det._detect_multi_classes(["bp_ui"], scan_from, latest_offset, 15.0)
        cache["events"]["bp_ui"].extend(bp_hits.get("bp_ui", []))
        cache["events"]["bp_ui"] = sorted(set(cache["events"]["bp_ui"]))

        cache["scanned_until_offset_sec"] = latest_offset
        cache["last_updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _save_cache_atomic(cache_path, cache)

        # 先檢查 end signals confirmed
        all_end_hits = sorted(set(
            cache["events"].get("end_graph", [])
            + cache["events"].get("nexus_explosion", [])
            + cache["events"].get("game_end_screen", [])
        ))
        # 只看 anchor 之後的（BP_PHASE 結束之後才算「這場」的結束訊號）
        end_hits_for_this = [t for t in all_end_hits if t >= anchor]
        confirmed_end = _confirmed_signal_offset(
            end_hits_for_this,
            min_duration=self.end_confirm_sec, max_gap=15.0,
        )

        if confirmed_end is not None:
            game_end = confirmed_end + self.end_buffer_sec
            self._cut_game(conn, broadcast, prefix, raw_dir, ts_files,
                           anchor, game_end, end_source=self._classify_end_source(
                               cache["events"], confirmed_end))
            DetectorStateRepo(conn).update_phase(bid, "cooldown", game_end,
                                                  last_scan_until_sec=latest_offset)
            return

        # 第一輪沒 confirmed_end + broadcast 已結束 + .ts 5 min 沒新增
        # → 觸發第二輪「細掃 end_graph」(conf=0.20 sample=1.0) 從 BP_PHASE 結束點掃到末端
        # user 鐵則：「一定要抓到 END_GRAPH，不能沒抓到就強制切」+「沒抓到就改用更細偵測再掃一次」
        if (broadcast.get("recording_status_v2") == "post_recording"
                and ts_files):
            latest_mtime = max(t.stat().st_mtime for t in ts_files)
            ts_age_min = (time.time() - latest_mtime) / 60
            if ts_age_min >= 5.0 and not cache.get("dense_rescan_done"):
                logger.warning(
                    "broadcast %s 第一輪 end_graph 沒 confirmed -> 觸發細掃 (conf=0.20 sample=1.0)",
                    bid,
                )
                eg_dense = self._get_end_graph_detector_dense()
                dense_scan_start = anchor + self.bp_phase_min_sec  # 從 BP 結束點掃
                dense_result = eg_dense.detect(
                    cumulative,
                    scan_start=dense_scan_start,
                    scan_end=latest_offset,
                )
                cache["events"]["end_graph"].extend(dense_result.raw_hits)
                cache["events"]["end_graph"] = sorted(set(cache["events"]["end_graph"]))
                cache["dense_rescan_done"] = True
                _save_cache_atomic(cache_path, cache)
                logger.info(
                    "broadcast %s 細掃結果：end_graph hits=%d",
                    bid, len(dense_result.raw_hits),
                )

                # 重新檢查 confirmed_end（含細掃 hits）
                all_end_hits_v2 = sorted(set(
                    cache["events"].get("end_graph", [])
                    + cache["events"].get("nexus_explosion", [])
                    + cache["events"].get("game_end_screen", [])
                ))
                end_hits_for_this_v2 = [t for t in all_end_hits_v2 if t >= anchor]
                confirmed_end_v2 = _confirmed_signal_offset(
                    end_hits_for_this_v2,
                    min_duration=self.end_confirm_sec, max_gap=15.0,
                )
                if confirmed_end_v2 is not None:
                    game_end = confirmed_end_v2 + self.end_buffer_sec
                    logger.info(
                        "broadcast %s 細掃抓到 end_graph confirmed @%.0fs -> 切 g%d",
                        bid, confirmed_end_v2, state.get("current_game_index", 1),
                    )
                    self._cut_game(conn, broadcast, prefix, raw_dir, ts_files,
                                   anchor, game_end,
                                   end_source=self._classify_end_source(
                                       cache["events"], confirmed_end_v2))
                    DetectorStateRepo(conn).update_phase(
                        bid, "cooldown", game_end,
                        last_scan_until_sec=latest_offset,
                    )
                    return
                # 細掃還是沒抓到 → 走 next_bp_fallback / fail-fast 邏輯
                logger.warning(
                    "broadcast %s 細掃也沒抓到 end_graph -> 嘗試 next_bp_fallback / fail-fast",
                    bid,
                )

        # ：永遠執行下一場 BP fallback cut（不等 45 min overdue）
        # filter 確保只看 BP_PHASE 結束之後的 bp_ui hits（不誤抓本場 BP）
        # 沒有 end signals confirmed，看 fallback：bp_ui confirmed
        search_bp_after = anchor + self.bp_phase_min_sec
        bp_hits_for_next = [t for t in cache["events"]["bp_ui"] if t >= search_bp_after]
        confirmed_bp = _confirmed_signal_offset(
            bp_hits_for_next, min_duration=30.0, max_gap=20.0,
        )
        if confirmed_bp is not None:
            # 下一場 BP first hit ≈ confirmed_bp - 30s
            first_bp_offset = confirmed_bp - 30.0
            game_end = first_bp_offset - 60.0  # 上一場 game_end = next BP 前 60s
            logger.info(
                "broadcast %s SEARCH_END fallback: 偵測下一場 BP @ %.0fs，前一場 game_end=%.0f",
                bid, first_bp_offset, game_end,
            )
            self._cut_game(conn, broadcast, prefix, raw_dir, ts_files,
                           anchor, game_end, end_source="next_bp_fallback")
            # 直接進 IDLE 處理下一場 BP（first_bp_offset 當 anchor 暫時用，IDLE 會掃 bp_ui 確認）
            DetectorStateRepo(conn).update_phase(
                bid, "idle", first_bp_offset,
                increment_game_index=True,
                last_scan_until_sec=latest_offset,
            )
            return

        # SEARCH_END 持續 ≥ 2 倍 overdue（90 min）仍找不到任何訊號
        # → 標這場 game 為 failed（不切垃圾 mp4），進 cooldown 找下一場
        max_search_duration = 2 * self.search_end_overdue_sec
        if time_in_phase >= max_search_duration:
            self._mark_game_failed_no_end(conn, broadcast, anchor, latest_offset)
            DetectorStateRepo(conn).update_phase(
                bid, "cooldown", latest_offset,
                last_scan_until_sec=latest_offset,
            )
            logger.error(
                "broadcast %s SEARCH_END 持續 %.0fs 仍找不到 end signals 也找不到下一場 BP "
                "-> 標 game failed 進 cooldown（fail-fast 跨場隔離）",
                bid, time_in_phase,
            )

    def _mark_game_failed_no_end(
        self, conn, broadcast: dict, anchor: float, latest_offset: float,
    ) -> None:
        """SEARCH_END 超時 fail-fast：寫 broadcast_games row 但不切 mp4、不 enqueue clip_jobs。

        寧可漏一場 highlight，也不要硬切垃圾或卡住下一場偵測。
        """
        bid = broadcast["broadcast_id"]
        repo = BroadcastGameRepo(conn)
        state = DetectorStateRepo(conn).get_or_init(bid)
        game_index = int(state.get("current_game_index", 1))

        bp_start = anchor - self.bp_phase_min_sec
        start_offset = max(0.0, bp_start - self.lead_in_sec)

        err_msg = "end signals 三類 + 下一場 BP 都偵測失敗（fail-fast 不切垃圾）"
        team_a, team_b, _n_series = self._get_team_codes_for_broadcast(conn, bid)
        existing = repo.get_by_index(bid, game_index)
        if existing is None:
            game_id = repo.insert(
                broadcast_id=bid, game_index=game_index,
                start_offset_sec=start_offset, end_offset_sec=latest_offset,
                start_source="bp_detected", end_source="not_detected",
                status="detecting", confidence=0.0,
                team_a_code=team_a, team_b_code=team_b,
            )
            repo.mark_failed(game_id, err_msg)
        else:
            repo.mark_failed(existing["game_id"], err_msg)

    def _classify_end_source(self, events: dict, confirmed_end_offset: float) -> str:
        """confirmed_end 是哪個訊號最晚 hit 的 → 標 end_source。"""
        # 找 confirmed_end_offset 是來自哪個 events list（可能多個都接近）
        # 用最近一個（差距 ≤ 15s）作為 source
        candidates = []
        for cls in ("end_graph", "nexus_explosion", "game_end_screen"):
            for t in events.get(cls, []):
                if abs(t - confirmed_end_offset) <= 15.0:
                    candidates.append((t, cls))
        if not candidates:
            return "end_graph"
        # 取最晚的
        return max(candidates, key=lambda x: x[0])[1]

    # ── 切片 + enqueue（從原版精簡） ────────────────────────────────────
    def _cut_game(self, conn, broadcast: dict, prefix: str, raw_dir: Path,
                   ts_files: list[Path], bp_start_anchor: float, game_end: float,
                   end_source: str) -> None:
        bid = broadcast["broadcast_id"]
        repo = BroadcastGameRepo(conn)
        state = DetectorStateRepo(conn).get_or_init(bid)
        game_index = int(state.get("current_game_index", 1))

        # bp_start_anchor 是 anchor + bp_phase_min_sec... 不對，要倒推真實 BP_start。
        # state 裡的 anchor 在 BP_PHASE→SEARCH_END 切換時 = (bp_start + bp_phase_min_sec)
        # 所以真實 bp_start = anchor - bp_phase_min_sec
        bp_start = bp_start_anchor - self.bp_phase_min_sec
        start_offset = max(0.0, bp_start - self.lead_in_sec)

        confidence = {"end_graph": 0.95, "game_end_screen": 0.90,
                      "nexus_explosion": 0.85, "next_bp_fallback": 0.55}.get(end_source, 0.0)

        # Phase 55：切前先 derive series → 命名直接對到 series（無需依賴 naming_finalizer 事後 rename）
        # lazy import 避免 circular（live_split → naming_finalizer 之前沒 import）
        from automation.services.naming_finalizer import derive_series_for_game_index
        derived = derive_series_for_game_index(bid, game_index)

        if derived:
            # 95% 場次：derive 成功 → 直接用對的 series + team codes + local_game_index
            series_id = derived["series_id"]
            series_order = derived["local_game_index"]
            team_a = derived["team_a_code"]
            team_b = derived["team_b_code"]
            provisional = False
            file_index = derived["local_game_index"]
            n_series = None  # 不需要再算
            logger.info(
                "broadcast %s game %d derive 成功：series_id=%s local_idx=%s team=%s/%s（命名 final）",
                bid, game_index, series_id, series_order, team_a, team_b,
            )
        else:
            # 5% race：series inProgress 沒比分 → derive 不出 → fallback 既有行為
            team_a, team_b, n_series = self._get_team_codes_for_broadcast(conn, bid)
            provisional = n_series > 1
            series_id = None
            series_order = None
            file_index = game_index  # 用 broadcast-wide game_index 當暫名
            logger.info(
                "broadcast %s game %d derive None（race window）-> fallback 第一 series codes "
                "team=%s/%s n_series=%s provisional=%s",
                bid, game_index, team_a, team_b, n_series, provisional,
            )

        existing = repo.get_by_index(bid, game_index)
        if existing is None:
            game_id = repo.insert(
                broadcast_id=bid, game_index=game_index,
                start_offset_sec=start_offset, end_offset_sec=game_end,
                start_source="bp_detected", end_source=end_source,
                status="detecting", confidence=confidence,
                team_a_code=team_a, team_b_code=team_b,
                series_id=series_id, series_order=series_order,
            )
            logger.info("broadcast %s game %d insert detecting (game_id=%d, derived=%s)",
                        bid, game_index, game_id, bool(derived))
            if provisional:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE broadcast_games SET naming_provisional=TRUE WHERE game_id=%s",
                        (game_id,),
                    )
                conn.commit()
                logger.info("broadcast %s game %d 多 series broadcast -> 標 naming_provisional",
                            bid, game_index)
        else:
            game_id = existing["game_id"]
            repo.update_boundary(game_id, end_offset_sec=game_end,
                                 end_source=end_source, confidence=confidence)
            # Phase 55：existing row 若這次 derive 成功，補寫 series_id/order/team_codes
            # + 清 naming_provisional（避免 cron 再 rename 已 final 的命名）
            if derived:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE broadcast_games SET series_id=%s, series_order=%s, "
                        "team_a_code=%s, team_b_code=%s, naming_provisional=FALSE "
                        "WHERE game_id=%s",
                        (series_id, series_order, team_a, team_b, game_id),
                    )
                conn.commit()
                logger.info("broadcast %s game %d existing row 補寫 series (derive 成功)",
                            bid, game_index)

        if self.dry_run:
            logger.info("[DRY] game_id=%s 不切", game_id); return
        if self.no_cut:
            logger.info("[--no-cut] game_id=%s 跳", game_id); return

        repo.update_status(game_id, "cutting")
        try:
            cumulative = raw_dir / "_cumulative.mp4"
            _ensure_cumulative_fresh(cumulative, ts_files, game_end)
            game_path = self._make_game_output_path(
                broadcast, file_index, team_a=team_a, team_b=team_b,
            )
            ffmpeg_utils.cut(cumulative, start_sec=start_offset, end_sec=game_end, output=game_path)
        except Exception as e:
            repo.mark_failed(game_id, f"{type(e).__name__}: {e}")
            logger.exception("broadcast %s game %d 切片失敗", bid, game_index)
            return

        enqueue = bool(broadcast.get("auto_clip")) and not self.no_enqueue
        try:
            repo.set_cut(game_id, str(game_path.resolve()), commit=False)
            if enqueue:
                ClipJobRepo(conn).enqueue_or_reset_failed(game_id, commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("broadcast %s game %d cut 狀態與 clip job 交易失敗", bid, game_index)
            raise

        logger.info("broadcast %s game %d 切完 -> %s", bid, game_index, game_path)
        if self.no_enqueue:
            logger.info("[--no-enqueue] game_id=%s 跳 enqueue", game_id)
        elif enqueue:
            logger.info("game_id=%s enqueued", game_id)

    # ── helpers ──────────────────────────────────────────────────────────
    def _make_game_output_path(
        self, broadcast: dict, file_index: int,
        team_a: str | None = None, team_b: str | None = None,
    ) -> Path:
        """命名格式：<LEAGUE>_<YYYYMMDD>_<TA>vs<TB>_g<file_index>.mp4

        例：LCK_20260508_T1vsGEN_g1.mp4

        file_index 由 caller 給：跨 series broadcast 已 derive 成功 → local_game_index
        （series 內第幾局）；fallback path → broadcast-wide game_index。

        若拿不到 team codes（series mapping 缺）退回舊格式 <league>_<date>_<platform>_<short_id>_g<n>.mp4。
        """
        league = broadcast["league_code"]
        date_str = broadcast["broadcast_date"].strftime("%Y%m%d")
        if team_a and team_b:
            prefix = f"{league}_{date_str}_{team_a.upper()}vs{team_b.upper()}"
        else:
            platform = broadcast["platform"]
            short_id = (broadcast.get("external_id") or "")[:8]
            prefix = f"{league}_{date_str}_{platform}_{short_id}"
        out_dir = self.games_output_root / prefix
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{prefix}_g{file_index}.mp4"

    def _get_team_codes_for_broadcast(
        self, conn, broadcast_id: int,
    ) -> tuple[str | None, str | None, int]:
        """從 broadcast_id 撈對應第一個 series 的 team_a_code / team_b_code + series 總數。

        除了 team codes 也回傳 n_series 給 _cut_game 判斷是否該標 naming_provisional：
          n_series == 1 → 命名永遠正確（包括 LPL 單 series + LCK/LCP 單場直播）
          n_series > 1  → 多 series broadcast（LCK 雙場連播）→ 暫用第一 series codes，標 provisional

        回傳 (team_a, team_b, n_series)。
        """
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ta.code AS team_a, tb.code AS team_b
                FROM broadcast_series bs
                JOIN series s ON s.series_id = bs.series_id
                LEFT JOIN teams ta ON ta.team_id = s.team_a_id
                LEFT JOIN teams tb ON tb.team_id = s.team_b_id
                WHERE bs.broadcast_id = %s
                ORDER BY bs.series_order
                LIMIT 1
                """,
                (broadcast_id,),
            )
            row = cur.fetchone()

            cur.execute(
                "SELECT COUNT(*) AS c FROM broadcast_series WHERE broadcast_id=%s",
                (broadcast_id,),
            )
            n = (cur.fetchone() or {}).get("c", 0) or 0

        if not row:
            return None, None, int(n)
        return row.get("team_a"), row.get("team_b"), int(n)

    def _infer_prefix(self, broadcast: dict, raw_dir: Path) -> str | None:
        ts = next(raw_dir.glob("*_part_*.ts"), None)
        if ts is None:
            return None
        stem = ts.stem
        idx = stem.rfind("_part_")
        if idx < 0:
            return None
        return stem[:idx]


# ─────────────────────────────────────────────────────────────────────────────
def run_worker(
    *,
    once: bool = False,
    dry_run: bool = False,
    no_cut: bool = False,
    no_enqueue: bool = False,
) -> None:
    cfg = _load_config().get("live_split", {})
    worker = LiveSplitWorker(
        poll_interval_sec=float(cfg.get("poll_interval_sec", DEFAULT_POLL_INTERVAL_SEC)),
        min_ts_duration_sec=float(cfg.get("min_ts_duration_sec", DEFAULT_MIN_TS_DURATION_SEC)),
        min_new_range_sec=float(cfg.get("min_new_range_sec", DEFAULT_MIN_NEW_RANGE_SEC)),
        ts_stable_age_sec=float(cfg.get("ts_stable_age_sec", DEFAULT_TS_STABLE_AGE_SEC)),
        stride_sec=float(cfg.get("stride_sec", DEFAULT_STRIDE_SEC)),
        lead_in_sec=float(cfg.get("lead_in_sec", DEFAULT_LEAD_IN_SEC)),
        games_output_root=(Path(cfg["games_output_root"]) if cfg.get("games_output_root") else None),
        device=cfg.get("device", DEFAULT_DEVICE),
        bp_confirm_sec=float(cfg.get("bp_confirm_duration_sec", DEFAULT_BP_CONFIRM_SEC)),
        bp_phase_min_sec=float(cfg.get("bp_phase_min_duration_sec", DEFAULT_BP_PHASE_MIN_SEC)),
        search_end_overdue_sec=float(cfg.get("search_end_overdue_sec", DEFAULT_SEARCH_END_OVERDUE)),
        end_confirm_sec=float(cfg.get("end_signal_confirm_sec", DEFAULT_END_CONFIRM_SEC)),
        cooldown_sec=float(cfg.get("cooldown_duration_sec", DEFAULT_COOLDOWN_SEC)),
        end_buffer_sec=float(cfg.get("end_buffer_sec", DEFAULT_END_BUFFER_SEC)),
        dry_run=dry_run, no_cut=no_cut, no_enqueue=no_enqueue,
    )
    worker.run(once=once)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-cut", action="store_true")
    p.add_argument("--no-enqueue", action="store_true")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    run_worker(once=args.once, dry_run=args.dry_run,
               no_cut=args.no_cut, no_enqueue=args.no_enqueue)


if __name__ == "__main__":
    main()

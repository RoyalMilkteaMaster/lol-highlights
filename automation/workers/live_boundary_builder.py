"""從 cache events 重組成 game boundaries。

# MIRROR FROM detectors/yolo_detector.py:
#   _aggregate_bp_intervals 邏輯（line 326-358）
#   _merge_bp_gap          邏輯（line 360-376）
#   game_end 配對          邏輯（line 378-403）
# 改既有 yolo_detector 規則時兩邊都要改！

跟既有 get_all_game_boundaries 的差別（v8 plan 修正）：
1. 加入 nexus_explosion + end_graph 兩個訊號（Blocker 1）
2. game_end = max(所有 end signals in window) + END_BUFFER_SEC（不是 min + 30s）
   理由：要把整個結算流程（爆炸 → victory → 賽後總表）剪進去（R7）
3. 最後一場沒結束訊號時：
   - 還在錄影：game_end=None（不切，等更多數據）
   - 已錄完（recorded）：用 stream_end_offset_sec - 30s 收尾（Blocker 2）

絕不依賴：DB / 檔案 IO。純 CPU 邏輯，方便 unit test。
"""

from __future__ import annotations

from dataclasses import dataclass

# ── 規則參數（與既有 get_all_game_boundaries 對齊）────────────────────────
DEFAULT_STRIDE_SEC      = 7.5     # 每 7.5 秒採樣一幀（驗證 stride=30→7.5 把救援率拉到 100%）
DEFAULT_DISAPPEAR_SEC   = 15.0    # = stride * 2，BP UI 連續消失 ≥ 15s 視為 BP 結束
DEFAULT_MIN_BP_DURATION = 120.0   # BP 持續 < 120s 視為短假 BP（採訪鏡頭等）
DEFAULT_MERGE_GAP_SEC   = 600.0   # 相鄰 BP gap < 10min 合併（修 Z）
DEFAULT_MIN_GAME_DURATION = 900.0 # bp_end → game_end 至少 15min
DEFAULT_END_BUFFER_SEC  = 15.0    # max(end signals) + 此 buffer 當切點，包進 end_graph
DEFAULT_NEXT_BP_GAP_SEC = 60.0    # 下一場 BP 開始前 60s 當 search_end


@dataclass
class GameBoundary:
    game_num: int                  # 1-based
    bp_start: float                # BP UI 第一次出現的秒數
    bp_end: float                  # BP UI 最後一次出現的秒數
    game_end: float | None         # 切片結束秒（None = 還沒抓到，不要切）
    end_source: str | None         # 'game_end_screen' / 'nexus_explosion' / 'end_graph'
                                   # / 'next_bp_fallback' / 'stream_end_fallback'
    is_real_game: bool             # game_end - bp_start >= MIN_GAME_DURATION

    def to_dict(self) -> dict:
        return {
            "game_num":     self.game_num,
            "bp_start":     self.bp_start,
            "bp_end":       self.bp_end,
            "game_end":     self.game_end,
            "end_source":   self.end_source,
            "is_real_game": self.is_real_game,
        }


# ─────────────────────────────────────────────────────────────────────────────
def _aggregate_bp_intervals(
    bp_times: list[float],
    *,
    disappear_sec: float,
    min_bp_duration: float,
) -> list[tuple[float, float]]:
    """從散點 bp_ui hits 聚合出 BP 區間。

    用「sorted + gap」邏輯（不依賴 timeline 對齊）：
    - 連續 hit 之間 gap <= disappear_sec → 同一段
    - gap > disappear_sec → 結算前段 + 開新段
    - 段長度 < min_bp_duration → 丟棄（短假 BP）

    bp_end 採用該段「最後 hit」（語意：BP UI 最後一次可見的時間點）。
    """
    sorted_times = sorted(set(bp_times))
    if not sorted_times:
        return []

    intervals: list[tuple[float, float]] = []
    current_start = sorted_times[0]
    current_end = sorted_times[0]

    for t in sorted_times[1:]:
        if t - current_end <= disappear_sec:
            current_end = t
        else:
            if current_end - current_start >= min_bp_duration:
                intervals.append((current_start, current_end))
            current_start = t
            current_end = t

    if current_end - current_start >= min_bp_duration:
        intervals.append((current_start, current_end))

    return intervals


# ─────────────────────────────────────────────────────────────────────────────
def _merge_bp_gap(
    intervals: list[tuple[float, float]],
    *,
    merge_gap_sec: float,
) -> list[tuple[float, float]]:
    """相鄰 BP 區間 gap < merge_gap_sec 合併（同場被拍選手鏡頭切斷的修正，修 Z）。"""
    if not intervals:
        return []
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        prev_s, prev_e = merged[-1]
        if s - prev_e < merge_gap_sec:
            merged[-1] = (prev_s, e)
        else:
            merged.append((s, e))
    return merged


# ─────────────────────────────────────────────────────────────────────────────
def build_boundaries(
    cache_events: dict,
    *,
    scan_start: float = 0.0,
    scan_end: float,
    stream_end_offset_sec: float | None = None,
    stride_sec: float = DEFAULT_STRIDE_SEC,
    disappear_sec: float = DEFAULT_DISAPPEAR_SEC,
    min_bp_duration: float = DEFAULT_MIN_BP_DURATION,
    merge_gap_sec: float = DEFAULT_MERGE_GAP_SEC,
    min_game_duration: float = DEFAULT_MIN_GAME_DURATION,
    end_buffer_sec: float = DEFAULT_END_BUFFER_SEC,
    next_bp_gap_sec: float = DEFAULT_NEXT_BP_GAP_SEC,
) -> list[GameBoundary]:
    """從 cache 4 類 events 重組成 boundaries。

    Args:
        cache_events           : {'bp_ui':[...], 'game_end_screen':[...],
                                  'nexus_explosion':[...], 'end_graph':[...]}
        scan_start / scan_end  : 已掃描的範圍（秒）
        stream_end_offset_sec  : Blocker 2 — 直播已結束才傳，否則 None
                                 用 latest_offset 也可（同義）
    """
    bp_intervals = _aggregate_bp_intervals(
        cache_events.get("bp_ui", []),
        disappear_sec=disappear_sec,
        min_bp_duration=min_bp_duration,
    )
    bp_intervals = _merge_bp_gap(bp_intervals, merge_gap_sec=merge_gap_sec)

    end_times   = sorted(set(cache_events.get("game_end_screen", [])))
    nexus_times = sorted(set(cache_events.get("nexus_explosion", [])))
    eg_times    = sorted(set(cache_events.get("end_graph", [])))

    boundaries: list[GameBoundary] = []
    for i, (bp_start, bp_end) in enumerate(bp_intervals):
        game_num = i + 1
        # game 結束的搜索窗口
        search_start = bp_end + min_game_duration
        next_bp_start = bp_intervals[i + 1][0] if i + 1 < len(bp_intervals) else None
        search_end = (
            (next_bp_start - next_bp_gap_sec)
            if next_bp_start is not None
            else scan_end
        )

        # 收集所有「結束訊號」候選
        candidates: list[tuple[float, str]] = []
        for t in end_times:
            if search_start <= t <= search_end:
                candidates.append((t, "game_end_screen"))
        for t in nexus_times:
            if search_start <= t <= search_end:
                candidates.append((t, "nexus_explosion"))
        for t in eg_times:
            if search_start <= t <= search_end:
                candidates.append((t, "end_graph"))

        game_end: float | None = None
        end_source: str | None = None

        if candidates:
            # 規則修正：取「最晚」的訊號 + buffer，把 end_graph 包進切片
            last_t, src = max(candidates, key=lambda x: x[0])
            game_end = last_t + end_buffer_sec
            end_source = src
        elif next_bp_start is not None:
            # 規則 #9：下一場 BP 已開始 → 用 search_end 當保險
            game_end = search_end
            end_source = "next_bp_fallback"
        elif stream_end_offset_sec is not None:
            # Blocker 2：直播結束才能用 stream_end fallback
            # 注意：search_end is None 才會走到這裡
            game_end = stream_end_offset_sec - 30.0
            end_source = "stream_end_fallback"
        # else：錄影中且最後一場沒結束訊號 → game_end=None（保險，不切）

        if game_end is None:
            is_real_game = False
        else:
            is_real_game = (game_end - bp_start) >= min_game_duration

        boundaries.append(GameBoundary(
            game_num=game_num,
            bp_start=bp_start,
            bp_end=bp_end,
            game_end=game_end,
            end_source=end_source,
            is_real_game=is_real_game,
        ))

    return boundaries


def confidence_for_source(end_source: str | None) -> float:
    """end_source → confidence 0~1（給 broadcast_games.confidence 用）。

    優先序：end_graph（賽後總表，最穩）> game_end_screen > nexus > next_bp_fallback > stream_end_fallback
    """
    return {
        "end_graph":           0.95,
        "game_end_screen":     0.90,
        "nexus_explosion":     0.85,
        "next_bp_fallback":    0.55,
        "stream_end_fallback": 0.50,
        "manual":              1.00,
    }.get(end_source or "", 0.0)

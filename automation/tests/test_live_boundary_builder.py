"""live_boundary_builder unit tests。

測試重點：
1. BP 聚合 + 合併規則 mirror 既有 get_all_game_boundaries 結果
2. game_end 取最晚訊號 + 15s buffer（不是最早 + 30s）
3. nexus / end_graph 補救（v8 Blocker 1）
4. 錄影中最後一場沒訊號 → game_end=None 不切（v8 Blocker 2）
5. 已錄完最後一場沒訊號 → stream_end fallback
"""

from __future__ import annotations

import unittest

from automation.workers.live_boundary_builder import (
    build_boundaries,
)


# ── Helper ───────────────────────────────────────────────────────────────────
def _bp_hits(start: float, end: float, stride: float = 7.5) -> list[float]:
    """產生連續 bp_ui 命中（模擬 BP 階段每 stride 都中）。"""
    out, t = [], start
    while t <= end + 1e-6:
        out.append(round(t, 3))
        t += stride
    return out


# ── 1) BP 聚合 + 合併 ────────────────────────────────────────────────────────
class BpAggregationTests(unittest.TestCase):

    def test_single_bp_interval(self):
        """單場：bp_ui 連續 5 分鐘 → 1 個區間。"""
        events = {
            "bp_ui": _bp_hits(300, 600, stride=7.5),
            "game_end_screen": [2400.0],
        }
        result = build_boundaries(events, scan_end=3600)
        self.assertEqual(len(result), 1)
        b = result[0]
        self.assertAlmostEqual(b.bp_start, 300, delta=15)
        self.assertAlmostEqual(b.bp_end, 600, delta=15)

    def test_short_bp_filtered(self):
        """BP < 120s（min_bp_duration）視為短假 BP，丟棄。"""
        events = {
            "bp_ui": _bp_hits(300, 380, stride=7.5),  # 80s < 120s
            "game_end_screen": [],
        }
        result = build_boundaries(events, scan_end=3600)
        self.assertEqual(len(result), 0)

    def test_bp_gap_merged(self):
        """同場 BP 中間有「拍選手鏡頭」斷檔 200s（< 600s）→ 合併成 1 個。"""
        events = {
            "bp_ui": (
                _bp_hits(300, 500, stride=7.5)   # 第一段
                + _bp_hits(700, 900, stride=7.5)  # 第二段（gap 200s）
            ),
            "game_end_screen": [],
        }
        result = build_boundaries(events, scan_end=3600)
        self.assertEqual(len(result), 1, "gap 200s < 600s 應合併")
        self.assertAlmostEqual(result[0].bp_start, 300, delta=15)
        self.assertAlmostEqual(result[0].bp_end, 900, delta=15)

    def test_bp_gap_too_far_not_merged(self):
        """場間距離 1500s > 600s → 不合併（兩場）。"""
        events = {
            "bp_ui": (
                _bp_hits(300, 500, stride=7.5)
                + _bp_hits(2000, 2200, stride=7.5)
            ),
            "game_end_screen": [1800.0, 3600.0],
        }
        result = build_boundaries(events, scan_end=4800)
        self.assertEqual(len(result), 2)


# ── 2) game_end 取最晚訊號 + 15s buffer ─────────────────────────────────────
class GameEndRuleTests(unittest.TestCase):

    def test_take_latest_not_earliest(self):
        """game_end = max(所有訊號) + 15s（要包進 end_graph）。"""
        events = {
            "bp_ui": _bp_hits(300, 600, stride=7.5),
            # bp_end ~ 600, search_start = 600 + 900 = 1500
            "nexus_explosion": [1500.0],   # 最早
            "game_end_screen": [1530.0],
            "end_graph": [1545.0, 1552.5, 1560.0],   # 最晚 = 1560
        }
        result = build_boundaries(events, scan_end=3600)
        self.assertEqual(len(result), 1)
        b = result[0]
        # 最晚 + 15s = 1560 + 15 = 1575
        self.assertEqual(b.end_source, "end_graph")
        self.assertAlmostEqual(b.game_end, 1575.0, delta=1)

    def test_only_nexus(self):
        """只有 nexus，沒 game_end_screen 或 end_graph → 用 nexus_LAST + 15s。"""
        events = {
            "bp_ui": _bp_hits(300, 600, stride=7.5),
            "nexus_explosion": [1500.0, 1530.0],
        }
        result = build_boundaries(events, scan_end=3600)
        self.assertEqual(result[0].end_source, "nexus_explosion")
        self.assertAlmostEqual(result[0].game_end, 1545.0, delta=1)

    def test_only_end_graph(self):
        """只有 end_graph（game 3 那種）→ 用 end_graph 收尾。"""
        events = {
            "bp_ui": _bp_hits(300, 600, stride=7.5),
            "end_graph": [1700.0, 1707.5, 1715.0],
        }
        result = build_boundaries(events, scan_end=3600)
        self.assertEqual(result[0].end_source, "end_graph")
        self.assertAlmostEqual(result[0].game_end, 1730.0, delta=1)


# ── 3) Fallback：next_bp / stream_end / None ────────────────────────────────
class FallbackTests(unittest.TestCase):

    def test_next_bp_fallback_when_no_signal(self):
        """有下一場 BP 但 game_end 訊號全漏 → 用 next_bp - 60s。"""
        events = {
            "bp_ui": (
                _bp_hits(300, 500, stride=7.5)
                + _bp_hits(2400, 2600, stride=7.5)
            ),
            # 第一場完全沒結束訊號
            "game_end_screen": [],
            "nexus_explosion": [],
            "end_graph": [],
        }
        result = build_boundaries(events, scan_end=3600)
        self.assertEqual(len(result), 2)
        b1 = result[0]
        self.assertEqual(b1.end_source, "next_bp_fallback")
        # 下一場 BP 開始於 2400，game_end = 2400 - 60 = 2340
        self.assertAlmostEqual(b1.game_end, 2340.0, delta=15)

    def test_last_game_recording_no_signal_no_cut(self):
        """最後一場 + 還在錄影 + 沒訊號 → game_end=None（不切）。"""
        events = {
            "bp_ui": _bp_hits(300, 600, stride=7.5),
            "game_end_screen": [],
            "nexus_explosion": [],
            "end_graph": [],
        }
        # stream_end_offset_sec=None 表示還在錄
        result = build_boundaries(events, scan_end=3600,
                                   stream_end_offset_sec=None)
        self.assertIsNone(result[0].game_end)
        self.assertIsNone(result[0].end_source)
        self.assertFalse(result[0].is_real_game)

    def test_last_game_recorded_no_signal_stream_end(self):
        """最後一場 + 已錄完 + 沒訊號 → stream_end - 30s fallback。"""
        events = {
            "bp_ui": _bp_hits(300, 600, stride=7.5),
            "game_end_screen": [],
            "nexus_explosion": [],
            "end_graph": [],
        }
        result = build_boundaries(events, scan_end=3600,
                                   stream_end_offset_sec=3600)
        self.assertEqual(result[0].end_source, "stream_end_fallback")
        self.assertAlmostEqual(result[0].game_end, 3570.0, delta=1)


# ── 4) 多場景：完整 BO5 ─────────────────────────────────────────────────────
class FullBoxScenarioTests(unittest.TestCase):

    def test_lck_carry_4_games_recovered(self):
        """模擬 LCK_Carry 4hr 4 場 — game 3 只有 end_graph 也能切。

        實測值（stride=7.5 對 LCK_Carry_xxx VOD 結果）：
        Game 1 BP 0:51:15~0:59:00, last signal end_graph 1:37:15
        Game 2 BP 1:50:45~1:57:15, last signal end_graph 2:28:15
        Game 3 BP 3:03:52~3:11:30, last signal end_graph 3:38:37
        Game 4 BP 3:52:00~4:00:15, last signal end_graph 4:32:52
        """
        # 用區段化的 hits 模擬（實測 stride=7.5 的 BP 區間）
        bp_times = []
        for s, e in [(3075, 3540), (6645, 7035), (11032.5, 11490), (13920, 14415)]:
            bp_times.extend(_bp_hits(s, e, stride=7.5))

        events = {
            "bp_ui": bp_times,
            "game_end_screen": [5775, 8610, 8632.5, 8655, 16177.5, 16357.5],
            "nexus_explosion": [3952.5, 4950, 4965, 5025, 5775, 13012.5, 15360, 16080],
            "end_graph": [5820, 5827.5, 5835, 8880, 8887.5, 8895, 13102.5, 13110, 13117.5, 16365, 16372.5],
        }
        result = build_boundaries(events, scan_end=17461)
        self.assertEqual(len(result), 4, f"應切 4 場，實際 {len(result)}：{[b.to_dict for b in result]}")
        for b in result:
            self.assertIsNotNone(b.game_end, f"game {b.game_num} game_end 應該有值")
            self.assertTrue(b.is_real_game, f"game {b.game_num} 應 is_real_game=True")
        # game 1 最晚訊號是 end_graph 5835 → game_end ≈ 5850
        self.assertAlmostEqual(result[0].game_end, 5850.0, delta=20)
        # game 4 最晚訊號是 end_graph 16372.5 → game_end ≈ 16387
        self.assertAlmostEqual(result[3].game_end, 16387.5, delta=20)


if __name__ == "__main__":
    unittest.main(verbosity=2)

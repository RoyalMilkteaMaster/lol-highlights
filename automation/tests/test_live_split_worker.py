"""live_split_worker 狀態機 + hit grouping 單元測試。"""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from automation.workers.live_split_worker import LiveSplitWorker, _confirmed_signal_offset


class ConfirmedSignalOffsetTests(unittest.TestCase):
    """GPT review 5：hit grouping 防誤判。"""

    def test_empty(self):
        self.assertIsNone(_confirmed_signal_offset([], min_duration=15, max_gap=15))

    def test_single_hit_not_confirmed(self):
        """單一 hit 持續時間 = 0，不應 confirmed。"""
        self.assertIsNone(_confirmed_signal_offset([100.0], min_duration=15, max_gap=15))

    def test_short_burst_not_confirmed(self):
        """連續 5 秒命中 < 15s min_duration → 不 confirmed。"""
        hits = [100, 102, 104, 105]   # 5s 持續
        self.assertIsNone(_confirmed_signal_offset(hits, min_duration=15, max_gap=15))

    def test_long_burst_confirmed(self):
        """連續 15 秒命中 → confirmed，回傳 last hit。"""
        hits = [100, 105, 110, 115]   # 15s 持續
        result = _confirmed_signal_offset(hits, min_duration=15, max_gap=15)
        self.assertEqual(result, 115)

    def test_grouping_by_max_gap(self):
        """gap > max_gap 的 hit 分到不同 group。"""
        # group 1: 100-115 (15s 持續)，group 2: 200-205 (5s 持續)
        hits = [100, 105, 110, 115, 200, 205]
        result = _confirmed_signal_offset(hits, min_duration=15, max_gap=15)
        # group 1 confirmed, group 2 not → 回 115
        self.assertEqual(result, 115)

    def test_returns_latest_confirmed_group(self):
        """多個 confirmed group 取最晚那組的 last。"""
        # group 1: 100-115 (15s)，group 2: 300-330 (30s)
        hits = [100, 105, 110, 115, 300, 310, 320, 330]
        result = _confirmed_signal_offset(hits, min_duration=15, max_gap=15)
        self.assertEqual(result, 330)

    def test_unsorted_input(self):
        """input 未排序也要正常 work。"""
        hits = [115, 100, 110, 105]
        result = _confirmed_signal_offset(hits, min_duration=15, max_gap=15)
        self.assertEqual(result, 115)

    def test_duplicate_hits(self):
        """重複 hits 去重。"""
        hits = [100, 105, 105, 110, 115]
        result = _confirmed_signal_offset(hits, min_duration=15, max_gap=15)
        self.assertEqual(result, 115)

    def test_exactly_min_duration(self):
        """剛好等於 min_duration 應 confirmed（>= 不 >）。"""
        hits = [100, 115]   # 15s 持續，max_gap=15 ✓
        result = _confirmed_signal_offset(hits, min_duration=15, max_gap=15)
        self.assertEqual(result, 115)

    def test_gap_exactly_max_gap_same_group(self):
        """gap = max_gap 視為同 group（<=，不是 <）。"""
        hits = [100, 115, 130]   # gap 都 = 15
        result = _confirmed_signal_offset(hits, min_duration=15, max_gap=15)
        self.assertEqual(result, 130)

    def test_realistic_end_graph_burst(self):
        """模擬實測：end_graph 持續 30 秒，每 7.5 秒一個 hit。"""
        hits = [3000.0, 3007.5, 3015.0, 3022.5, 3030.0]   # 30s 持續
        result = _confirmed_signal_offset(hits, min_duration=15, max_gap=15)
        self.assertEqual(result, 3030.0)

    def test_realistic_replay_false_positive_rejected(self):
        """模擬實測：replay 短暫一閃 (5s) 不應觸發切片。"""
        hits = [3000.0, 3005.0]   # 5s 不夠
        self.assertIsNone(_confirmed_signal_offset(hits, min_duration=15, max_gap=15))


class DurationCacheTests(unittest.TestCase):
    def test_unchanged_segment_is_probed_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            segment = Path(temp_dir) / "part.ts"
            segment.write_bytes(b"segment")
            worker = LiveSplitWorker(games_output_root=Path(temp_dir))

            with patch(
                "automation.workers.live_split_worker.ffmpeg_utils.ffprobe_duration",
                return_value=12.5,
            ) as probe:
                self.assertEqual(worker._duration_for(segment), 12.5)
                self.assertEqual(worker._duration_for(segment), 12.5)

            probe.assert_called_once_with(segment)


if __name__ == "__main__":
    unittest.main(verbosity=2)

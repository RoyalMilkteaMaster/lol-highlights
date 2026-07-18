"""title_parser + broadcast_mapper 的 unit test（10 個真實 / 變化標題情境）。

跑法：
    python -m pytest automation/tests/test_broadcast_mapper.py -v
或：
    python -m unittest automation.tests.test_broadcast_mapper
"""

from __future__ import annotations

import unittest

from automation.transformers.broadcast_mapper import (
    map_broadcast_to_series,
)
from automation.transformers.title_parser import extract_teams_from_title
from automation.transformers.types import BroadcastDraft


# ─────────────────────────────────────────────────────────────────────────────
LCK_TEAMS = {"T1", "GEN", "HLE", "DK", "DRX", "KT", "BRO", "DNS", "NS", "FOX"}
LPL_TEAMS = {"JDG", "BLG", "T1JD", "TT", "RA", "WBG", "TES", "EDG", "FPX", "RNG", "WE", "IG"}
LCP_TEAMS = {"SHC", "CFO", "GAM", "TSW", "PSG", "FK", "BBR", "DTN", "FRK", "TGRD"}


def make_draft(title: str, league: str = "LCK") -> BroadcastDraft:
    """建一個假 BroadcastDraft（給 mapper 測試用）。"""
    tz_map = {"LCK": "Asia/Seoul", "LPL": "Asia/Shanghai", "LCP": "Asia/Taipei"}
    return BroadcastDraft(
        platform="youtube",
        external_id="dummy_video_id",
        league_code=league,
        league_timezone=tz_map.get(league, "UTC"),
        url="https://example",
        title=title,
    )


def make_series(series_id: int, team_a: str, team_b: str) -> dict:
    """建一個假 series row（給 mapper 測試用）。"""
    return {
        "series_id":   series_id,
        "team_a_code": team_a,
        "team_b_code": team_b,
    }


# ─────────────────────────────────────────────────────────────────────────────
class TitleParserTests(unittest.TestCase):
    """extract_teams_from_title 邏輯。"""

    def test_two_pairs(self):
        teams = extract_teams_from_title(
            "T1 vs GEN, HLE vs DK | 2026 LCK Spring",
            LCK_TEAMS,
        )
        self.assertEqual(teams, ["T1", "GEN", "HLE", "DK"])

    def test_single_pair(self):
        teams = extract_teams_from_title("GEN vs T1 | Match of the Week | LCK", LCK_TEAMS)
        self.assertEqual(teams, ["GEN", "T1"])

    def test_lpl_chinese(self):
        teams = extract_teams_from_title("英雄聯盟 LPL春季賽 JDG vs BLG", LPL_TEAMS)
        self.assertEqual(teams, ["JDG", "BLG"])

    def test_no_teams(self):
        teams = extract_teams_from_title("LCK Spring Split Round 2", LCK_TEAMS)
        self.assertEqual(teams, [])

    def test_avoid_random_word(self):
        # "OF" 是 of 的大寫，不該被誤抓
        # "AT" 也是；只抓白名單內的真實 team code
        teams = extract_teams_from_title("Match of the Year at LCK", LCK_TEAMS)
        self.assertEqual(teams, [])

    def test_three_pairs(self):
        teams = extract_teams_from_title(
            "2026 LCK Spring | T1 vs GEN | HLE vs DK | KT vs DRX",
            LCK_TEAMS,
        )
        self.assertEqual(teams, ["T1", "GEN", "HLE", "DK", "KT", "DRX"])

    def test_dedup_preserves_order(self):
        teams = extract_teams_from_title("T1 prep vs T1 finals | LCK", LCK_TEAMS)
        # 同隊出現兩次只回一次
        self.assertEqual(teams, ["T1"])


# ─────────────────────────────────────────────────────────────────────────────
class BroadcastMapperTests(unittest.TestCase):
    """map_broadcast_to_series：confidence 分級。"""

    def test_high_two_pairs(self):
        """標題 2 隊 + series 對得上 → high。"""
        draft = make_draft("T1 vs GEN, HLE vs DK | 2026 LCK Spring")
        series = [
            make_series(1001, "T1", "GEN"),
            make_series(1002, "HLE", "DK"),
        ]
        result = map_broadcast_to_series(draft, series, LCK_TEAMS)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].confidence, "high")
        self.assertEqual(result[0].series_id, 1001)
        self.assertEqual(result[1].series_id, 1002)
        self.assertEqual(result[0].series_order, 1)
        self.assertEqual(result[1].series_order, 2)

    def test_high_single_pair(self):
        draft = make_draft("JDG vs BLG | 2026 LPL Spring Day 3", "LPL")
        series = [make_series(2001, "JDG", "BLG")]
        result = map_broadcast_to_series(draft, series, LPL_TEAMS)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].confidence, "high")
        self.assertEqual(result[0].series_id, 2001)

    def test_high_lcp(self):
        draft = make_draft("SHC vs CFO | LCP", "LCP")
        series = [make_series(3001, "SHC", "CFO")]
        result = map_broadcast_to_series(draft, series, LCP_TEAMS)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].confidence, "high")

    def test_high_chinese_lpl(self):
        draft = make_draft("英雄聯盟 LPL春季賽 JDG vs BLG", "LPL")
        series = [make_series(2001, "JDG", "BLG")]
        result = map_broadcast_to_series(draft, series, LPL_TEAMS)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].confidence, "high")

    def test_high_team_order_swapped(self):
        """series 寫 (T1, GEN)，標題寫 (GEN vs T1) → 仍 high。"""
        draft = make_draft("GEN vs T1 | LCK")
        series = [make_series(1001, "T1", "GEN")]
        result = map_broadcast_to_series(draft, series, LCK_TEAMS)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].confidence, "high")
        self.assertEqual(result[0].series_id, 1001)

    def test_medium_single_team(self):
        """標題只抓到 1 隊 + 該日期僅一場含此隊 → medium。"""
        draft = make_draft("T1 - Match of the Week")
        series = [make_series(1001, "T1", "GEN")]   # 該日期僅一場含 T1
        result = map_broadcast_to_series(draft, series, LCK_TEAMS)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].confidence, "medium")
        self.assertEqual(result[0].series_id, 1001)

    def test_medium_blocked_by_multiple_candidates(self):
        """標題只 1 隊 + 該日期 ≥ 2 場含此隊 → 退到 low（不是 medium）。"""
        draft = make_draft("T1 - Match of the Week")
        series = [
            make_series(1001, "T1", "GEN"),
            make_series(1002, "T1", "DK"),
        ]
        result = map_broadcast_to_series(draft, series, LCK_TEAMS)
        # 不是 medium（候選 ≥ 2）；也不是 low（low 規則是「日期僅 1 場」，這裡有 2 場）
        # 所以應該是 failed（空 list）
        self.assertEqual(result, [])

    def test_low_date_only(self):
        """標題抓不到隊伍 + 該日期僅 1 場 → low。"""
        draft = make_draft("LCK Watch Party")
        series = [make_series(1001, "T1", "GEN")]
        result = map_broadcast_to_series(draft, series, LCK_TEAMS)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].confidence, "low")
        self.assertEqual(result[0].source, "date_only")

    def test_failed_no_match(self):
        """標題抓不到隊伍 + 該日期 ≥ 2 場 → failed。"""
        draft = make_draft("LCK Spring Split Round 2")
        series = [
            make_series(1001, "T1", "GEN"),
            make_series(1002, "HLE", "DK"),
        ]
        result = map_broadcast_to_series(draft, series, LCK_TEAMS)
        self.assertEqual(result, [])

    def test_failed_no_series(self):
        """根本沒 series → 空 list。"""
        draft = make_draft("T1 vs GEN")
        result = map_broadcast_to_series(draft, [], LCK_TEAMS)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()

"""sanitize_filename 單元測試。

跑法：
    python -m unittest automation.tests.test_vod_metadata
"""

from __future__ import annotations

import unittest
from datetime import date

from highlight.utils.vod_metadata import (
    VodMetadata,
    generate_split_filename,
    sanitize_filename,
)


class SanitizeFilenameTests(unittest.TestCase):
    def test_replaces_spaces(self):
        self.assertEqual(sanitize_filename("LCK 2026 g1"), "LCK_2026_g1")

    def test_removes_invalid_chars(self):
        # Windows 非法字元：< > : " / \ | ? *
        self.assertEqual(sanitize_filename('T1?vs:GEN'), "T1vsGEN")
        self.assertEqual(sanitize_filename('a/b\\c|d'), "abcd")

    def test_unicode_chinese(self):
        # 中文應保留
        self.assertEqual(sanitize_filename("英雄聯盟 春季賽"), "英雄聯盟_春季賽")

    def test_max_length(self):
        long = "x" * 300
        result = sanitize_filename(long, max_len=200)
        self.assertEqual(len(result), 200)

    def test_empty_or_only_invalid(self):
        # 全是非法字元 → 回 'untitled'
        self.assertEqual(sanitize_filename("///"), "untitled")
        self.assertEqual(sanitize_filename(""), "untitled")
        self.assertEqual(sanitize_filename(None or ""), "untitled")


class GenerateSplitFilenameTests(unittest.TestCase):
    def test_full_metadata_with_teams(self):
        meta = VodMetadata(league="LCK", match_date=date(2026, 5, 6))
        result = generate_split_filename(meta, 1, ("T1", "GEN"), "fallback")
        self.assertEqual(result, "LCK_20260506_T1vsGEN_g1.mp4")

    def test_full_metadata_no_teams(self):
        meta = VodMetadata(league="LCK", match_date=date(2026, 5, 6))
        result = generate_split_filename(meta, 2, ("", ""), "fallback")
        self.assertEqual(result, "LCK_20260506_g2.mp4")

    def test_fallback_no_metadata(self):
        meta = VodMetadata()
        result = generate_split_filename(meta, 1, ("T1", "GEN"), "LCK_Carry_xxx")
        self.assertEqual(result, "LCK_Carry_xxx_T1vsGEN_g1.mp4")

    def test_fallback_with_special_chars_sanitized(self):
        meta = VodMetadata()
        result = generate_split_filename(meta, 1, ("T1", "GEN"),
                                         "LCK Day:1 W?eek")
        # 空格 → _，特殊字元拿掉
        self.assertEqual(result, "LCK_Day1_Week_T1vsGEN_g1.mp4")


if __name__ == "__main__":
    unittest.main()

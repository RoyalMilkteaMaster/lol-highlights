"""Automation operating window tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from automation.infra.control import automation_status
from automation import scheduler


class AutomationControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.timezone = ZoneInfo("Asia/Taipei")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.pause_path = Path(self.temp_dir.name) / "system_paused.lock"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def status(self, now: datetime, window: dict) -> dict:
        return automation_status(
            now=now,
            config={"automation_window": window},
            pause_path=self.pause_path,
        )

    def test_disabled_window_allows_work(self):
        result = self.status(
            datetime(2026, 7, 20, 3, 0, tzinfo=self.timezone),
            {"enabled": False, "timezone": "Asia/Taipei"},
        )
        self.assertTrue(result["allowed"])

    def test_daytime_window(self):
        window = {
            "enabled": True,
            "timezone": "Asia/Taipei",
            "days": ["mon"],
            "start": "09:00",
            "end": "18:00",
        }
        self.assertTrue(self.status(datetime(2026, 7, 20, 10, 0, tzinfo=self.timezone), window)["allowed"])
        self.assertFalse(self.status(datetime(2026, 7, 20, 20, 0, tzinfo=self.timezone), window)["allowed"])

    def test_overnight_window(self):
        window = {
            "enabled": True,
            "timezone": "Asia/Taipei",
            "days": ["mon"],
            "start": "20:00",
            "end": "02:00",
        }
        self.assertTrue(self.status(datetime(2026, 7, 20, 23, 0, tzinfo=self.timezone), window)["allowed"])
        self.assertTrue(self.status(datetime(2026, 7, 21, 1, 0, tzinfo=self.timezone), window)["allowed"])
        self.assertFalse(self.status(datetime(2026, 7, 21, 3, 0, tzinfo=self.timezone), window)["allowed"])

    def test_manual_pause_overrides_window(self):
        until = datetime(2026, 7, 20, 12, 0, tzinfo=self.timezone)
        self.pause_path.write_text(
            json.dumps({"reason": "maintenance", "until": until.isoformat()}),
            encoding="utf-8",
        )
        result = self.status(
            datetime(2026, 7, 20, 10, 0, tzinfo=self.timezone),
            {"enabled": False, "timezone": "Asia/Taipei"},
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["source"], "manual")

    def test_invalid_timezone_blocks_safely(self):
        result = self.status(
            datetime(2026, 7, 20, 10, 0, tzinfo=self.timezone),
            {"enabled": True, "timezone": "Invalid/Timezone"},
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["source"], "config_error")

    def test_expired_pause_is_ignored_without_mutation(self):
        until = datetime(2026, 7, 20, 9, 0, tzinfo=self.timezone)
        self.pause_path.write_text(json.dumps({"until": until.isoformat()}), encoding="utf-8")
        result = self.status(
            datetime(2026, 7, 20, 10, 0, tzinfo=self.timezone),
            {"enabled": False, "timezone": "Asia/Taipei"},
        )
        self.assertTrue(result["allowed"])
        self.assertTrue(self.pause_path.exists())

    def test_empty_legacy_lock_is_indefinite_pause(self):
        self.pause_path.touch()
        result = self.status(
            datetime(2026, 7, 20, 10, 0, tzinfo=self.timezone),
            {"enabled": False, "timezone": "Asia/Taipei"},
        )
        self.assertFalse(result["allowed"])


class SchedulerPolicyTransitionTests(unittest.TestCase):
    def test_opening_window_recovers_upcoming_recordings(self):
        scheduler._LAST_AUTOMATION_ALLOWED = False
        decision = {
            "allowed": True,
            "source": "window",
            "reason": "inside window",
            "next_change_at": None,
        }
        with (
            patch("automation.scheduler.automation_status", return_value=decision),
            patch("automation.scheduler._periodic_schedule_scrape") as scrape,
            patch("automation.scheduler._daily_find_live") as find_live,
        ):
            scheduler._periodic_policy_refresh(object())

        scrape.assert_called_once()
        find_live.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)

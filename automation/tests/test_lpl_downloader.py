"""LPL retry state transition tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from automation.workers import lpl_downloader
from automation.sources import bilibili_vod_finder


class LplRetryTests(unittest.TestCase):
    def test_ytdlp_command_contains_only_strings(self):
        with patch.object(bilibili_vod_finder, "_get_random_ua", return_value="test-agent"):
            command = bilibili_vod_finder._ytdlp_cmd_base(None, None)

        self.assertTrue(all(isinstance(value, str) for value in command))
        self.assertEqual(command[command.index("--user-agent") + 1], "test-agent")

    def test_failure_below_limit_stays_retryable(self):
        repo = MagicMock()
        repo.record_retry_failure.return_value = 2
        context = MagicMock()
        context.__enter__.return_value = object()

        with (
            patch.object(lpl_downloader, "_load_config", return_value={"scheduler": {"lpl_max_retries": 3}}),
            patch.object(lpl_downloader, "mysql_conn", return_value=context),
            patch.object(lpl_downloader, "BroadcastStateRepo", return_value=repo),
        ):
            lpl_downloader._handle_failure(123, "network error")

        repo.record_retry_failure.assert_called_once_with(123, "network error")
        repo.update_status.assert_not_called()

    def test_failure_at_limit_becomes_terminal(self):
        repo = MagicMock()
        repo.record_retry_failure.return_value = 3
        context = MagicMock()
        context.__enter__.return_value = object()

        with (
            patch.object(lpl_downloader, "_load_config", return_value={"scheduler": {"lpl_max_retries": 3}}),
            patch.object(lpl_downloader, "mysql_conn", return_value=context),
            patch.object(lpl_downloader, "BroadcastStateRepo", return_value=repo),
        ):
            lpl_downloader._handle_failure(123, "network error")

        repo.update_status.assert_called_once_with(
            123,
            "failed",
            error_message="LPL failed after 3 retries: network error",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

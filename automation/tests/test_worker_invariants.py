"""Worker lock lifetime and repository transaction controls."""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from automation.db.repositories import BroadcastGameRepo, ClipJobRepo
from automation.workers import clip_worker


class WorkerLockLifetimeTests(unittest.TestCase):
    def test_named_lock_releases_before_connection_closes(self):
        events: list[str] = []
        connection = object()

        @contextmanager
        def fake_mysql_conn():
            events.append("connection_enter")
            try:
                yield connection
            finally:
                events.append("connection_exit")

        class FakeWorkerLockRepo:
            def __init__(self, conn):
                self.asserted_conn = conn

            def acquire(self, name, timeout_sec=0):
                events.append("acquire")
                return True

            def release(self, name):
                events.append("release")

        with (
            patch.object(clip_worker, "mysql_conn", fake_mysql_conn),
            patch.object(clip_worker, "WorkerLockRepo", FakeWorkerLockRepo),
            clip_worker._held_worker_lock("clip_worker") as acquired,
        ):
            self.assertTrue(acquired)
            self.assertEqual(events, ["connection_enter", "acquire"])

        self.assertEqual(
            events,
            ["connection_enter", "acquire", "release", "connection_exit"],
        )

    def test_unacquired_lock_is_not_released(self):
        connection = object()
        lock = MagicMock()
        lock.acquire.return_value = False

        @contextmanager
        def fake_mysql_conn():
            yield connection

        with (
            patch.object(clip_worker, "mysql_conn", fake_mysql_conn),
            patch.object(clip_worker, "WorkerLockRepo", return_value=lock),
            clip_worker._held_worker_lock("clip_worker") as acquired,
        ):
            self.assertFalse(acquired)

        lock.release.assert_not_called()


class RepositoryTransactionControlTests(unittest.TestCase):
    def test_enqueue_can_join_caller_transaction(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = None
        cursor.lastrowid = 17

        job_id = ClipJobRepo(conn).enqueue_or_reset_failed(9, commit=False)

        self.assertEqual(job_id, 17)
        conn.commit.assert_not_called()

    def test_set_cut_can_join_caller_transaction(self):
        conn = MagicMock()

        BroadcastGameRepo(conn).set_cut(9, "game.mp4", commit=False)

        conn.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

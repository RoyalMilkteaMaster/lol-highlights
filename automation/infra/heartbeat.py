"""背景 thread 包裝：每 30 秒 upsert worker_heartbeats（dashboard 用）。

用法：
    hb = HeartbeatThread('live_split_worker')
    hb.start()
    try:
        ... main loop ...
    finally:
        hb.stop
"""

from __future__ import annotations

import logging
import os
import socket
import threading
from contextlib import suppress

from automation.db.connection import mysql_conn
from automation.db.repositories import WorkerHeartbeatRepo

logger = logging.getLogger(__name__)


class HeartbeatThread(threading.Thread):
    def __init__(
        self,
        worker_name: str,
        *,
        interval_sec: float = 30.0,
        message_callback=None,   # Optional[Callable[[], str]]
    ) -> None:
        super().__init__(daemon=True)
        self.worker_name = worker_name
        self.interval_sec = interval_sec
        self._message_callback = message_callback
        self._pid = os.getpid()
        self._host = socket.gethostname()
        self._stop = threading.Event()
        self._beat_first()

    def _beat_first(self) -> None:
        """init 時馬上發第一發 heartbeat (status='starting')。"""
        try:
            with mysql_conn() as conn:
                WorkerHeartbeatRepo(conn).beat(
                    self.worker_name,
                    pid=self._pid,
                    host=self._host,
                    status="starting",
                    message=None,
                )
        except Exception as e:
            logger.warning("heartbeat 初始化失敗：%s", e)

    def run(self) -> None:
        # 進入 thread 馬上設 running
        self._beat_once(status="running")
        while not self._stop.is_set():
            self._stop.wait(self.interval_sec)
            if self._stop.is_set():
                break
            self._beat_once(status="running")

    def _beat_once(self, *, status: str) -> None:
        msg = None
        if self._message_callback:
            with suppress(Exception):
                msg = self._message_callback()
        try:
            with mysql_conn() as conn:
                WorkerHeartbeatRepo(conn).beat(
                    self.worker_name,
                    pid=self._pid,
                    host=self._host,
                    status=status,
                    message=msg,
                )
        except Exception as e:
            logger.warning("[%s] heartbeat 寫入失敗：%s", self.worker_name, e)

    def stop(self) -> None:
        """停止 + 寫一筆 status='stopping'。"""
        self._stop.set()
        self._beat_once(status="stopping")

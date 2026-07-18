"""Migration 004：worker_heartbeats 表（dashboard 用）。

每個常駐 worker 每 30 秒 upsert 一筆心跳。dashboard 讀此表判斷 worker 是否活著。
- last_heartbeat_at < UTC_NOW - 90s → 視為「掛了」（橘色）
- last_heartbeat_at < UTC_NOW - 300s → 視為「死了」（紅色）
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run(conn) -> None:
    _create_table_if_missing(conn, "worker_heartbeats", """
        worker_name        VARCHAR(64) PRIMARY KEY,
        host               VARCHAR(64) NULL,
        pid                INT NULL,
        last_heartbeat_at  DATETIME NOT NULL,
        status             ENUM('starting','running','idle','stopping','crashed')
                           NOT NULL DEFAULT 'running',
        message            VARCHAR(500) NULL
    """)
    logger.info("Migration 004 完成：worker_heartbeats")


def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,),
        )
        return cur.fetchone() is not None


def _create_table_if_missing(conn, table: str, body: str) -> None:
    if _table_exists(conn, table):
        return
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {table} ({body}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )

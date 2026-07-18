"""Migration 002：錄影狀態機 + clip_jobs queue。

採 Python migration，用 information_schema 確認後才動作，
避免依賴 ALTER TABLE ADD COLUMN IF NOT EXISTS（MySQL 5.x 不支援）。

新增：
- broadcasts 補：auto_clip / recording_status_v2 / recording_started_at /
  recording_ended_at / scheduled_end_utc / last_heartbeat_at /
  retry_count / error_message / raw_segments_dir
- 新表 recording_locks（broadcast_id + pid）
- 新表 clip_jobs（broadcast_id UNIQUE，每場 1 筆）
"""

from __future__ import annotations


def run(conn) -> None:
    """主入口（init_db 動態 import 後呼叫）。"""
    _add_column_if_missing(conn, "broadcasts", "auto_clip",
        "BOOLEAN NOT NULL DEFAULT TRUE")
    _add_column_if_missing(conn, "broadcasts", "recording_status_v2",
        "ENUM('scheduled','waiting_stream','recording','merging','recorded','failed') NULL")
    _add_column_if_missing(conn, "broadcasts", "recording_started_at",
        "DATETIME NULL")
    _add_column_if_missing(conn, "broadcasts", "recording_ended_at",
        "DATETIME NULL")
    _add_column_if_missing(conn, "broadcasts", "scheduled_end_utc",
        "DATETIME NULL")
    _add_column_if_missing(conn, "broadcasts", "last_heartbeat_at",
        "DATETIME NULL")
    _add_column_if_missing(conn, "broadcasts", "retry_count",
        "INT NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "broadcasts", "error_message",
        "VARCHAR(500) NULL")
    _add_column_if_missing(conn, "broadcasts", "raw_segments_dir",
        "VARCHAR(500) NULL")

    _create_table_if_missing(conn, "recording_locks", """
        broadcast_id  BIGINT UNSIGNED PRIMARY KEY,
        started_at    DATETIME NOT NULL,
        pid           INT,
        FOREIGN KEY (broadcast_id) REFERENCES broadcasts(broadcast_id) ON DELETE CASCADE
    """)

    _create_table_if_missing(conn, "clip_jobs", """
        job_id        BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
        broadcast_id  BIGINT UNSIGNED NOT NULL UNIQUE,
        status        ENUM('pending','running','done','failed') NOT NULL DEFAULT 'pending',
        enqueued_at   DATETIME DEFAULT CURRENT_TIMESTAMP(),
        started_at    DATETIME NULL,
        ended_at      DATETIME NULL,
        error_message VARCHAR(500) NULL,
        retry_count   INT NOT NULL DEFAULT 0,
        FOREIGN KEY (broadcast_id) REFERENCES broadcasts(broadcast_id) ON DELETE CASCADE,
        INDEX idx_status (status, enqueued_at)
    """)


def _column_exists(conn, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = %s AND column_name = %s
            """,
            (table, column),
        )
        return cur.fetchone() is not None


def _table_exists(conn, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = DATABASE() AND table_name = %s
            """,
            (table,),
        )
        return cur.fetchone() is not None


def _add_column_if_missing(conn, table: str, column: str, definition: str) -> None:
    if _column_exists(conn, table, column):
        return
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _create_table_if_missing(conn, table: str, body: str) -> None:
    if _table_exists(conn, table):
        return
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {table} ({body}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )

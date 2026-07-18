"""Migration 014：broadcast_games 加 timeline_external_id（Phase D 5/17）。

timeline_anchor service 對映後寫入這個欄位，後續 selection/timeline_validator
可以直接拿到正確的 timeline JSON file（不用再重 cargo query）。

值格式：
  leaguepedia source: 'LOLTMNT01_387874'
  bilibili source:    'BV1AhL361EtY_38382865738'

JSON 檔位置：E:/videos/timelines/<YYYYMMDD>/<source>_<external_id>.json
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run(conn) -> None:
    if _column_exists(conn, "broadcast_games", "timeline_external_id"):
        logger.info("Migration 014：broadcast_games.timeline_external_id 已存在，跳過")
        return
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE broadcast_games ADD COLUMN timeline_external_id VARCHAR(128) NULL "
            "COMMENT 'timeline 對應的 external_id (Leaguepedia RPGId / Bilibili BV_cid)'"
        )
    conn.commit()
    logger.info("Migration 014 完成：broadcast_games 加 timeline_external_id 欄位")


def _column_exists(conn, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s",
            (table, column),
        )
        row = cur.fetchone()
    return (row.get("c") or 0) > 0

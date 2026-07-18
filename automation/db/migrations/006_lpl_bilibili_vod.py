"""Migration 006：LPL 走 Bilibili 官方剪好的場次 VOD。

加：
- broadcasts.platform 加 'bilibili_vod' enum 值
- broadcast_games.start_source 加 'lpl_official' enum 值
- broadcast_games.end_source 加 'lpl_official' enum 值

LPL 不錄直播；schedule_scrape 後建 platform='bilibili_vod' broadcast row，
scheduler 排 lpl_downloader job at match_time + 80min 去 Bilibili 找官方剪好的場次。
"""

from __future__ import annotations
import logging
logger = logging.getLogger(__name__)


def run(conn) -> None:
    # broadcasts.platform: ENUM('youtube','bilibili') → ENUM('youtube','bilibili','bilibili_vod')
    if not _enum_has_value(conn, "broadcasts", "platform", "bilibili_vod"):
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE broadcasts MODIFY COLUMN platform "
                "ENUM('youtube','bilibili','bilibili_vod') NOT NULL"
            )
        logger.info("broadcasts.platform 加 'bilibili_vod' enum 值")

    # broadcast_games.start_source 加 'lpl_official'
    if not _enum_has_value(conn, "broadcast_games", "start_source", "lpl_official"):
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE broadcast_games MODIFY COLUMN start_source "
                "ENUM('bp_detected','manual','lpl_official') NOT NULL DEFAULT 'bp_detected'"
            )
        logger.info("broadcast_games.start_source 加 'lpl_official'")

    # broadcast_games.end_source 加 'lpl_official'
    if not _enum_has_value(conn, "broadcast_games", "end_source", "lpl_official"):
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE broadcast_games MODIFY COLUMN end_source "
                "ENUM('game_end_screen','nexus_explosion','end_graph',"
                "     'next_bp_fallback','stream_end_fallback','manual','lpl_official') NULL"
            )
        logger.info("broadcast_games.end_source 加 'lpl_official'")

    logger.info("Migration 006 完成：LPL bilibili_vod platform")


def _enum_has_value(conn, table: str, column: str, value: str) -> bool:
    """檢查 ENUM 欄位是否已含某 value（idempotent 用）。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_TYPE FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
            (table, column),
        )
        row = cur.fetchone()
    if not row:
        return False
    col_type = row.get("COLUMN_TYPE") or row.get("column_type") or ""
    return f"'{value}'" in str(col_type)

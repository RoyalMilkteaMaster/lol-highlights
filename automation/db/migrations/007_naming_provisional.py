"""Migration 007：broadcast_games 加 naming_provisional + naming_attempts。

針對 LCK 多 series broadcast（如「DK vs KT - BRO vs BFX」雙場直播）的命名問題：
- 切 game 時不知道 game_index 屬哪個 series
- 暫用第一 series team codes 命名 → 標 naming_provisional=true
- cron 每 5 min 重試 refetch lolesports → 拿到比分後 rename + 清旗標
- 最多 4 次重試（共 20 min），達上限放棄

LPL（platform='bilibili_vod'）broadcast 永遠單 series，不會走這個流程。
LCK/LCP 單 series broadcast 也不走（broadcast_series count=1 直接 done）。
"""

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


def run(conn) -> None:
    if not _column_exists(conn, "broadcast_games", "naming_provisional"):
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE broadcast_games "
                "ADD COLUMN naming_provisional BOOLEAN NOT NULL DEFAULT FALSE "
                "  COMMENT 'true=用第一 series team codes 暫命名，待 lolesports 比分更新後 rename'"
            )
        logger.info("broadcast_games 加 naming_provisional 欄位")

    if not _column_exists(conn, "broadcast_games", "naming_attempts"):
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE broadcast_games "
                "ADD COLUMN naming_attempts TINYINT NOT NULL DEFAULT 0 "
                "  COMMENT '已嘗試 refetch lolesports 確認命名的次數（上限 4 次）'"
            )
        logger.info("broadcast_games 加 naming_attempts 欄位")

    # 加 index 給 _periodic_naming_retry cron 撈用
    if not _index_exists(conn, "broadcast_games", "idx_naming_provisional"):
        with conn.cursor() as cur:
            cur.execute(
                "CREATE INDEX idx_naming_provisional ON broadcast_games "
                "  (naming_provisional, naming_attempts)"
            )
        logger.info("broadcast_games 加 idx_naming_provisional index")

    logger.info("Migration 007 完成：naming_provisional + naming_attempts")


def _column_exists(conn, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
            (table, column),
        )
        row = cur.fetchone()
    return (row.get("c") or 0) > 0


def _index_exists(conn, table: str, index_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s",
            (table, index_name),
        )
        row = cur.fetchone()
    return (row.get("c") or 0) > 0

"""Migration 009：recording_interruptions 表。

背景：
streamlink 在直播期間若 watchdog 偵測到 streamlink rc!=0 → 重啟。
重啟前後通常漏錄 30-60s。
這些漏錄的內容無法即時從 HLS DVR 拿回（streamlink --hls-live-restart 跳過歷史 segment），
但直播結束後 30 min YouTube archive VOD 出現 → 可從 archive 用 yt-dlp partial 補回該段。

設計：
- streamlink_recorder.py watchdog restart 時 INSERT 一筆紀錄 (broadcast_id, offset)
- scheduler._periodic_interruption_recovery 每 30 min 撈 recovered=0 + broadcast 已 recorded 的紀錄
- 找出哪場 game 涵蓋該中斷時刻 → spawn recover_broadcast_game --auto --force 重抓 → 重出 highlight

（H23）：把昨天手動 CREATE TABLE 的邏輯寫成正式 migration，
新電腦 / 重 init-db 才會自動建表，否則 streamlink_recorder.py INSERT 永遠失敗（被 except 吞）→
中斷自動補錄機制完全沒救援過。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run(conn) -> None:
    _create_table_if_missing(conn, "recording_interruptions", """
        id                  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        broadcast_id        BIGINT UNSIGNED NOT NULL,
        interrupted_at_sec  FLOAT NOT NULL COMMENT 'broadcast offset 秒（中斷時刻估計）',
        resumed_at_sec      FLOAT NULL COMMENT 'broadcast offset 秒（重啟接續時刻估計）',
        duration_sec        FLOAT NULL COMMENT '中斷時長秒',
        recovered           TINYINT(1) NOT NULL DEFAULT 0
                            COMMENT '是否已從 archive VOD 補錄重出 highlight',
        recovered_at        TIMESTAMP NULL,
        detected_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP(),
        KEY idx_broadcast_id (broadcast_id),
        KEY idx_recovered (recovered, broadcast_id)
    """)
    conn.commit()
    logger.info("Migration 009 完成：recording_interruptions")


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
        logger.info("Migration 009：%s 已存在，跳過", table)
        return
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {table} ({body}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )

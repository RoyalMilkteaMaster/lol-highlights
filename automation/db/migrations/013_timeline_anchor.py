"""Migration 013：broadcast_games 加 timeline 對齊欄位。

發現 YOLO end_graph / nexus_explosion 偵測有 false positive
（WBG g2 在 2000s 截斷，實際 GAME_END=2570s）。

解法：拓 Riot V5 timeline (LCK/LCP via Leaguepedia) + Bilibili view_points (LPL)
拿到權威 GAME_END_ms，配 flash icon anchor 對齊 game.mp4 內 in-game t=0。

新欄位：
- timeline_anchor_sec: flash icon 第一次出現的時間（game.mp4 內秒數），對應 in-game t=0+α
- timeline_game_duration_sec: timeline 給的 GAME_END 秒數
- timeline_source: 'leaguepedia' / 'bilibili' / 'none'
- timeline_verified: 1=已比對，0=未比對
- needs_recut: 1=YOLO 切點跟 timeline 差 > 30s（建議重切，目前 flag 不自動執行）
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run(conn) -> None:
    cols_to_add = [
        ("timeline_anchor_sec",
         "ALTER TABLE broadcast_games ADD COLUMN timeline_anchor_sec FLOAT NULL "
         "COMMENT 'flash icon anchor (game.mp4 内秒数, ≈ in-game t=0+α)'"),
        ("timeline_game_duration_sec",
         "ALTER TABLE broadcast_games ADD COLUMN timeline_game_duration_sec FLOAT NULL "
         "COMMENT 'timeline GAME_END 秒数 (Leaguepedia V5 GAME_END / Bilibili last event)'"),
        ("timeline_source",
         "ALTER TABLE broadcast_games ADD COLUMN timeline_source VARCHAR(20) NULL "
         "COMMENT 'leaguepedia / bilibili / none / unknown'"),
        ("timeline_verified",
         "ALTER TABLE broadcast_games ADD COLUMN timeline_verified TINYINT(1) NOT NULL DEFAULT 0 "
         "COMMENT '1=timeline 比对完成'"),
        ("needs_recut",
         "ALTER TABLE broadcast_games ADD COLUMN needs_recut TINYINT(1) NOT NULL DEFAULT 0 "
         "COMMENT '1=YOLO 切点跟 timeline 差 > 30s, 建议重切'"),
    ]
    for col, sql in cols_to_add:
        if _column_exists(conn, "broadcast_games", col):
            logger.info("Migration 013：broadcast_games.%s 已存在，跳過", col)
            continue
        with conn.cursor() as cur:
            cur.execute(sql)
        logger.info("Migration 013：broadcast_games 加 %s 欄位", col)

    conn.commit()
    logger.info("Migration 013 完成：broadcast_games 加 timeline_* 欄位")


def _column_exists(conn, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM information_schema.columns "
            "WHERE table_schema=DATABASE() AND table_name=%s AND column_name=%s",
            (table, column),
        )
        row = cur.fetchone()
    return (row.get("c") or 0) > 0

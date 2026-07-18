"""Migration 005：broadcast_detector_state 加狀態機欄位。

加：
- detector_phase: ENUM('idle','bp_phase','search_end','cooldown') NOT NULL DEFAULT 'idle'
- phase_anchor_offset_sec: DOUBLE NULL — 進入該 phase 時的 cumulative ts offset（秒）
- current_game_index: INT NOT NULL DEFAULT 1 — 目前在處理第幾場（IDLE 找下一場時 +1）

狀態機說明：
  IDLE         ← 找 BP（只掃 bp_ui）
  BP_PHASE     ← BP 階段 + 早期 game（不掃任何，等 anchor + 20 min）
  SEARCH_END   ← 找結束訊號（掃 end_graph + nexus + game_end_screen，超過 45 min 加掃 bp_ui fallback）
  COOLDOWN     ← 場間休息（不掃任何，等 anchor + 10 min → IDLE）
"""

from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


def run(conn) -> None:
    _add_column_if_missing(conn, "broadcast_detector_state", "detector_phase",
        "ENUM('idle','bp_phase','search_end','cooldown') NOT NULL DEFAULT 'idle'")
    _add_column_if_missing(conn, "broadcast_detector_state", "phase_anchor_offset_sec",
        "DOUBLE NULL")
    _add_column_if_missing(conn, "broadcast_detector_state", "current_game_index",
        "INT NOT NULL DEFAULT 1")
    logger.info("Migration 005 完成：broadcast_detector_state 加狀態機欄位")


def _column_exists(conn, table: str, column: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
            (table, column),
        )
        return cur.fetchone() is not None


def _add_column_if_missing(conn, table: str, column: str, definition: str) -> None:
    if _column_exists(conn, table, column):
        return
    with conn.cursor() as cur:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

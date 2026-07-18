"""Migration 003：邊錄邊切（live game detection）schema。

新增 / 修改：
- 新表 broadcast_games：每場 game 一筆（一個 broadcast 可有多場）
- 新表 broadcast_detector_state：detector 跑的狀態 + 上次 boundaries 快照
- clip_jobs：DROP+REBUILD（broadcast_id UNIQUE → game_id UNIQUE）

跟 002 一樣用 information_schema 檢查，避免依賴 ALTER TABLE IF NOT EXISTS。

評審吸收：
- v8 Blocker 1：broadcast_games 加 end_source 紀錄是 game_end_screen / nexus / end_graph
- v8 Blocker 2：start_offset_sec / end_offset_sec 是相對 cumulative.mp4 的 seconds
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run(conn) -> None:
    """主入口（init_db 動態 import 後呼叫）。"""

    # ── 1) broadcast_games ────────────────────────────────────────────────
    _create_table_if_missing(conn, "broadcast_games", """
        game_id            BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
        broadcast_id       BIGINT UNSIGNED NOT NULL,
        game_index         INT NOT NULL,
        start_offset_sec   DOUBLE NOT NULL,
        end_offset_sec     DOUBLE NULL,
        start_source       ENUM('bp_detected','manual') NOT NULL DEFAULT 'bp_detected',
        end_source         ENUM(
            'game_end_screen', 'nexus_explosion', 'end_graph',
            'next_bp_fallback', 'stream_end_fallback', 'manual'
        ) NULL,
        status             ENUM('detecting','cutting','cut','failed','skipped')
                           NOT NULL DEFAULT 'detecting',
        confidence         FLOAT NULL,
        game_path          VARCHAR(500) NULL,
        series_id          BIGINT UNSIGNED NULL,
        series_order       INT NULL,
        team_a_code        VARCHAR(8) NULL,
        team_b_code        VARCHAR(8) NULL,
        metadata_confidence ENUM('high','medium','low') NULL,
        detected_at        DATETIME NULL,
        cut_at             DATETIME NULL,
        error_message      VARCHAR(500) NULL,
        UNIQUE KEY uk_broadcast_game (broadcast_id, game_index),
        INDEX idx_status (status),
        INDEX idx_broadcast (broadcast_id),
        FOREIGN KEY (broadcast_id) REFERENCES broadcasts(broadcast_id) ON DELETE CASCADE
    """)

    # ── 2) broadcast_detector_state ───────────────────────────────────────
    _create_table_if_missing(conn, "broadcast_detector_state", """
        broadcast_id       BIGINT UNSIGNED PRIMARY KEY,
        detector_status    ENUM('active','finished','failed') NOT NULL DEFAULT 'active',
        last_run_at        DATETIME NULL,
        last_scan_until_sec DOUBLE NULL,
        last_boundaries_json TEXT NULL,
        error_message      VARCHAR(500) NULL,
        FOREIGN KEY (broadcast_id) REFERENCES broadcasts(broadcast_id) ON DELETE CASCADE
    """)

    # ── 3) clip_jobs：broadcast_id → game_id（DROP+REBUILD）───────────────
    # 若已有 game_id 欄位 → 已遷移，跳過
    if _column_exists(conn, "clip_jobs", "game_id"):
        logger.info("clip_jobs.game_id 已存在，跳過")
    else:
        # 警告：drop 會清掉所有 pending / running / done / failed 的舊 row
        # 因為 broadcast-level → game-level 是語意斷層，無法自動轉換
        with conn.cursor() as cur:
            # 先看有沒有 running job（會直接打斷 worker）
            if _table_exists(conn, "clip_jobs"):
                cur.execute("SELECT COUNT(*) AS n FROM clip_jobs WHERE status='running'")
                running = cur.fetchone()["n"]
                if running > 0:
                    raise RuntimeError(
                        f"clip_jobs 有 {running} 個 running job — "
                        "請先停 clip_worker 讓 job 跑完或手動 mark_failed 才能 migrate"
                    )
                logger.warning("DROP TABLE clip_jobs（broadcast-level → game-level 語意斷層）")
                cur.execute("DROP TABLE IF EXISTS clip_jobs")

        _create_table_if_missing(conn, "clip_jobs", """
            job_id        BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
            game_id       BIGINT UNSIGNED NOT NULL UNIQUE,
            status        ENUM('pending','running','done','failed') NOT NULL DEFAULT 'pending',
            enqueued_at   DATETIME DEFAULT CURRENT_TIMESTAMP(),
            started_at    DATETIME NULL,
            ended_at      DATETIME NULL,
            error_message VARCHAR(500) NULL,
            retry_count   INT NOT NULL DEFAULT 0,
            FOREIGN KEY (game_id) REFERENCES broadcast_games(game_id) ON DELETE CASCADE,
            INDEX idx_status (status, enqueued_at)
        """)

    logger.info("Migration 003 完成：broadcast_games + detector_state + clip_jobs(game_id)")


# ── 通用 helpers（複製 002）────────────────────────────────────────────────
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


def _create_table_if_missing(conn, table: str, body: str) -> None:
    if _table_exists(conn, table):
        return
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TABLE {table} ({body}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )

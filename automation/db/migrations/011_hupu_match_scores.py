"""Migration 011：hupu_match_scores cache table + team_aliases mapping table。

背景：
lolesports 賽後 1-3 hr 才更新比分，naming_finalizer 4 retry (20 min) 拿不到比分 → game
永遠卡 naming_provisional=1。設計重構：補一個 hupu 資料源作為 fallback。

5/16 找到虎撲公開 web API（無 sign / 無 cookie）：
    https://match-api.hupu.com/1/8.2.10/matchallapi/bff/standard/getScheduleListByTagForH5
LPL hupu 比 lolesports 快 1-3 hr，LCK 兩者一致，LCP / LCS hupu 不報。

設計（user 確認 A 方案：新 cache table，不混進 broadcast_series）：
- hupu_match_scores：cache raw API response + 解析後欄位
  * raw_json 保留：debug + 未來情緒分析擴充用
  * UPSERT by hupu_match_id：同一 match 重複撈不重複 row
- team_aliases：對應虎撲隊名 → teams.code（GPT review 提醒：GEN/GENG/Gen.G 要對齊）
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run(conn) -> None:
    _create_hupu_match_scores(conn)
    _create_team_aliases(conn)
    conn.commit()
    logger.info("Migration 011 完成：hupu_match_scores + team_aliases")


def _create_hupu_match_scores(conn) -> None:
    if _table_exists(conn, "hupu_match_scores"):
        logger.info("Migration 011：hupu_match_scores 已存在，跳過")
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE hupu_match_scores (
                id                 BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                hupu_match_id      VARCHAR(64) NOT NULL
                                       COMMENT '虎撲內部 match id（UPSERT 唯一鍵）',
                league_code        VARCHAR(8) NULL
                                       COMMENT 'LPL / LCK 等。NULL=hupu intro 無法解析',
                match_date         DATE NOT NULL,
                team_a_name        VARCHAR(64) NOT NULL
                                       COMMENT '虎撲原始隊名（debug alias mismatch）',
                team_b_name        VARCHAR(64) NOT NULL,
                team_a_code        VARCHAR(8) NULL
                                       COMMENT '對齊 teams.code（透過 team_aliases 解析）',
                team_b_code        VARCHAR(8) NULL,
                score_a            TINYINT NOT NULL DEFAULT 0,
                score_b            TINYINT NOT NULL DEFAULT 0,
                series_status      VARCHAR(16) NULL
                                       COMMENT 'COMPLETED / INPROGRESS / NOTSTARTED',
                match_introduction VARCHAR(200) NULL
                                       COMMENT 'hupu 原始 intro 字串',
                raw_json           JSON NULL,
                fetched_at         DATETIME NOT NULL
                                       DEFAULT CURRENT_TIMESTAMP()
                                       ON UPDATE CURRENT_TIMESTAMP(),
                UNIQUE KEY uniq_hupu_match (hupu_match_id),
                INDEX idx_lookup (league_code, match_date),
                INDEX idx_team_pair (team_a_code, team_b_code, match_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
              COMMENT='虎撲 web API 快取 — naming_finalizer fallback 用'
        """)
    logger.info("Migration 011：hupu_match_scores 建立完成")


def _create_team_aliases(conn) -> None:
    if _table_exists(conn, "team_aliases"):
        logger.info("Migration 011：team_aliases 已存在，跳過")
        return
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE team_aliases (
                id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                team_code   VARCHAR(8)  NOT NULL
                                COMMENT '對齊 teams.code (e.g. GEN)',
                alias       VARCHAR(64) NOT NULL
                                COMMENT '別名 (e.g. GENG, Gen.G, 生氏電競)',
                source      VARCHAR(16) NULL
                                COMMENT 'hupu / lolesports / manual',
                created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP(),
                UNIQUE KEY uniq_alias (team_code, alias),
                INDEX idx_alias (alias)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
              COMMENT='隊伍別名對應（hupu 隊名→teams.code 模糊 match 用）'
        """)
    logger.info("Migration 011：team_aliases 建立完成")


def _table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS c FROM information_schema.tables "
            "WHERE table_schema=DATABASE() AND table_name=%s",
            (table_name,),
        )
        row = cur.fetchone()
    return (row.get("c") or 0) > 0

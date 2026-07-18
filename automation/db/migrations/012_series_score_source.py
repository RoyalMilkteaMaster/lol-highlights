"""Migration 012：series 加 score_source 欄位（user 確認 hupu 為準）。

背景：
user 明確指示「除了 LCP 以外，比分以虎撲為準」。LCK / LPL 都優先信虎撲，避免 lolesports
慢更新覆蓋 hupu 已給的正確值。

設計：
- score_source ENUM('lolesports', 'hupu') DEFAULT 'lolesports'
- lolesports refetch 走 SeriesRepo._update_existing → 看 score_source='hupu' 就不動
  score_a / score_b / status / winner_team_id（保留其他欄位更新）
- hupu fallback (SeriesRepo.update_score_only) → 設 score_source='hupu' 鎖死

LCP：hupu 不報 LCP，永遠 score_source 保留 'lolesports'，lolesports 仍然可正常更新。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SHOW COLUMNS FROM series LIKE 'score_source'")
        if cur.fetchone():
            logger.info("Migration 012：series.score_source 已存在，跳過")
            return
        cur.execute(
            "ALTER TABLE series ADD COLUMN score_source ENUM('lolesports','hupu') "
            "  NOT NULL DEFAULT 'lolesports' "
            "  COMMENT '5/17 起：標哪個來源寫了 score。hupu 鎖死後 lolesports 不再覆蓋'"
        )
    conn.commit()
    logger.info("Migration 012 完成：series 加 score_source 欄位")

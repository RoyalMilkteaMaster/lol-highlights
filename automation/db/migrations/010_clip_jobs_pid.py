"""Migration 010：clip_jobs 加 pid 欄位。

背景：
dashboard 的 emergency kill 要能精準對「被 kill 的 main.py pid」reset 對應 clip_job
而不是粗暴對「全部 status='running'」reset（會誤動其他跑中 job）。
clip_worker spawn main.py 後立刻寫 pid 到 DB，dashboard kill 時用 WHERE pid=X 精準 reset。

設計：
- pid INT NULL（main.py 未跑時為 NULL）
- clip_worker 寫入時機：subprocess.Popen 後立刻 UPDATE
- clip_worker 清除時機：main.py 跑完（成功 / 失敗 / timeout）都在 finally clear pid=NULL
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def run(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SHOW COLUMNS FROM clip_jobs LIKE 'pid'")
        if cur.fetchone():
            logger.info("Migration 010：clip_jobs.pid 已存在，跳過")
            return
        cur.execute(
            "ALTER TABLE clip_jobs ADD COLUMN pid INT NULL "
            "COMMENT 'main.py spawn 後寫入；dashboard emergency kill 用'"
        )
    conn.commit()
    logger.info("Migration 010 完成：clip_jobs 加 pid 欄位")

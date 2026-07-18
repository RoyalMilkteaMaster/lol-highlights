"""--reset-broadcast <id>：清 broadcast 整個狀態。

包含：
- recording_status_v2 → NULL
- error_message → NULL
- 強制 release recording_locks
- broadcast_games + clip_jobs 砍掉（CASCADE，讓下次重新切片 + enqueue）
- broadcast_detector_state 砍掉（讓下次重跑 YOLO）
"""

from __future__ import annotations

import logging

from automation.db.connection import mysql_conn
from automation.db.repositories import BroadcastStateRepo, RecordingLockRepo

logger = logging.getLogger(__name__)


def reset_broadcast(broadcast_id: int) -> None:
    with mysql_conn() as conn:
        repo = BroadcastStateRepo(conn)
        broadcast = repo.get_by_id(broadcast_id)
        if not broadcast:
            logger.error("broadcast_id=%s 不存在", broadcast_id)
            return

        # 1. release lock
        RecordingLockRepo(conn).force_release(broadcast_id)

        # 2. reset broadcast 狀態
        repo.reset_status(broadcast_id)

        # 3. 砍 broadcast_games（CASCADE 會帶走它的 clip_jobs，）
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM broadcast_games WHERE broadcast_id=%s",
                (broadcast_id,),
            )
            cur.execute(
                "DELETE FROM broadcast_detector_state WHERE broadcast_id=%s",
                (broadcast_id,),
            )
        conn.commit()

    logger.info(
        "broadcast %s 已完全 reset（status / lock / games / detector_state 都清掉）",
        broadcast_id,
    )

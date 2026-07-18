"""--retry-record <id>：直接呼叫 record_broadcast 補錄。

不靠 scheduler 補錄（避免 hours_back 範圍漏掉）。

atomic reset：除了 reset broadcast status，還要清 detector_state +
raw_dir 內的 _cumulative.mp4 / _yolo_events_cache.json，避免上次失敗的 stale state
污染這次重錄的偵測（之前 5/7 連續清 3 次才清乾淨的事故根因）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from automation.db.connection import mysql_conn
from automation.db.repositories import (
    BroadcastStateRepo,
    RecordingLockRepo,
)
from automation.recorders.streamlink_recorder import record_broadcast

logger = logging.getLogger(__name__)

from highlight.utils import paths as _paths
_DEFAULT_OUTPUT_BASE = _paths.live_recordings_dir()


def _clean_stale_yolo_state(broadcast: dict) -> None:
    """清 raw_dir 內的 cumulative.mp4 / yolo cache（live_split 跨場污染來源）。

    保留 .ts 段（要接續錄影編號）；只清重 concat / yolo 用的衍生檔。
    """
    raw_dir_str = broadcast.get("raw_segments_dir")
    if not raw_dir_str:
        return
    raw_dir = Path(raw_dir_str)
    if not raw_dir.is_dir():
        return

    for name in ("_cumulative.mp4", "_yolo_events_cache.json"):
        p = raw_dir / name
        if p.is_file():
            try:
                p.unlink()
                logger.info("已刪 stale state：%s", p)
            except OSError as e:
                logger.warning("刪 %s 失敗：%s", p, e)


def _reset_detector_state(broadcast_id: int) -> None:
    """把 broadcast_detector_state 重置回 idle (anchor=0, scan_until=0)。

    若 row 不存在則 init（live_split 下次掃時也會 get_or_init，這裡先 atomic 確保乾淨）。
    """
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcast_detector_state SET "
                "  detector_phase='idle', phase_anchor_offset_sec=0.0, "
                "  current_game_index=1, last_scan_until_sec=0, "
                "  last_boundaries_json=NULL, last_run_at=NULL, "
                "  error_message=NULL "
                "WHERE broadcast_id=%s",
                (broadcast_id,),
            )
        conn.commit()
    logger.info("broadcast %s detector_state 已 reset 回 idle", broadcast_id)


def retry_record(broadcast_id: int, output_dir: Path | None = None) -> Path | None:
    """重置狀態（atomic 4 步）+ 直接呼叫 record_broadcast。

    Atomic reset 步驟：
      1. force_release lock
      2. reset broadcast status + retry_count++
      3. 刪 raw_dir 內的 _cumulative.mp4 + _yolo_events_cache.json
      4. UPDATE broadcast_detector_state SET phase='idle', anchor=0 ...

    保留 .ts 段（segment_start_number 會接續編號），保留 broadcast_games / clip_jobs
    舊紀錄。如要全部砍掉重來請用 --reset-broadcast（hard mode）。
    """
    with mysql_conn() as conn:
        repo = BroadcastStateRepo(conn)
        broadcast = repo.get_by_id(broadcast_id)
        if not broadcast:
            logger.error("broadcast_id=%s 不存在", broadcast_id)
            return None

        # 1. 強制清 lock
        RecordingLockRepo(conn).force_release(broadcast_id)

        # 2. reset broadcast status + retry_count++
        repo.reset_status(broadcast_id)
        repo.increment_retry_count(broadcast_id)
        logger.info(
            "broadcast %s 已重置（retry_count=%s+1），開始 atomic reset 殘留 state",
            broadcast_id, broadcast.get("retry_count", 0),
        )

    # 3. 清 raw_dir 內的 stale state
    _clean_stale_yolo_state(broadcast)

    # 4. reset detector_state
    _reset_detector_state(broadcast_id)

    logger.info(
        "broadcast %s atomic reset 完成（保留 .ts + broadcast_games + clip_jobs），開始補錄",
        broadcast_id,
    )

    out = output_dir or _DEFAULT_OUTPUT_BASE
    return record_broadcast(broadcast_id, out)

"""--merge-segments <broadcast_id>：對指定 broadcast 的 raw_segments_dir 手動合併
。

使用情境：
- recorder 在錄影中被 Ctrl+C → status='failed'，但 .ts 段保留
- 本工具讀 raw_segments_dir 跑 ffmpeg concat → 產出 mp4
- 寫回 broadcasts.recording_path
- user 可手動再用 main.py --from-broadcast <id> 觸發剪輯
"""

from __future__ import annotations

import logging
from pathlib import Path

from automation.db.connection import mysql_conn
from automation.db.repositories import BroadcastStateRepo
from automation.recorders import duration_check, ts_concat

logger = logging.getLogger(__name__)


def merge_segments(broadcast_id: int) -> Path | None:
    """合併 raw_segments_dir 內的 .ts 為 mp4 + 寫回 recording_path。"""
    with mysql_conn() as conn:
        repo = BroadcastStateRepo(conn)
        broadcast = repo.get_by_id(broadcast_id)
        if not broadcast:
            logger.error("broadcast_id=%s 不存在", broadcast_id)
            return None

    raw_dir_str = broadcast.get("raw_segments_dir")
    if not raw_dir_str:
        # 沒設 raw_segments_dir → 嘗試用約定的 live_recordings/ 目錄推
        date_str = broadcast["broadcast_date"].strftime("%Y%m%d")
        prefix = (
            f"{broadcast['league_code']}_{date_str}_"
            f"{broadcast['platform']}_{broadcast['external_id'][:8]}"
        )
        from highlight.utils import paths as _paths
        raw_dir = _paths.live_recordings_dir()
    else:
        raw_dir = Path(raw_dir_str)
        date_str = broadcast["broadcast_date"].strftime("%Y%m%d")
        prefix = (
            f"{broadcast['league_code']}_{date_str}_"
            f"{broadcast['platform']}_{broadcast['external_id'][:8]}"
        )

    if not raw_dir.is_dir():
        logger.error("raw_segments_dir 不存在：%s", raw_dir)
        return None

    ts_files = sorted(raw_dir.glob(f"{prefix}_part_*.ts"))
    if not ts_files:
        logger.error("找不到 .ts 段（prefix=%s, dir=%s）", prefix, raw_dir)
        return None

    logger.info("找到 %d 段 .ts，開始合併", len(ts_files))
    mp4_path = ts_concat.concat_to_mp4(ts_files, raw_dir / f"{prefix}.mp4")

    # 驗證時長
    duration = duration_check.get_duration_sec(mp4_path)
    logger.info("合併完成，duration=%.0fs：%s", duration, mp4_path)

    # 寫回 DB（不改 status，user 自己決定要不要 retry-record / from-broadcast）
    with mysql_conn() as conn:
        BroadcastStateRepo(conn).set_paths(
            broadcast_id,
            recording_path=str(mp4_path.resolve()),
            raw_segments_dir=str(raw_dir.resolve()),
        )

    return mp4_path

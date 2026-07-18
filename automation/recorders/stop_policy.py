"""錄影停止策略。

決定錄影應該何時自動停止：
- 有 scheduled_end_utc → 用它 + 1 小時 buffer
- 沒有 → 從 broadcast_series 推 series 數量 × per_series + buffer
- 都不行 → max_duration_hr 上限
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def compute_stop_time(
    broadcast: dict,
    n_series: int,
    *,
    estimated_hours_per_series: float = 2.0,
    extra_buffer_hours: float = 1.0,
    max_duration_hr: float = 6.0,
) -> datetime:
    """計算錄影應該何時自動停止（timezone-aware UTC）。

    Args:
        broadcast                : broadcasts row dict（有 scheduled_start_utc / scheduled_end_utc）
        n_series                 : 該 broadcast 對應的 series 數量
        estimated_hours_per_series: 每場 series 預估耗時（含 BO 重賽）
        extra_buffer_hours       : 額外 buffer（賽前/賽後/技術暫停）
        max_duration_hr          : 絕對上限（避免無限長）
    """
    start = broadcast.get("scheduled_start_utc")
    if start is None:
        # 沒有預定開始 → 從現在算
        start = datetime.now(timezone.utc)
    elif start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)

    # 1. 有 scheduled_end_utc → 直接用 + 1 小時 buffer
    sched_end = broadcast.get("scheduled_end_utc")
    if sched_end is not None:
        if sched_end.tzinfo is None:
            sched_end = sched_end.replace(tzinfo=timezone.utc)
        candidate = sched_end + timedelta(hours=extra_buffer_hours)
    else:
        # 2. 用 BO 預估
        est_hours = max(1.0, n_series) * estimated_hours_per_series + extra_buffer_hours
        candidate = start + timedelta(hours=est_hours)

    # 3. 不超過絕對上限
    cap = start + timedelta(hours=max_duration_hr)
    return min(candidate, cap)

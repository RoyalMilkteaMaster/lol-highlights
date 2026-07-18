"""NTP 時間漂移檢查。

【鐵則】NTP 失敗只 warn，絕不阻擋系統啟動。
比賽錄不到比時間誤差更嚴重。
"""

from __future__ import annotations

import logging
from typing import Tuple

logger = logging.getLogger(__name__)


def check_time_drift(threshold_sec: float = 30.0) -> Tuple[bool, float]:
    """檢查系統時間 vs NTP server 漂移。

    Returns:
        (within_threshold, drift_seconds)
        - within_threshold: 漂移 < threshold 為 True
        - drift_seconds   : 絕對值（NTP 失敗時回 0.0）
    """
    try:
        import ntplib
        client = ntplib.NTPClient()
        response = client.request("pool.ntp.org", version=3, timeout=5)
        drift = abs(response.offset)
        within = drift < threshold_sec
        if within:
            logger.info("NTP 時間檢查 OK（漂移 %.2fs）", drift)
        else:
            logger.warning(
                "[WARN] 系統時間漂移 %.1fs（> %ss）— 錄影排程時間可能不準",
                drift, threshold_sec,
            )
        return within, drift
    except Exception as e:
        logger.warning("NTP 檢查失敗：%s（繼續執行）", e)
        return True, 0.0

"""磁碟空間檢查。"""

from __future__ import annotations

import shutil
from pathlib import Path


def check_disk_space(path: Path | str, min_gb: float = 5.0) -> bool:
    """檢查指定路徑所在磁碟剩餘空間 >= min_gb。

    用 shutil.disk_usage（Windows / Linux 都支援）。
    """
    p = Path(path) if isinstance(path, str) else path
    # 找一個存在的父目錄（如果指定 path 還沒建）
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        usage = shutil.disk_usage(str(p))
    except OSError:
        return True   # 查不到當沒問題（避免誤殺）
    free_gb = usage.free / (1024 ** 3)
    return free_gb >= min_gb


def free_gb(path: Path | str) -> float:
    """回傳剩餘 GB（給 log 用）。"""
    p = Path(path) if isinstance(path, str) else path
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(str(p)).free / (1024 ** 3)
    except OSError:
        return -1.0

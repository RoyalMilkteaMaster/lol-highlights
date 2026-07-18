"""共用 process 工具 — pid 驗證，避免 Windows pid 重用誤判。

Windows pid 重用：process 死後 pid 可能很快被新 process 拿走。
psutil.pid_exists 只看數字存不存在，遇到 pid 重用會誤判殭屍 pid 為活著。
本 helper 加兩重驗證：
  1. process name 必須包含 'python'（本專案 lol-env 跑的 main.py / worker）
  2. process create_time 不晚於 DB 記錄的 started_at + 60s
"""
from __future__ import annotations

from datetime import datetime, timezone

import psutil


def is_pid_alive_python(pid, started_at: datetime | None = None) -> bool:
    """檢查 pid 還活 + 是 python(.exe) + create_time 不晚於 started_at + 60s。

    Args:
        pid: process id；None / 非數字 → False
        started_at: 該 process 啟動時間（UTC naive datetime，因為 DB 用 UTC_TIMESTAMP()）；
                    None → 跳過 create_time 比對（只看 pid + name）

    Returns:
        True 表示確定還活；False 表示已死或無法判斷（保守）。
    """
    if pid is None:
        return False
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    try:
        if not psutil.pid_exists(pid_int):
            return False
        p = psutil.Process(pid_int)
        if "python" not in p.name().lower():
            return False
        if started_at is not None:
            create_ts = p.create_time()                          # UNIX UTC timestamp
            # 重要：DB 的 started_at 是 naive UTC（因為用 UTC_TIMESTAMP()）。
            # naive datetime.timestamp() 會把它當 local 時區解讀 → TW UTC+8 偏 8 hr。
            # 必須明確標 UTC tzinfo 再算 timestamp，才跟 psutil 的 UTC ts 對齊。
            started_ts = started_at.replace(tzinfo=timezone.utc).timestamp()
            # 300s tolerance：真實 pid 重用要分鐘級別才發生（Windows pid 池夠大）。
            # 60s 太緊（測試難寫 + 正常 clip_worker 啟動延遲時就誤判）。
            if create_ts > started_ts + 300:
                return False
        return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

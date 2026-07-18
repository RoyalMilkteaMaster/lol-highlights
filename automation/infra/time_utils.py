"""UTC 時間 helpers。

【鐵則】
- DB DATETIME 欄位一律存 UTC（naive datetime，但語意是 UTC）
- 程式內部用 timezone-aware UTC（datetime.now(timezone.utc)）
- 禁止使用 MySQL `NOW()` 或 Python `datetime.now()`（無 tzinfo）
- 寫入 DB 用 `to_utc_naive(utc_now())`；讀出來用 `from_db_utc` 補 tzinfo
"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """timezone-aware UTC 現在時間。"""
    return datetime.now(timezone.utc)


def to_utc_naive(dt: datetime) -> datetime:
    """timezone-aware datetime → naive UTC（給 MySQL DATETIME 寫入用）。

    若已是 naive 假設它原本就是 UTC，直接回傳。
    若是 aware，先轉成 UTC 再去掉 tzinfo。
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def from_db_utc(dt: datetime | None) -> datetime | None:
    """DB 讀出的 naive datetime → timezone-aware UTC。

    讀回來的 DATETIME 是 naive，依鐵則「DB 一律 UTC」直接補 UTC tzinfo。
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=timezone.utc)

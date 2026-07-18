"""從直播標題抓出隊伍縮寫清單。

支援的標題格式（部分例子）：
- "T1 vs GEN, HLE vs DK | 2026 LCK Spring"     → [T1, GEN, HLE, DK]
- "GEN vs T1 | Match of the Week | LCK"        → [GEN, T1]
- "JDG vs BLG | LPL Spring Day 3"               → [JDG, BLG]
- "英雄聯盟 LPL春季賽 JDG vs BLG"               → [JDG, BLG]
- "LCK Watch Party"                              → []
- "T1 - Match of the Week"                       → [T1]

設計：
- 用 DB teams.code 白名單比對 — 只認「真實隊伍縮寫」，避免誤抓單字（OF / AT / IS）
- 大小寫不敏感
- 中英混合標題都支援
"""

from __future__ import annotations

import re
from typing import Iterable

# 「隊伍縮寫」的可能字元：英數，2~5 字（例如 T1, GEN, HLE, BLG, KT, JDG）
# 邊界用非字母數字（含中文標點）做 split
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]+")


def extract_teams_from_title(
    title: str,
    team_codes_whitelist: Iterable[str],
) -> list[str]:
    """從標題抓出隊伍縮寫，依出現順序回傳（去重保序）。

    Args:
        title: 直播標題
        team_codes_whitelist: 已知的隊伍 code 集合（來自 DB teams.code）

    Returns:
        list of team codes（大寫），按出現順序、已去重
    """
    if not title or not team_codes_whitelist:
        return []

    whitelist = {c.upper() for c in team_codes_whitelist if c}
    if not whitelist:
        return []

    found: list[str] = []
    seen: set[str] = set()

    for match in _TOKEN_PATTERN.finditer(title):
        token = match.group(0).upper()
        if token in whitelist and token not in seen:
            found.append(token)
            seen.add(token)

    return found

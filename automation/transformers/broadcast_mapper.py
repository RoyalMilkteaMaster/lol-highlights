"""把 BroadcastDraft 對應到 series 表的 row（含 confidence）。

對應規則（confidence 由高到低）：
- high   : 標題抓到「兩隊以上 + 日期吻合」 → 對應到唯一 series
- medium : 標題抓到「一隊 + 日期吻合 + 該日期僅一場含此隊」
- low    : 只靠日期推測（標題對不上、有多場）
- failed : 完全對不上 → broadcasts row 仍寫，broadcast_series 留空（user 手動補）

寫入策略（在 pipeline 端處理）：
- high / medium / low → 寫 broadcast_series + 對應 confidence
- failed → 不寫 broadcast_series

【ChatGPT #10 + #11】mapping_confidence + mapping_source + mapping_reason 寫進 DB，給 debug 用。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from automation.transformers.title_parser import extract_teams_from_title
from automation.transformers.types import BroadcastDraft


@dataclass
class BroadcastMapping:
    """單一 broadcast → series 的對應結果。"""
    series_id:           int
    series_order:        int
    confidence:          str    # 'high' / 'medium' / 'low' / 'failed'
    source:              str    # 'title_pair+date' / 'title_single+unique_date' / 'date_only'
    reason:              str    # 給 debug 看的文字
    team_a_code:         str = ""
    team_b_code:         str = ""


# ─────────────────────────────────────────────────────────────────────────────
def map_broadcast_to_series(
    broadcast: BroadcastDraft,
    series_rows_for_date: list[dict],     # 同一個 broadcast_date + league 下的所有 series
    team_codes_whitelist: Iterable[str],  # 該 league 的 team codes
) -> list[BroadcastMapping]:
    """主流程：對應 broadcast 到 series。

    Args:
        broadcast              : BroadcastDraft（含 title 與 broadcast_date_local）
        series_rows_for_date   : 該日期該聯賽的 series 候選（list of dict）
                                 每個 dict 至少要有：series_id, team_a_code, team_b_code
        team_codes_whitelist   : 該 league 的合法 team code 清單

    Returns:
        list of BroadcastMapping（順序 = series_order）

    note:
        若標題抓到的隊伍與 series 完全對不上 → 回 [] (failed)
    """
    if not series_rows_for_date:
        return []

    teams_in_title = extract_teams_from_title(broadcast.title, team_codes_whitelist)

    # ── 規則 1: high — 標題抓到 ≥2 隊，兩兩配對對應 series ─────────────
    if len(teams_in_title) >= 2:
        mappings = _map_by_title_pairs(teams_in_title, series_rows_for_date)
        if mappings:
            return mappings

    # ── 規則 2: medium — 標題只抓到 1 隊 + 該日期僅一場含此隊 ──────────
    if len(teams_in_title) == 1:
        single_team = teams_in_title[0]
        candidates = [
            s for s in series_rows_for_date
            if s["team_a_code"] == single_team or s["team_b_code"] == single_team
        ]
        if len(candidates) == 1:
            s = candidates[0]
            return [BroadcastMapping(
                series_id=s["series_id"],
                series_order=1,
                confidence="medium",
                source="title_single+unique_date",
                reason=f"title 只抓到 {single_team}，該日期僅一場含此隊",
                team_a_code=s["team_a_code"],
                team_b_code=s["team_b_code"],
            )]

    # ── 規則 3: low — 只靠日期推測（候選正好 1 場才標 low）──────────────
    if len(series_rows_for_date) == 1:
        s = series_rows_for_date[0]
        return [BroadcastMapping(
            series_id=s["series_id"],
            series_order=1,
            confidence="low",
            source="date_only",
            reason=f"title 抓不到隊伍（{teams_in_title}），但該日期僅一場 series",
            team_a_code=s["team_a_code"],
            team_b_code=s["team_b_code"],
        )]

    # ── 規則 4: failed — 對不上 ──────────────────────────────────────────
    return []


# ─────────────────────────────────────────────────────────────────────────────
def _map_by_title_pairs(
    teams_in_title: list[str],
    series_rows: list[dict],
) -> list[BroadcastMapping]:
    """兩兩配對，對應到 series 候選。

    例：teams=[T1, GEN, HLE, DK]
        → 嘗試 (T1, GEN) → 找 series；(HLE, DK) → 找 series；最後依 series_order 排
    """
    consumed: set[int] = set()     # 已配對的 series_id
    mappings: list[BroadcastMapping] = []

    # 兩兩配對：[T1, GEN, HLE, DK] → (T1,GEN), (HLE,DK)
    pairs: list[tuple[str, str]] = []
    for i in range(0, len(teams_in_title) - 1, 2):
        pairs.append((teams_in_title[i], teams_in_title[i + 1]))

    order = 1
    for a, b in pairs:
        match = _find_series_by_pair(a, b, series_rows, consumed)
        if match is None:
            continue
        consumed.add(match["series_id"])
        mappings.append(BroadcastMapping(
            series_id=match["series_id"],
            series_order=order,
            confidence="high",
            source="title_pair+date",
            reason=f"title pair ({a}, {b}) 對應 series_id={match['series_id']}",
            team_a_code=match["team_a_code"],
            team_b_code=match["team_b_code"],
        ))
        order += 1

    return mappings


def _find_series_by_pair(
    a: str, b: str, series_rows: list[dict], consumed: set[int],
) -> dict | None:
    """在 series_rows 中找 (a, b) 對戰（不論誰先誰後），跳過已配過的 series_id。"""
    A, B = a.upper(), b.upper()
    for s in series_rows:
        if s["series_id"] in consumed:
            continue
        ta = (s["team_a_code"] or "").upper()
        tb = (s["team_b_code"] or "").upper()
        if {ta, tb} == {A, B}:
            return s
    return None

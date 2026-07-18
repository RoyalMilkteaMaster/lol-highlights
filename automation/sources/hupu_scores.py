"""虎撲 LoL 賽程／比分 fetcher。

公開 web API（pilot 已驗證，5/17）：
    GET https://match-api.hupu.com/1/8.2.10/matchallapi/bff/standard/getScheduleListByTagForH5
    params:
      businessId    = 'lol'
      tab           = '1'
      businessType  = 'common'
      datasource    = 'navigation'
      scheduleName  = '英雄联盟赛事'  (UTF-8 simplified Chinese)

回傳 JSON 結構（要欄位）：
    result.dayGameData[]              # 每天一個 entry
        dayTime: '2026-05-16'
        dateBlock: '5月16日 周六'
        matchData[]:                  # 該天比賽列
            matchId: '1398612681670656'    # hupu 內部 match id（v1 拿來當 UPSERT 唯一鍵）
            matchStatus: 'COMPLETED' | 'INPROGRESS' | 'NOTSTARTED' | 'CANCELLED' …
            matchStartDate: '2026-05-16'
            matchIntroduction: 'LCK联赛第一轮-第二轮' / 'LPL第二赛段组内赛'
            againstInfo.memberInfos[2]:
                memberName: 'GEN'/'T1'/'EDG'/'TT' …  # 對齊 teams.code
                memberBaseScore: '1' | '2' | ...  # series 累計勝場
                memberLogo: ...
            subMatchBusinessType: 'lol'

LoL only — hupu API 不報 LCP / LCS（v1 限定 LCK / LPL，user 確認）。

設計：
- 只解析 + 回 dataclass list，**不寫 DB**
- DB 寫入留給 repositories.upsert_hupu_match_score

CLI：
    python -m automation.sources.hupu_scores --date 2026-05-16
    python -m automation.sources.hupu_scores --date 2026-05-16 --league LCK
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from automation.sources._hupu_common import fetch_json

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
ENDPOINT = (
    "https://match-api.hupu.com/1/8.2.10/matchallapi/bff/standard"
    "/getScheduleListByTagForH5"
)

DEFAULT_PARAMS = {
    "businessId": "lol",
    "tab": "1",
    "businessType": "common",
    "datasource": "navigation",
    "scheduleName": "英雄联盟赛事",
}

# matchIntroduction → league_code mapping
# hupu 中文比賽名稱開頭規律比對
_LEAGUE_PREFIX = [
    ("LCK", "LCK"),
    ("LPL", "LPL"),
    ("MSI", "MSI"),
    ("S赛", "WCS"),     # 全球总决赛
    ("世界赛", "WCS"),
    ("德甲", "PRM"),     # PRM (虎撲可能不用這詞，保留)
]


# ─────────────────────────────────────────────────────────────────────────────
# Data class
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class HupuMatch:
    """一場虎撲對位（series 概念，bo3/bo5 整體）。"""

    hupu_match_id: str
    league_code: str | None         # 'LCK' / 'LPL' / None（解析不出）
    match_date: date                # series 日期
    team_a_name: str                # 'GEN'
    team_b_name: str                # 'T1'
    score_a: int
    score_b: int
    status: str                     # 'COMPLETED' / 'INPROGRESS' / 'NOTSTARTED' / ...
    match_introduction: str         # 'LCK联赛第一轮-第二轮' 原文
    raw: dict[str, Any] = field(default_factory=dict)  # 原始 matchData（debug + 之後擴充）


# ─────────────────────────────────────────────────────────────────────────────
# Parsing helpers
# ─────────────────────────────────────────────────────────────────────────────
def parse_league_code(match_introduction: str) -> str | None:
    """從 'LCK联赛第一轮-第二轮' 抓 'LCK'。"""
    if not match_introduction:
        return None
    for prefix, code in _LEAGUE_PREFIX:
        if match_introduction.startswith(prefix):
            return code
    return None


def _parse_score(raw_score: Any) -> int:
    """memberBaseScore 可能是 str('1') / int(1) / None / '-'。"""
    if raw_score in (None, "", "-"):
        return 0
    try:
        return int(raw_score)
    except (TypeError, ValueError):
        logger.warning("無法解析 score=%r 當 int", raw_score)
        return 0


def _parse_match_date(match_data: dict) -> date | None:
    """從 matchStartDate / matchStartTimeStamp 取 date。"""
    d_str = match_data.get("matchStartDate")
    if d_str:
        try:
            return datetime.strptime(d_str, "%Y-%m-%d").date()
        except ValueError:
            pass
    ts = match_data.get("matchStartTimeStamp")
    if ts:
        try:
            return datetime.fromtimestamp(int(ts) / 1000).date()
        except (TypeError, ValueError):
            pass
    return None


def _parse_match(match_data: dict) -> HupuMatch | None:
    """把一筆 matchData 解析成 HupuMatch。失敗回 None（log warning）。"""
    mid = match_data.get("matchId")
    if not mid:
        return None
    if match_data.get("subMatchBusinessType") != "lol":
        return None  # 過濾非 lol 比賽

    intro = match_data.get("matchIntroduction") or ""
    league = parse_league_code(intro)

    members = (match_data.get("againstInfo") or {}).get("memberInfos") or []
    if len(members) < 2:
        logger.debug("match %s 沒有兩隊資訊（against=%s）", mid, members)
        return None

    a, b = members[0], members[1]
    match_date = _parse_match_date(match_data)
    if match_date is None:
        logger.warning("match %s 沒有 match_date，跳過", mid)
        return None

    return HupuMatch(
        hupu_match_id=str(mid),
        league_code=league,
        match_date=match_date,
        team_a_name=str(a.get("memberName") or "").strip(),
        team_b_name=str(b.get("memberName") or "").strip(),
        score_a=_parse_score(a.get("memberBaseScore")),
        score_b=_parse_score(b.get("memberBaseScore")),
        status=str(match_data.get("matchStatus") or ""),
        match_introduction=intro,
        raw=match_data,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
def fetch_schedule() -> list[HupuMatch]:
    """打 hupu API 拿完整 schedule（多日，含 LCK / LPL）。失敗回空 list。"""
    data = fetch_json(ENDPOINT, params=DEFAULT_PARAMS)
    if not data:
        logger.error("hupu fetch_schedule 拿不到資料")
        return []
    if not data.get("success"):
        logger.error("hupu API 回 success=false errorMsg=%s", data.get("errorMsg"))
        return []

    result = data.get("result") or {}
    day_data = result.get("dayGameData") or []
    matches: list[HupuMatch] = []
    for day in day_data:
        for m in day.get("matchData") or []:
            parsed = _parse_match(m)
            if parsed:
                matches.append(parsed)
    logger.info("hupu fetch_schedule 共解析 %d 場 (raw %d 日)", len(matches), len(day_data))
    return matches


def filter_matches(
    matches: list[HupuMatch],
    *,
    match_date: date | None = None,
    league_code: str | None = None,
    status: str | None = None,
) -> list[HupuMatch]:
    """便利篩選器。"""
    result = matches
    if match_date is not None:
        result = [m for m in result if m.match_date == match_date]
    if league_code is not None:
        result = [m for m in result if m.league_code == league_code.upper()]
    if status is not None:
        result = [m for m in result if m.status == status.upper()]
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CLI（單跑驗證用）
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    # Windows console 預設 cp950 不會印簡中，強制 stdout UTF-8
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="虎撲 LoL 賽程／比分 fetcher（單跑驗證用）",
    )
    parser.add_argument(
        "--date",
        type=str,
        help="篩特定日期，格式 YYYY-MM-DD（如 2026-05-16）。預設不篩。",
    )
    parser.add_argument(
        "--league",
        type=str,
        choices=["LCK", "LPL"],
        help="篩特定聯賽（LCK / LPL）。預設不篩。",
    )
    parser.add_argument(
        "--status",
        type=str,
        choices=["COMPLETED", "INPROGRESS", "NOTSTARTED", "CANCELLED"],
        help="篩特定狀態。預設不篩。",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="多 log 輸出（debug 等級）。",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 篩選日期
    match_date: date | None = None
    if args.date:
        try:
            match_date = datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            print(f"日期格式錯誤: {args.date}（要 YYYY-MM-DD）")
            return 2

    print("=== Fetching hupu schedule ===")
    matches = fetch_schedule()
    print(f"  共 {len(matches)} 場 LoL 比賽")

    filtered = filter_matches(
        matches,
        match_date=match_date,
        league_code=args.league,
        status=args.status,
    )
    print(f"  篩選後 {len(filtered)} 場")
    print()

    # 依日期分組印出
    by_date: dict[date, list[HupuMatch]] = {}
    for m in filtered:
        by_date.setdefault(m.match_date, []).append(m)

    for d in sorted(by_date):
        print(f"--- {d} ---")
        for m in by_date[d]:
            league_disp = m.league_code or "???"
            print(
                f"  [{league_disp:5}] {m.team_a_name:6} {m.score_a} : {m.score_b} {m.team_b_name:6}"
                f"  status={m.status:11} mid={m.hupu_match_id}"
                f"  ({m.match_introduction[:30]})"
            )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

"""虎撲比分同步 service。

職責：
    1. 從 hupu_scores.fetch_schedule 拉今日/近期 LoL 比賽
    2. 把 hupu 隊名解析成 teams.code（透過 team_aliases）
    3. UPSERT 進 hupu_match_scores cache table

執行時機：**on-demand**— naming_finalizer 需要 hupu 資料時呼叫
`sync_if_stale`。內建 5 min throttle 防同 cron 連發。

設計理由：
- hupu API 一次回所有日期所有 LoL 比賽（123 場一次），不必分批拉。
- UPSERT by hupu_match_id：重複跑同一場不會生多 row，比分更新會覆寫。
- 隊伍 alias resolve 失敗時 team_a_code/team_b_code 寫 NULL，仍存 raw_json（debug）。
- on-demand 而非 cron：避免空轉（一天沒比賽也每 5 min 打 API）。
"""

from __future__ import annotations

import json
import logging

from automation.db.connection import mysql_conn
from automation.db.repositories import HupuScoreRepo, TeamAliasRepo
from automation.sources.hupu_scores import fetch_schedule

logger = logging.getLogger(__name__)


def _cache_age_minutes() -> float | None:
    """回 hupu_match_scores 中最新 fetched_at 距今分鐘數。空表回 None。"""
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TIMESTAMPDIFF(SECOND, MAX(fetched_at), NOW()) AS age_sec "
                "FROM hupu_match_scores"
            )
            row = cur.fetchone()
    if not row or row.get("age_sec") is None:
        return None
    return row["age_sec"] / 60.0


def sync_if_stale(max_age_minutes: float = 5.0) -> dict:
    """若 cache 比 max_age_minutes 老（或空），重 sync；否則 skip。

    naming_finalizer.apply_hupu_fallback 進入前呼叫此 function 即可。
    Returns:
        {'synced': bool, 'reason': str, 'fetched': N, 'upserted': N, 'unresolved_teams': []}
    """
    age = _cache_age_minutes()
    if age is not None and age < max_age_minutes:
        logger.debug("[hupu_sync] cache 還新（%.1f min < %.0f min），跳過 sync", age, max_age_minutes)
        return {"synced": False, "reason": f"cache_fresh ({age:.1f}min)",
                "fetched": 0, "upserted": 0, "unresolved_teams": []}
    return sync_hupu_scores()


def sync_hupu_scores() -> dict:
    """執行一次完整 sync：fetch → resolve alias → upsert。

    Returns:
        統計 dict：{'fetched': N, 'upserted': N, 'unresolved_teams': [list of names]}
    """
    matches = fetch_schedule()
    if not matches:
        logger.warning("[hupu_sync] fetch_schedule 回 0 場（API 異常或無資料）")
        return {"fetched": 0, "upserted": 0, "unresolved_teams": []}

    upserted = 0
    unresolved: set[str] = set()

    with mysql_conn() as conn:
        alias_repo = TeamAliasRepo(conn)
        hupu_repo = HupuScoreRepo(conn)

        for m in matches:
            team_a_code = alias_repo.resolve(m.team_a_name)
            team_b_code = alias_repo.resolve(m.team_b_name)
            if team_a_code is None:
                unresolved.add(m.team_a_name)
            if team_b_code is None:
                unresolved.add(m.team_b_name)
            try:
                hupu_repo.upsert(
                    hupu_match_id=m.hupu_match_id,
                    league_code=m.league_code,
                    match_date=m.match_date,
                    team_a_name=m.team_a_name,
                    team_b_name=m.team_b_name,
                    team_a_code=team_a_code,
                    team_b_code=team_b_code,
                    score_a=m.score_a,
                    score_b=m.score_b,
                    series_status=m.status,
                    match_introduction=m.match_introduction,
                    raw_json=json.dumps(m.raw, ensure_ascii=False),
                )
                upserted += 1
            except Exception:
                logger.exception(
                    "[hupu_sync] upsert 失敗 match_id=%s %s vs %s",
                    m.hupu_match_id, m.team_a_name, m.team_b_name,
                )

    if unresolved:
        logger.warning(
            "[hupu_sync] %d 個隊名 alias resolve 不到（%s）— 之後手動加 team_aliases",
            len(unresolved),
            ", ".join(sorted(unresolved)[:10]),
        )
    logger.info(
        "[hupu_sync] 完成：fetched=%d upserted=%d unresolved=%d",
        len(matches), upserted, len(unresolved),
    )
    return {
        "synced": True,
        "reason": "fresh_fetch",
        "fetched": len(matches),
        "upserted": upserted,
        "unresolved_teams": sorted(unresolved),
    }


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    result = sync_hupu_scores()
    print(f"\n=== Sync result ===")
    print(f"  fetched:           {result['fetched']}")
    print(f"  upserted:          {result['upserted']}")
    print(f"  unresolved teams:  {len(result['unresolved_teams'])}")
    if result["unresolved_teams"]:
        print(f"    list: {result['unresolved_teams']}")

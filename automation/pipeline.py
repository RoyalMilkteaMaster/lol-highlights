"""主流程編排：來源 → 轉換 → DB 寫入。

設計原則：
- pipeline.py 只負責串接，不包邏輯
- 每階段結果都記錄計數，最後印一行摘要 log（重複跑沒動到一目了然）
- transaction 由 connection.mysql_conn() + 顯式 conn.commit() 控制
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from automation.db.connection import mysql_conn
from automation.db.repositories import (
    BroadcastRepo,
    BroadcastSeriesRepo,
    GameRepo,
    LeagueRepo,
    SeriesRepo,
    TeamRepo,
)
from automation.infra.config import load_config as _load_config
from automation.sources.lolesports import LolEsportsSource

logger = logging.getLogger(__name__)

@dataclass
class ScrapeStats:
    """單次 scrape 的計數摘要（給 log 用）。"""
    leagues_new: int = 0
    leagues_updated: int = 0
    teams_new: int = 0
    teams_updated: int = 0
    series_new: int = 0
    series_updated: int = 0
    games_new: int = 0
    games_updated: int = 0
    skipped: int = 0
    skipped_reasons: list[str] = field(default_factory=list)

    def to_log_lines(self) -> list[str]:
        return [
            "scrape complete:",
            f"  leagues  : +{self.leagues_new} new, ~{self.leagues_updated} updated",
            f"  teams    : +{self.teams_new} new, ~{self.teams_updated} updated",
            f"  series   : +{self.series_new} new, ~{self.series_updated} updated",
            f"  games    : +{self.games_new} new, ~{self.games_updated} updated",
            f"  skipped  : {self.skipped} ({'; '.join(self.skipped_reasons[:3]) or '-'})",
        ]


class ScraperPipeline:
    """爬蟲主流程。"""

    def __init__(self, config: dict | None = None) -> None:
        self._config = _load_config() if config is None else config
        self._source = LolEsportsSource(
            request_interval_sec=float(
                self._config.get("fetch", {}).get("request_interval_sec", 0.8)
            ),
        )
        # 聯賽 code → league_id 對照表
        self._league_id_map: dict[str, int] = {
            k.upper(): int(v)
            for k, v in self._config.get("leagues", {}).items()
        }

    def run(
        self,
        league_codes: list[str],
        days_ahead: int = 14,
        days_back: int = 0,
    ) -> ScrapeStats:
        """執行一輪完整 scrape。

        Args:
            league_codes: 例如 ['LCK','LPL']
            days_ahead  : 抓未來幾天（含當下）
            days_back   : 抓過去幾天（更新比分用，預設 0 = 不抓過去）
        """
        stats = ScrapeStats()

        with mysql_conn() as conn:
            league_repo = LeagueRepo(conn, self._league_id_map)
            team_repo = TeamRepo(conn)
            series_repo = SeriesRepo(conn, team_repo)
            game_repo = GameRepo(conn)

            # ── 1) 聯賽 ─────────────────────────────────────────────────
            self._sync_leagues(league_repo, league_codes, stats)

            # ── 2) 隊伍（每個聯賽各自處理）─────────────────────────────
            for code in league_codes:
                if code.upper() not in self._league_id_map:
                    stats.skipped += 1
                    stats.skipped_reasons.append(f"未知聯賽 {code}")
                    logger.warning("聯賽 %s 不在對照表，已跳過", code)
                    continue
                self._sync_teams(team_repo, code, stats)

            # ── 3) 賽程 ─────────────────────────────────────────────────
            for code in league_codes:
                if code.upper() not in self._league_id_map:
                    continue
                self._sync_schedule(
                    series_repo, game_repo, code, days_ahead, days_back, stats,
                )

            conn.commit()

        # 摘要 log（INFO 等級，重複跑沒動到一目了然）
        for line in stats.to_log_lines():
            logger.info(line)
        return stats

    # ── 三大階段 ────────────────────────────────────────────────────────

    def _sync_leagues(
        self, repo: LeagueRepo, codes: list[str], stats: ScrapeStats,
    ) -> None:
        """同步聯賽資料。"""
        try:
            all_leagues = self._source.fetch_leagues()
        except Exception as e:
            logger.error("拉聯賽失敗：%s", e)
            return

        wanted = {c.upper() for c in codes}
        for league in all_leagues:
            if league["code"] not in wanted:
                continue
            _, is_new = repo.upsert(league)
            if is_new:
                stats.leagues_new += 1
            else:
                stats.leagues_updated += 1

    def _sync_teams(
        self, repo: TeamRepo, league_code: str, stats: ScrapeStats,
    ) -> None:
        """同步指定聯賽的隊伍。"""
        league_id = self._league_id_map[league_code.upper()]
        try:
            teams = self._source.fetch_teams(league_code)
        except Exception as e:
            logger.error("拉 %s 隊伍失敗：%s", league_code, e)
            return

        for team in teams:
            try:
                _, is_new = repo.upsert(team, league_id)
                if is_new:
                    stats.teams_new += 1
                else:
                    stats.teams_updated += 1
            except Exception as e:
                logger.warning("upsert 隊伍 %s 失敗：%s", team.get("code"), e)
                stats.skipped += 1
                stats.skipped_reasons.append(f"team {team.get('code')}")

    def _sync_schedule(
        self,
        series_repo: SeriesRepo,
        game_repo: GameRepo,
        league_code: str,
        days_ahead: int,
        days_back: int,
        stats: ScrapeStats,
    ) -> None:
        """同步指定聯賽的賽程。"""
        league_id = self._league_id_map[league_code.upper()]
        try:
            series_list = self._source.fetch_schedule(
                league_code, days_ahead, days_back,
            )
        except Exception as e:
            logger.error("拉 %s 賽程失敗：%s", league_code, e)
            return

        for s in series_list:
            try:
                series_id, is_new = series_repo.upsert(s, league_id, league_code)
                if series_id is None:
                    stats.skipped += 1
                    continue

                if is_new:
                    stats.series_new += 1
                else:
                    stats.series_updated += 1

                # 為每個 series 預生成 game skeleton
                gn, gu = game_repo.upsert_skeleton(series_id, s["best_of"])
                stats.games_new += gn
                stats.games_updated += gu

            except Exception as e:
                logger.warning(
                    "upsert series %s 失敗：%s", s.get("external_id"), e,
                )
                stats.skipped += 1


    # ────────────────────────────────────────────────────────────────────
    # 直播 URL 偵測
    # ────────────────────────────────────────────────────────────────────
    def find_live(
        self,
        leagues: list[str],
        days_ahead: int = 7,
    ) -> dict:
        """找直播 URL（YouTube + Bilibili），對應到 broadcasts + broadcast_series。

        Args:
            leagues   : 例如 ['LCK', 'LCP', 'LPL']
            days_ahead: YouTube finder 看未來幾天（Bilibili 只看當下）

        Returns:
            dict 含計數摘要（給 log 用）
        """
        # Lazy import 各 finder（不一定每次都需要全部）
        import os

        from automation.sources.youtube_live import YouTubeLiveFinder
        from automation.sources.bilibili_live import BilibiliLiveFinder
        from automation.transformers.broadcast_mapper import map_broadcast_to_series
        from automation.transformers.types import BroadcastDraft

        wanted = {c.upper() for c in leagues}
        stats = {
            "drafts_found":  0,
            "broadcasts_new": 0,
            "broadcasts_updated": 0,
            "mappings_high": 0,
            "mappings_medium": 0,
            "mappings_low": 0,
            "mappings_failed": 0,
        }

        # ── 1) YouTube finder（LCK / LCP）─────────────────────────────────
        yt_drafts: list = []
        yt_cfg = self._config.get("youtube") or {}
        yt_channels = [
            c for c in (yt_cfg.get("channels") or [])
            if c.get("league", "").upper() in wanted and c.get("channel_id")
        ]
        if yt_channels:
            yt = YouTubeLiveFinder(channels=yt_channels, api_key=os.getenv("YOUTUBE_API_KEY"))
            try:
                yt_drafts = yt.fetch(days_ahead=days_ahead)
            except Exception as e:
                logger.warning("YouTube finder 整體失敗：%s", e)
        else:
            logger.info("config.yaml 沒設 YouTube channels（或不在 leagues 範圍），跳過")

        # ── 2) Bilibili finder（LPL）──────────────────────────────────────
        bl_drafts: list = []
        bl_cfg = self._config.get("bilibili") or {}
        bl_rooms = [
            r for r in (bl_cfg.get("rooms") or [])
            if r.get("league", "").upper() in wanted
        ]
        if bl_rooms:
            bl = BilibiliLiveFinder(rooms=bl_rooms)
            try:
                bl_drafts = bl.fetch()
            except Exception as e:
                logger.warning("Bilibili finder 整體失敗：%s", e)
        else:
            logger.info("config.yaml 沒設 Bilibili rooms（或不在 leagues 範圍），跳過")

        all_drafts: list[BroadcastDraft] = list(yt_drafts) + list(bl_drafts)
        stats["drafts_found"] = len(all_drafts)
        logger.info("找到 %d 個直播（YT=%d + BL=%d）",
                    len(all_drafts), len(yt_drafts), len(bl_drafts))

        # 跨 fetch 去重 — 同 (league, broadcast_date, scheduled_hour) 只保留第一筆
        # YouTubeLiveFinder 已按 priority asc 排 channels，所以「第一筆」 = 優先 channel（LCKCarry > LCKglobal）
        # 也 query DB 看是否已存在（之前 cron fetch 留下的 fallback channel broadcast）→ skip 新的
        deduped: list = []
        seen_slot: set = set()
        for draft in all_drafts:
            sched = draft.scheduled_start_utc
            slot_hour = sched.hour if sched else None
            key = (draft.league_code, draft.broadcast_date_local(), slot_hour)
            if key in seen_slot:
                logger.info("[fallback dedupe] %s %s hour=%s 已被 priority 較高的 channel 抓到 → skip %s (channel=%s)",
                            draft.league_code, draft.broadcast_date_local(), slot_hour,
                            draft.platform, draft.channel_id)
                continue
            seen_slot.add(key)
            deduped.append(draft)
        if len(deduped) < len(all_drafts):
            logger.info("[fallback dedupe] %d 筆 drafts → %d 筆（跳過 %d 重複 channel）",
                        len(all_drafts), len(deduped), len(all_drafts) - len(deduped))
        all_drafts = deduped

        if not all_drafts:
            return stats

        # ── 3) 寫 broadcasts + broadcast_series ──────────────────────────
        with mysql_conn() as conn:
            broadcast_repo = BroadcastRepo(conn)
            bs_repo = BroadcastSeriesRepo(conn)

            seen_broadcast_ids: dict[str, set[int]] = {"youtube": set, "bilibili": set}

            for draft in all_drafts:
                league_id = self._league_id_map.get(draft.league_code.upper())
                if league_id is None:
                    logger.warning("draft league=%s 不在對照表，跳過", draft.league_code)
                    continue

                # DB 跨 fetch 去重 — 同 league + 同 date + 同 hour 已有 broadcast 從 _不同_ channel
                # （broadcast_mapper 把多 channel fallback drafts 視為同場直播）→ skip 新的（保留先進的）
                # 不對自己 external_id 跳過（upsert 會處理）
                sched = draft.scheduled_start_utc
                if sched:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT broadcast_id, external_id, channel_id FROM broadcasts "
                            "WHERE league_id=%s AND broadcast_date=%s "
                            "  AND scheduled_start_utc IS NOT NULL "
                            "  AND ABS(TIMESTAMPDIFF(MINUTE, scheduled_start_utc, %s)) < 60 "
                            "  AND external_id != %s "
                            "LIMIT 1",
                            (league_id, draft.broadcast_date_local(), sched, draft.external_id),
                        )
                        dup = cur.fetchone()
                    if dup:
                        logger.info(
                            "[cross-fetch dedupe] %s %s ~%s 已有 broadcast %s (external_id=%s channel=%s) → skip %s",
                            draft.league_code, draft.broadcast_date_local(), sched,
                            dup["broadcast_id"], dup["external_id"][:20],
                            (dup.get("channel_id") or "")[:20], draft.external_id[:20],
                        )
                        continue

                # broadcasts upsert
                broadcast_id, is_new, action = broadcast_repo.upsert_by_external(
                    platform=draft.platform,
                    external_id=draft.external_id,
                    broadcast_date=draft.broadcast_date_local(),
                    league_id=league_id,
                    league_code=draft.league_code,
                    league_timezone=draft.league_timezone,
                    url=draft.url,
                    title=draft.title,
                    channel_id=draft.channel_id,
                    scheduled_start_utc=draft.scheduled_start_utc,
                    source_status=draft.source_status,
                    confidence="medium",   # 由 mapper 結果決定，先給預設
                )
                seen_broadcast_ids[draft.platform].add(broadcast_id)
                if is_new:
                    stats["broadcasts_new"] += 1
                else:
                    stats["broadcasts_updated"] += 1
                conn.commit()

                # mapper 配對 series
                series_rows = self._fetch_series_for_mapper(
                    conn, draft.broadcast_date_local(), league_id,
                )
                team_codes = self._fetch_team_codes_for_league(conn, league_id)

                mappings = map_broadcast_to_series(draft, series_rows, team_codes)
                if not mappings:
                    stats["mappings_failed"] += 1
                else:
                    for m in mappings:
                        bs_repo.add_mapping(
                            broadcast_id=broadcast_id,
                            series_id=m.series_id,
                            series_order=m.series_order,
                            mapping_confidence=m.confidence,
                            mapping_source=m.source,
                            mapping_reason=m.reason,
                        )
                        if m.confidence == "high":
                            stats["mappings_high"] += 1
                        elif m.confidence == "medium":
                            stats["mappings_medium"] += 1
                        else:
                            stats["mappings_low"] += 1
                conn.commit()

            # 把這次掃描沒看到的 live/pending → ended（離線判定）
            for plat, ids in seen_broadcast_ids.items():
                offline = broadcast_repo.mark_offline_if_not_seen(ids, plat)
                if offline:
                    logger.info("%s 平台標記 %d 個 broadcast 為 ended", plat, offline)
            conn.commit()

        for k, v in stats.items():
            logger.info("  %s: %s", k, v)
        return stats

    @staticmethod
    def _fetch_series_for_mapper(conn, match_date, league_id: int) -> list[dict]:
        """給 broadcast_mapper 用：拉指定日期 + 聯賽的 series 候選。"""
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.series_id,
                       ta.code AS team_a_code,
                       tb.code AS team_b_code
                FROM series s
                JOIN teams ta ON ta.team_id = s.team_a_id
                JOIN teams tb ON tb.team_id = s.team_b_id
                WHERE s.match_date = %s AND s.league_id = %s
                ORDER BY s.match_time, s.series_id
                """,
                (match_date, league_id),
            )
            return [dict(r) for r in cur.fetchall()]

    @staticmethod
    def _fetch_team_codes_for_league(conn, league_id: int) -> list[str]:
        """給 title_parser 用：拉指定聯賽的 team codes 白名單。"""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT code FROM teams WHERE league_id=%s", (league_id,),
            )
            return [r["code"] for r in cur.fetchall()]


# ── 模組級 helper ──────────────────────────────────────────────────────────

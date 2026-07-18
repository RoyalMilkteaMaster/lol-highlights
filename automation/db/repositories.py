"""Repository 層：DB 寫入／讀取的唯一進出口。

設計重點：
- 所有寫入走 INSERT ... ON DUPLICATE KEY UPDATE（去重 layer 3）
- series / teams 寫入前先查 external_ids 沿用既有 PK（去重 layer 1）
- daily_seq 用 SELECT ... FOR UPDATE 鎖定，避免同行程競態
- status 狀態機只前進不倒退（已 completed 不會被覆寫回 scheduled）
- 每個 repo 回傳 (new_count, updated_count) 供摘要 log 使用

⚠️ external_ids 是 polymorphic reference，DB 不強制 FK，
   必須由本層保證 entity_type 與 entity_id 對應正確：
   寫 external_ids 前，主表的 row 必須已存在。
"""

from __future__ import annotations

import logging
from datetime import date

import pymysql

from automation.transformers.id_generator import (
    make_game_id,
    make_match_code,
    make_series_id,
)

logger = logging.getLogger(__name__)

# status 優先序（高 → 低），用於「狀態只前進不倒退」邏輯
_STATUS_RANK = {
    "scheduled": 0,
    "live":      1,
    "completed": 2,
    "cancelled": 3,  # cancelled 視為終態，不會被覆寫
}


# ============================================================================
#  ExternalIdRepo — 識別「同一筆」的真依據
# ============================================================================

class ExternalIdRepo:
    """external_ids 表的 CRUD。"""

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def find_internal_id(
        self, entity_type: str, source: str, external_id: str,
    ) -> int | None:
        """查詢內部 PK；找不到回 None。"""
        sql = (
            "SELECT entity_id FROM external_ids "
            "WHERE entity_type=%s AND source=%s AND external_id=%s"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (entity_type, source, external_id))
            row = cur.fetchone()
        return int(row["entity_id"]) if row else None

    def upsert_link(
        self,
        entity_type: str,
        entity_id: int,
        source: str,
        external_id: str,
    ) -> None:
        """建立外部 ID 對應。已存在則無動作（idempotent）。"""
        sql = (
            "INSERT INTO external_ids (entity_type, entity_id, source, external_id) "
            "VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE entity_id=VALUES(entity_id)"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (entity_type, entity_id, source, external_id))


# ============================================================================
#  LeagueRepo
# ============================================================================

class LeagueRepo:
    """leagues 表。league_id 由 config.yaml 對照表決定（不是 AUTO_INCREMENT）。"""

    def __init__(
        self,
        conn: pymysql.connections.Connection,
        league_id_map: dict[str, int],
    ) -> None:
        self._conn = conn
        self._id_map = {k.upper(): v for k, v in league_id_map.items()}
        self._ext_repo = ExternalIdRepo(conn)

    def upsert(self, data: dict) -> tuple[int | None, bool]:
        """寫入聯賽。

        Returns:
            (league_id, is_new)；若 code 不在對照表 → (None, False) 並 log warning。
        """
        code = data.get("code", "").upper()
        league_id = self._id_map.get(code)
        if league_id is None:
            logger.warning("聯賽 code '%s' 不在 config.yaml 對照表，已跳過", code)
            return None, False

        sql = (
            "INSERT INTO leagues (league_id, code, name, region, external_id) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "  name=VALUES(name), region=VALUES(region), external_id=VALUES(external_id)"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                league_id,
                code,
                data.get("name", code),
                data.get("region", ""),
                data.get("external_id", ""),
            ))
            is_new = cur.rowcount == 1  # MySQL：1=insert, 2=update

        # 同步建外部 ID 對應
        ext_id = data.get("external_id")
        if ext_id:
            self._ext_repo.upsert_link("league", league_id, "lolesports", ext_id)

        return league_id, is_new

    def get_id_by_code(self, code: str) -> int | None:
        """從對照表查 league_id（不查 DB）。"""
        return self._id_map.get(code.upper())


# ============================================================================
#  TeamRepo
# ============================================================================

class TeamRepo:
    """teams 表；用 external_id 識別「同一隊」。"""

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn
        self._ext_repo = ExternalIdRepo(conn)

    def upsert(self, data: dict, league_id: int) -> tuple[int, bool]:
        """寫入隊伍；已存在沿用 team_id 走 UPDATE。

        Returns:
            (team_id, is_new)
        """
        ext_id = data.get("external_id", "")
        existing = (
            self._ext_repo.find_internal_id("team", "lolesports", ext_id)
            if ext_id else None
        )

        code = data.get("code", "").upper()
        name = data.get("name", code)
        logo = data.get("logo_url", "")

        if existing:
            # 沿用既有 team_id：UPDATE 隊伍資訊（隊伍可能改名 / 換 logo）
            sql = (
                "UPDATE teams SET code=%s, name=%s, league_id=%s, logo_url=%s "
                "WHERE team_id=%s"
            )
            with self._conn.cursor() as cur:
                cur.execute(sql, (code, name, league_id, logo, existing))
            return existing, False

        # 新增：INSERT ... ON DUPLICATE KEY UPDATE 兜底（避免 race condition 撞 uk_league_code）
        sql = (
            "INSERT INTO teams (code, name, league_id, logo_url) "
            "VALUES (%s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "  name=VALUES(name), logo_url=VALUES(logo_url), team_id=LAST_INSERT_ID(team_id)"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (code, name, league_id, logo))
            new_id = cur.lastrowid
            is_new = cur.rowcount == 1

        if ext_id:
            self._ext_repo.upsert_link("team", new_id, "lolesports", ext_id)
        return new_id, is_new

    def find_id_by_external_id(self, external_id: str) -> int | None:
        """查 team_id；給 series 寫入時做隊伍對應用。"""
        return self._ext_repo.find_internal_id("team", "lolesports", external_id)

    def find_id_by_code(self, league_id: int, code: str) -> int | None:
        """以 (league_id, code) 查 team_id。

        lolesports schedule 的 teams 物件只給 code（沒給 team id），
        所以 series 寫入時用這個方法對應，比 external_id 通用。
        """
        sql = (
            "SELECT team_id FROM teams "
            "WHERE league_id=%s AND code=%s LIMIT 1"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (league_id, code.upper()))
            row = cur.fetchone()
        return int(row["team_id"]) if row else None


# ============================================================================
#  SeriesRepo + GameRepo
# ============================================================================

class SeriesRepo:
    """series 表；6 層去重的核心實作。

    寫入流程：
        1. 查 external_ids 找 series_id
           - 命中 → UPDATE 可變欄位（status / score / vod / time）
           - 未命中 → 走 INSERT 路徑：
             a. SELECT MAX(daily_seq) FOR UPDATE 鎖定當天該聯賽
             b. 用 id_generator 產生 series_id
             c. INSERT series + external_ids
    """

    def __init__(
        self,
        conn: pymysql.connections.Connection,
        team_repo: TeamRepo,
    ) -> None:
        self._conn = conn
        self._team_repo = team_repo
        self._ext_repo = ExternalIdRepo(conn)

    def upsert(
        self,
        data: dict,
        league_id: int,
        league_code: str,
    ) -> tuple[int | None, bool]:
        """寫入系列賽。data 來自 SeriesDict + 已解析的 team_a_id / team_b_id。

        Returns:
            (series_id, is_new)；找不到隊伍時回 (None, False)
        """
        # 1) 找對手 team_id（隊伍應已預先 upsert）
        # lolesports schedule 的 teams 沒有 team id，只有 code，所以用 (league_id, code) 對應
        team_a_id = self._team_repo.find_id_by_code(league_id, data["team_a_code"])
        team_b_id = self._team_repo.find_id_by_code(league_id, data["team_b_code"])
        if not team_a_id or not team_b_id:
            logger.warning(
                "series external_id=%s 找不到對應隊伍 (a=%s/%s, b=%s/%s)，已跳過",
                data["external_id"],
                data["team_a_code"], team_a_id,
                data["team_b_code"], team_b_id,
            )
            return None, False

        # 2) 查 external_ids 沿用 series_id（去重 layer 1）
        ext_id = data["external_id"]
        existing = self._ext_repo.find_internal_id("series", "lolesports", ext_id)

        if existing:
            self._update_existing(existing, data, team_a_id, team_b_id, league_id)
            return existing, False

        # 3) 分配新 series_id（鎖該天該聯賽）
        match_date: date = data["match_date"]
        daily_seq = self._allocate_daily_seq(match_date, league_id)

        new_series_id = make_series_id(match_date, league_id, daily_seq)
        match_code = make_match_code(
            match_date, league_code, daily_seq,
            data["team_a_code"], data["team_b_code"],
        )

        self._insert_new(
            new_series_id, match_code, data, team_a_id, team_b_id, league_id,
        )
        self._ext_repo.upsert_link("series", new_series_id, "lolesports", ext_id)
        return new_series_id, True

    # ── 內部：分配 daily_seq（用 FOR UPDATE 鎖該天該聯賽）─────────────────

    def _allocate_daily_seq(self, match_date: date, league_id: int) -> int:
        """取 max(daily_seq) + 1。"""
        sql = (
            "SELECT series_id FROM series "
            "WHERE match_date=%s AND league_id=%s "
            "ORDER BY series_id DESC LIMIT 1 FOR UPDATE"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (match_date, league_id))
            row = cur.fetchone()

        if not row:
            return 1

        # 從 series_id 倒推 daily_seq（位數 11~13）
        s = str(row["series_id"])
        last_seq = int(s[10:13])
        return last_seq + 1

    # ── 內部：INSERT 與 UPDATE ────────────────────────────────────────────

    def _insert_new(
        self,
        series_id: int,
        match_code: str,
        data: dict,
        team_a_id: int,
        team_b_id: int,
        league_id: int,
    ) -> None:
        winner_id = self._resolve_winner_id(data, team_a_id, team_b_id)
        sql = (
            "INSERT INTO series ("
            "  series_id, match_code, league_id, "
            "  match_date, match_time, timezone, match_datetime_utc, "
            "  team_a_id, team_b_id, best_of, stage, status, "
            "  winner_team_id, score_a, score_b, stream_url, vod_url"
            ") VALUES ("
            "  %s, %s, %s, %s, %s, %s, %s, "
            "  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s"
            ")"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                series_id, match_code, league_id,
                data["match_date"], data["match_time"], data["timezone"],
                data["match_datetime_utc"],
                team_a_id, team_b_id, data["best_of"], data.get("stage", ""),
                data["status"],
                winner_id, data.get("score_a"), data.get("score_b"),
                data.get("stream_url"), data.get("vod_url"),
            ))

    def _update_existing(
        self,
        series_id: int,
        data: dict,
        team_a_id: int,
        team_b_id: int,
        league_id: int,
    ) -> None:
        """只更新可變欄位。status 用「只前進不倒退」邏輯。

        若 score_source='hupu'，不動 score_a / score_b / status / winner_team_id
        （hupu 已寫 → user 指定 hupu 為準，除 LCP 外）。
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT status, score_source FROM series WHERE series_id=%s",
                (series_id,),
            )
            row = cur.fetchone()
        current_status = row["status"] if row else "scheduled"
        # hupu 鎖時不動 score / status
        is_hupu_locked = (row and row.get("score_source") == "hupu")

        new_status = self._pick_status(current_status, data["status"])
        winner_id = self._resolve_winner_id(data, team_a_id, team_b_id)

        if is_hupu_locked:
            sql = (
                "UPDATE series SET "
                "  match_time=%s, timezone=%s, match_datetime_utc=%s, "
                "  team_a_id=%s, team_b_id=%s, best_of=%s, stage=%s, "
                "  stream_url=%s, vod_url=%s "
                "WHERE series_id=%s"
            )
            params = (
                data["match_time"], data["timezone"], data["match_datetime_utc"],
                team_a_id, team_b_id, data["best_of"], data.get("stage", ""),
                data.get("stream_url"), data.get("vod_url"),
                series_id,
            )
        else:
            sql = (
                "UPDATE series SET "
                "  match_time=%s, timezone=%s, match_datetime_utc=%s, "
                "  team_a_id=%s, team_b_id=%s, best_of=%s, stage=%s, status=%s, "
                "  winner_team_id=%s, score_a=%s, score_b=%s, "
                "  stream_url=%s, vod_url=%s "
                "WHERE series_id=%s"
            )
            params = (
                data["match_time"], data["timezone"], data["match_datetime_utc"],
                team_a_id, team_b_id, data["best_of"], data.get("stage", ""),
                new_status,
                winner_id, data.get("score_a"), data.get("score_b"),
                data.get("stream_url"), data.get("vod_url"),
                series_id,
            )
        with self._conn.cursor() as cur:
            cur.execute(sql, params)

    @staticmethod
    def _pick_status(current: str, incoming: str) -> str:
        """status 只前進不倒退：completed > live > scheduled。"""
        cur_rank = _STATUS_RANK.get(current, 0)
        new_rank = _STATUS_RANK.get(incoming, 0)
        return incoming if new_rank > cur_rank else current

    def update_score_only(
        self,
        series_id: int,
        score_a: int,
        score_b: int,
        status: str,
    ) -> bool:
        """只更新 score / status / winner（hupu fallback 用）。

        跟 _update_existing 的差別：不動 match_time / vod_url / stream_url 等欄位
        （hupu 沒這些資訊，避免覆蓋 lolesports 既有值）。

        status 仍走「只前進不倒退」邏輯。winner 從 score 推算。
        回 True：有 row affected；False：series_id 不存在或值跟現在一樣。
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT status, team_a_id, team_b_id FROM series WHERE series_id=%s",
                (series_id,),
            )
            row = cur.fetchone()
        if not row:
            return False
        new_status = self._pick_status(row["status"] or "scheduled", status)
        winner_id = None
        if new_status == "completed":
            if score_a > score_b:
                winner_id = row["team_a_id"]
            elif score_b > score_a:
                winner_id = row["team_b_id"]
        # 標 score_source='hupu' 後 lolesports refetch 不再覆蓋（除 LCP 外）
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE series SET score_a=%s, score_b=%s, status=%s, "
                "  winner_team_id=%s, score_source='hupu' "
                "WHERE series_id=%s",
                (score_a, score_b, new_status, winner_id, series_id),
            )
            affected = cur.rowcount
        return affected > 0

    @staticmethod
    def _resolve_winner_id(
        data: dict, team_a_id: int, team_b_id: int,
    ) -> int | None:
        """根據 winner_team_code 對應到 team_a_id 或 team_b_id。"""
        winner_code = data.get("winner_team_code")
        if not winner_code:
            return None
        if winner_code.upper() == data.get("team_a_code", "").upper():
            return team_a_id
        if winner_code.upper() == data.get("team_b_code", "").upper():
            return team_b_id
        return None


class GameRepo:
    """games 表（用戶要求每局一筆）。

    第一版：lolesports getSchedule 不直接給每局資料，先在 series 寫入後
    依 best_of 預生成 N 個 game row（vod_url / winner / riot_game_id 留空）。
    Phase B 才從 getEventDetails 補實際資料。
    """

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def upsert_skeleton(self, series_id: int, best_of: int) -> tuple[int, int]:
        """為一個 series 預先建立 N 個 game row。

        Returns:
            (new_count, updated_count)
        """
        sql = (
            "INSERT INTO games (game_id, series_id, game_number) "
            "VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE game_id=VALUES(game_id)"
        )
        new_cnt = upd_cnt = 0
        with self._conn.cursor() as cur:
            for n in range(1, best_of + 1):
                game_id = make_game_id(series_id, n)
                cur.execute(sql, (game_id, series_id, n))
                if cur.rowcount == 1:
                    new_cnt += 1
                else:
                    upd_cnt += 1
        return new_cnt, upd_cnt


# ============================================================================
#  BroadcastRepo + BroadcastSeriesRepo
# ============================================================================

class BroadcastRepo:
    """broadcasts 表 — 一個 broadcast = 一個 YouTube live / Bilibili live room 直播。

    跨日特殊邏輯（Gemini #2）：
        對 platform='bilibili' 同 room_id 連續直播跨午夜時，DB 不該建兩筆 row。
        upsert_by_external 對 bilibili 先查 source_status IN ('pending_confirm','live')
        的既有 row，有則 UPDATE（保留原 broadcast_date），無才 INSERT。

    狀態機（Gemini #3）：
        source_status = 'pending_confirm' / 'live' / 'ended' ...
        Bilibili 第一次抓到設 'pending_confirm'，下次跑 --find-live 仍 live → 升 'live'。
        這樣 CLI fetch 不需要 sleep 等二次確認。
    """

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def upsert_by_external(
        self,
        platform: str,
        external_id: str,
        broadcast_date,        # date
        league_id: int,
        league_code: str,
        league_timezone: str,
        url: str,
        title: str,
        channel_id: str | None = None,
        scheduled_start_utc=None,    # datetime | None
        source_status: str = "upcoming",
        confidence: str = "low",
    ) -> tuple[int, bool, str]:
        """寫入或更新 broadcast。

        Returns:
            (broadcast_id, is_new, action)
            action ∈ {'insert', 'update_existing_live', 'update_normal', 'promote_pending'}
        """
        # ── Bilibili 跨日特殊處理（Gemini #2）─────────────────────────────────
        # 同 room_id + source_status='live'/'pending_confirm' → 視為「同一場跨日直播」
        if platform == "bilibili":
            existing_live = self._find_active_bilibili_row(external_id)
            if existing_live:
                # UPDATE 該筆（保留原 broadcast_date 為初始日）
                self._update_existing(
                    existing_live["broadcast_id"],
                    title=title,
                    url=url,
                    source_status=source_status,
                    last_checked=True,
                )
                # 若原狀態是 pending_confirm 且現在仍 live → 升級成 live
                action = "update_existing_live"
                if existing_live["source_status"] == "pending_confirm" and source_status == "live":
                    self.promote_pending_to_live(existing_live["broadcast_id"])
                    action = "promote_pending"
                return existing_live["broadcast_id"], False, action

        # ── 一般路徑（YouTube + Bilibili 首次出現）─────────────────────────
        # 用 (platform, external_id, broadcast_date) 的 UNIQUE KEY upsert
        sql = (
            "INSERT INTO broadcasts ("
            "  platform, external_id, channel_id, league_id, league_code, "
            "  league_timezone, broadcast_date, stream_url, title, "
            "  scheduled_start_utc, source_status, confidence, last_checked_at"
            ") VALUES ("
            "  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()"
            ") ON DUPLICATE KEY UPDATE "
            "  channel_id=VALUES(channel_id), "
            "  stream_url=VALUES(stream_url), "
            "  title=VALUES(title), "
            "  scheduled_start_utc=VALUES(scheduled_start_utc), "
            "  source_status=VALUES(source_status), "
            "  confidence=VALUES(confidence), "
            "  last_checked_at=NOW(), "
            "  broadcast_id=LAST_INSERT_ID(broadcast_id)"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                platform, external_id, channel_id, league_id, league_code,
                league_timezone, broadcast_date, url, title,
                scheduled_start_utc, source_status, confidence,
            ))
            broadcast_id = cur.lastrowid
            is_new = cur.rowcount == 1   # MySQL: 1=insert, 2=update
        return broadcast_id, is_new, ("insert" if is_new else "update_normal")

    def _find_active_bilibili_row(self, external_id: str) -> dict | None:
        """查 platform='bilibili' 且 source_status IN ('pending_confirm','live')
        的既有 row（Gemini #2 跨日邏輯用）。
        """
        sql = (
            "SELECT broadcast_id, broadcast_date, source_status "
            "FROM broadcasts "
            "WHERE platform='bilibili' AND external_id=%s "
            "  AND source_status IN ('pending_confirm','live') "
            "ORDER BY broadcast_date DESC, broadcast_id DESC "
            "LIMIT 1"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (external_id,))
            row = cur.fetchone()
        return row

    def _update_existing(
        self,
        broadcast_id: int,
        *,
        title: str | None = None,
        url: str | None = None,
        source_status: str | None = None,
        last_checked: bool = False,
    ) -> None:
        """更新指定 broadcast 的可變欄位。"""
        fields, values = [], []
        if title is not None:
            fields.append("title=%s"); values.append(title)
        if url is not None:
            fields.append("stream_url=%s"); values.append(url)
        if source_status is not None:
            fields.append("source_status=%s"); values.append(source_status)
        if last_checked:
            fields.append("last_checked_at=NOW()")
        if not fields:
            return
        values.append(broadcast_id)
        sql = f"UPDATE broadcasts SET {', '.join(fields)} WHERE broadcast_id=%s"
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(values))

    def promote_pending_to_live(self, broadcast_id: int) -> None:
        """把 source_status='pending_confirm' 的 broadcast 升級成 'live'（Gemini #3 狀態機）。"""
        sql = (
            "UPDATE broadcasts SET source_status='live' "
            "WHERE broadcast_id=%s AND source_status='pending_confirm'"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (broadcast_id,))

    def get_by_id(self, broadcast_id: int) -> dict | None:
        """以 broadcast_id 查單筆，給 --download 用。"""
        sql = "SELECT * FROM broadcasts WHERE broadcast_id=%s"
        with self._conn.cursor() as cur:
            cur.execute(sql, (broadcast_id,))
            return cur.fetchone()

    def update_recording_path(self, broadcast_id: int, path: str) -> None:
        """寫入 recording_path（給 --download 完成後用）。"""
        sql = (
            "UPDATE broadcasts SET recording_path=%s, recording_status='done' "
            "WHERE broadcast_id=%s"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (path, broadcast_id))

    def mark_offline_if_not_seen(
        self,
        seen_broadcast_ids: set[int],
        platform: str,
    ) -> int:
        """把這次掃描沒看到、但之前是 live/pending_confirm 的 row 升級為 'ended'。

        Returns: 受影響 row 數
        """
        if not seen_broadcast_ids:
            placeholders = "(NULL)"
        else:
            placeholders = "(" + ",".join(str(i) for i in seen_broadcast_ids) + ")"
        sql = (
            f"UPDATE broadcasts SET source_status='ended' "
            f"WHERE platform=%s "
            f"  AND source_status IN ('pending_confirm','live') "
            f"  AND broadcast_id NOT IN {placeholders}"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (platform,))
            return cur.rowcount


class BroadcastSeriesRepo:
    """broadcast_series 多對多 join 表（broadcast ↔ series 對應，含 confidence）。"""

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def add_mapping(
        self,
        broadcast_id: int,
        series_id: int,
        series_order: int,
        mapping_confidence: str,
        mapping_source: str,
        mapping_reason: str = "",
    ) -> bool:
        """寫入或更新 mapping。

        Returns: True=新增，False=既有更新
        """
        sql = (
            "INSERT INTO broadcast_series ("
            "  broadcast_id, series_id, series_order, "
            "  mapping_confidence, mapping_source, mapping_reason"
            ") VALUES (%s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "  series_order=VALUES(series_order), "
            "  mapping_confidence=VALUES(mapping_confidence), "
            "  mapping_source=VALUES(mapping_source), "
            "  mapping_reason=VALUES(mapping_reason)"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                broadcast_id, series_id, series_order,
                mapping_confidence, mapping_source, mapping_reason,
            ))
            return cur.rowcount == 1

    def get_series_for_broadcast(self, broadcast_id: int) -> list[dict]:
        """查一個 broadcast 對應的所有 series（依 series_order 排序）。"""
        sql = (
            "SELECT bs.*, s.match_code, s.team_a_id, s.team_b_id "
            "FROM broadcast_series bs "
            "JOIN series s ON s.series_id = bs.series_id "
            "WHERE bs.broadcast_id=%s "
            "ORDER BY bs.series_order"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (broadcast_id,))
            return list(cur.fetchall())

    def get_broadcast_for_series(self, series_id: int) -> dict | None:
        """查一個 series 對應的 broadcast（理論上 1 對 1，取信心最高那筆）。"""
        sql = (
            "SELECT * FROM broadcast_series "
            "WHERE series_id=%s "
            "ORDER BY FIELD(mapping_confidence,'high','medium','low') "
            "LIMIT 1"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (series_id,))
            return cur.fetchone()


# ============================================================================
#  錄影狀態機 + clip_jobs queue + 各種 lock
# ============================================================================

class BroadcastStateRepo:
    """broadcasts 表的「錄影狀態」操作（方法）。

    跟既有 BroadcastRepo 分開避免互相干擾；既有的 upsert_by_external 等
    保持不動，這裡只新增狀態機相關方法。
    """

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def update_status(
        self,
        broadcast_id: int,
        new_status: str,
        error_message: str | None = None,
    ) -> None:
        """更新 recording_status_v2。"""
        if error_message is not None:
            sql = (
                "UPDATE broadcasts SET recording_status_v2=%s, error_message=%s "
                "WHERE broadcast_id=%s"
            )
            params = (new_status, error_message[:500], broadcast_id)
        else:
            sql = (
                "UPDATE broadcasts SET recording_status_v2=%s "
                "WHERE broadcast_id=%s"
            )
            params = (new_status, broadcast_id)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
        self._conn.commit()

    def update_heartbeat(self, broadcast_id: int) -> None:
        """更新 last_heartbeat_at = UTC_TIMESTAMP()）。"""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcasts SET last_heartbeat_at=UTC_TIMESTAMP() "
                "WHERE broadcast_id=%s",
                (broadcast_id,),
            )
        self._conn.commit()

    def set_recording_started_at(self, broadcast_id: int) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcasts SET recording_started_at=UTC_TIMESTAMP() "
                "WHERE broadcast_id=%s",
                (broadcast_id,),
            )
        self._conn.commit()

    def set_recording_ended_at(self, broadcast_id: int) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcasts SET recording_ended_at=UTC_TIMESTAMP() "
                "WHERE broadcast_id=%s",
                (broadcast_id,),
            )
        self._conn.commit()

    def set_paths(
        self,
        broadcast_id: int,
        *,
        recording_path: str | None = None,
        raw_segments_dir: str | None = None,
    ) -> None:
        """設定錄影輸出路徑。"""
        sets, vals = [], []
        if recording_path is not None:
            sets.append("recording_path=%s"); vals.append(recording_path)
        if raw_segments_dir is not None:
            sets.append("raw_segments_dir=%s"); vals.append(raw_segments_dir)
        if not sets:
            return
        vals.append(broadcast_id)
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE broadcasts SET {', '.join(sets)} WHERE broadcast_id=%s",
                tuple(vals),
            )
        self._conn.commit()

    def reset_status(self, broadcast_id: int) -> None:
        """清狀態給 --retry-record 用。"""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcasts SET recording_status_v2=NULL, "
                "  error_message=NULL, last_heartbeat_at=NULL, "
                "  recording_started_at=NULL, recording_ended_at=NULL "
                "WHERE broadcast_id=%s",
                (broadcast_id,),
            )
        self._conn.commit()

    def increment_retry_count(self, broadcast_id: int) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcasts SET retry_count=retry_count+1 WHERE broadcast_id=%s",
                (broadcast_id,),
            )
        self._conn.commit()

    def record_retry_failure(self, broadcast_id: int, error_message: str) -> int:
        """Atomically record a retry failure and return the new retry count."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcasts SET retry_count=COALESCE(retry_count, 0)+1, "
                "error_message=%s WHERE broadcast_id=%s",
                (error_message[:500], broadcast_id),
            )
            cur.execute(
                "SELECT retry_count FROM broadcasts WHERE broadcast_id=%s",
                (broadcast_id,),
            )
            row = cur.fetchone()
        self._conn.commit()
        return int(row["retry_count"] or 0) if row else 0

    def get_by_id(self, broadcast_id: int) -> dict | None:
        """完整 row（給 recorder / scheduler 用）。"""
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM broadcasts WHERE broadcast_id=%s", (broadcast_id,))
            return cur.fetchone()

    def find_recent_and_upcoming(
        self,
        hours_back: int = 3,
        minutes_ahead: int = 30,
    ) -> list[dict]:
        """掃 NOW()-hours_back ~ NOW()+minutes_ahead 範圍的 broadcast。"""
        sql = """
            SELECT * FROM broadcasts
            WHERE scheduled_start_utc BETWEEN
                  DATE_SUB(UTC_TIMESTAMP(), INTERVAL %s HOUR)
              AND DATE_ADD(UTC_TIMESTAMP(), INTERVAL %s MINUTE)
            ORDER BY scheduled_start_utc
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (hours_back, minutes_ahead))
            return list(cur.fetchall())


class RecordingLockRepo:
    """recording_locks 操作。"""

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def acquire(self, broadcast_id: int, pid: int) -> bool:
        """嘗試取得 lock。已被其他人持有回 False。"""
        try:
            with self._conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO recording_locks (broadcast_id, started_at, pid) "
                    "VALUES (%s, UTC_TIMESTAMP(), %s)",
                    (broadcast_id, pid),
                )
            self._conn.commit()
            return True
        except pymysql.IntegrityError:
            self._conn.rollback()
            return False

    def release(self, broadcast_id: int, pid: int) -> bool:
        """release lock。"""
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM recording_locks "
                "WHERE broadcast_id=%s AND (pid=%s OR pid IS NULL)",
                (broadcast_id, pid),
            )
            affected = cur.rowcount
        self._conn.commit()
        return affected > 0

    def force_release(self, broadcast_id: int) -> None:
        """強制 release（給 stale lock 清理用）。"""
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM recording_locks WHERE broadcast_id=%s",
                (broadcast_id,),
            )
        self._conn.commit()

    def find_all(self) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM recording_locks")
            return list(cur.fetchall())


class ClipJobRepo:
    """clip_jobs queue 操作。

    起改成 game-level（每場 game 一筆 job），不再是 broadcast-level。
    """

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def enqueue_or_reset_failed(self, game_id: int, *, commit: bool = True) -> int:
        """新增 pending job；若已 failed 則 reset 成 pending。

        若已 pending/running/done → 不動，回該 job_id。
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT job_id, status, retry_count FROM clip_jobs WHERE game_id=%s",
                (game_id,),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    "INSERT INTO clip_jobs (game_id, status) VALUES (%s, 'pending')",
                    (game_id,),
                )
                if commit:
                    self._conn.commit()
                return cur.lastrowid

            if row["status"] == "failed":
                cur.execute(
                    "UPDATE clip_jobs SET status='pending', "
                    "  enqueued_at=UTC_TIMESTAMP(), error_message=NULL, "
                    "  retry_count=retry_count+1, started_at=NULL, ended_at=NULL "
                    "WHERE job_id=%s",
                    (row["job_id"],),
                )
                if commit:
                    self._conn.commit()
            return row["job_id"]

    def acquire_next_pending_atomic(self) -> dict | None:
        """has_running + acquire 同 transaction（SELECT FOR UPDATE）。

        若已有 running job 回 None；否則撈最早 pending → 改 running 後回傳。

        修正：D 修法 SQL 過濾 naming_provisional=FALSE 已**拿掉**。
          - 舊：clip_worker 等 finalizer 確認 series 才剪 → 把 finalizer 失敗 (lolesports 延遲) 變成永久死鎖
          - 新：剪輯不依賴 series 名（main.py 只用 game_path），naming_finalizer 改在 rename 前
                check clip_jobs.status='running' 避開 race（見 naming_finalizer.py rename_game_files）
          - 解耦：剪輯先跑、命名後 finalize → 不再阻塞
        """
        try:
            with self._conn.cursor() as cur:
                # 1. 確認沒 running
                cur.execute(
                    "SELECT COUNT(*) AS n FROM clip_jobs WHERE status='running' FOR UPDATE"
                )
                if cur.fetchone()["n"] > 0:
                    self._conn.rollback()
                    return None
                # 2. 撈最早 pending（不再過濾 naming_provisional）
                cur.execute(
                    "SELECT * FROM clip_jobs WHERE status='pending' "
                    "ORDER BY enqueued_at LIMIT 1 FOR UPDATE"
                )
                job = cur.fetchone()
                if not job:
                    self._conn.rollback()
                    return None
                # 3. 改 running
                cur.execute(
                    "UPDATE clip_jobs SET status='running', started_at=UTC_TIMESTAMP() "
                    "WHERE job_id=%s",
                    (job["job_id"],),
                )
            self._conn.commit()
            return job
        except Exception:
            self._conn.rollback()
            raise

    def has_running(self) -> bool:
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM clip_jobs WHERE status='running'")
            return cur.fetchone()["n"] > 0

    def mark_done(self, job_id: int) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE clip_jobs SET status='done', ended_at=UTC_TIMESTAMP() "
                "WHERE job_id=%s",
                (job_id,),
            )
        self._conn.commit()

    def mark_failed(self, job_id: int, error_message: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE clip_jobs SET status='failed', ended_at=UTC_TIMESTAMP(), "
                "  error_message=%s WHERE job_id=%s",
                (error_message[:500], job_id),
            )
        self._conn.commit()

    def find_zombie_candidates(self, stale_sec: int = 60) -> list[dict]:
        """掃 status='running' 但 started_at 比 stale_sec 秒前久的 job。

        回 list of {job_id, game_id, pid, started_at, run_min}。
        呼叫者用 is_pid_alive_python 驗證 pid，死的就 mark_zombie。

        ug A：加 `pid IS NOT NULL` filter — clip_worker D 策略
        wait_to_cut_at+15min 期間 pid 還是 NULL（main.py 還沒 spawn），不能用
        pid alive 判斷，否則 zombie check 會誤殺自己在 wait 的 job。
        pid NULL 但 started_at 過老（譬如 > 60 min）的另外處理（caller 用 run_min cap）。
        """
        with self._conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT job_id, game_id, pid, started_at, "
                "TIMESTAMPDIFF(MINUTE, started_at, UTC_TIMESTAMP()) AS run_min "
                "FROM clip_jobs "
                "WHERE status='running' "
                "  AND started_at IS NOT NULL "
                "  AND pid IS NOT NULL "
                "  AND started_at < UTC_TIMESTAMP() - INTERVAL %s SECOND",
                (stale_sec,),
            )
            return list(cur.fetchall())

    def mark_zombie(self, job_id: int, reason: str,
                    game_id: int | None = None) -> None:
        """zombie 標記 → mark failed + 影片可能不完整時設 broadcast_games.needs_recut=1。

        error_message 前綴 `[ZOMBIE]` 給 watch_progress 識別。
        UPDATE WHERE status='running' 防 race（避免跟 mark_done/mark_failed 衝突）。

        Args:
            job_id: 要標 failed 的 job
            reason: 原因（會 prefix [ZOMBIE]）
            game_id: 若給 → 同時 UPDATE broadcast_games.needs_recut=1
        """
        msg = f"[ZOMBIE] {reason}"[:500]
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE clip_jobs SET status='failed', ended_at=UTC_TIMESTAMP(), "
                "  error_message=%s, pid=NULL "
                "WHERE job_id=%s AND status='running'",
                (msg, job_id),
            )
        if game_id is not None:
            with self._conn.cursor() as cur:
                cur.execute(
                    "UPDATE broadcast_games SET needs_recut=1 WHERE game_id=%s",
                    (game_id,),
                )
        self._conn.commit()


class WorkerLockRepo:
    """MySQL GET_LOCK / RELEASE_LOCK 全域命名鎖。"""

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def acquire(self, name: str, timeout_sec: int = 0) -> bool:
        """0 timeout 表示「拿不到立刻回 false」。"""
        with self._conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, %s) AS v", (name, timeout_sec))
            row = cur.fetchone()
            return row.get("v") == 1

    def release(self, name: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT RELEASE_LOCK(%s)", (name,))


# ============================================================================
#  broadcast_games + detector_state
# ============================================================================

class BroadcastGameRepo:
    """broadcast_games：每場 game 一筆 row（邊錄邊切的核心表）。

    狀態機：
        detecting：boundary 算好但還沒切
        cutting  ：ffmpeg 正在切
        cut      ：切片完成（game_path 已寫）
        failed   ：切片失敗
        skipped  ：手動標記（is_real_game=False 或 admin reset）
    """

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def get_by_index(self, broadcast_id: int, game_index: int) -> dict | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM broadcast_games "
                "WHERE broadcast_id=%s AND game_index=%s",
                (broadcast_id, game_index),
            )
            return cur.fetchone()

    def get_by_id(self, game_id: int) -> dict | None:
        with self._conn.cursor() as cur:
            cur.execute("SELECT * FROM broadcast_games WHERE game_id=%s", (game_id,))
            return cur.fetchone()

    def find_by_broadcast(self, broadcast_id: int) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM broadcast_games WHERE broadcast_id=%s "
                "ORDER BY game_index",
                (broadcast_id,),
            )
            return list(cur.fetchall())

    def insert(
        self,
        *,
        broadcast_id: int,
        game_index: int,
        start_offset_sec: float,
        end_offset_sec: float | None,
        start_source: str = "bp_detected",
        end_source: str | None = None,
        status: str = "detecting",
        confidence: float | None = None,
        series_id: int | None = None,
        series_order: int | None = None,
        team_a_code: str | None = None,
        team_b_code: str | None = None,
        metadata_confidence: str | None = None,
        commit: bool = True,
    ) -> int:
        """新增 broadcast_games row，回傳 game_id。"""
        sql = (
            "INSERT INTO broadcast_games ("
            "  broadcast_id, game_index, start_offset_sec, end_offset_sec, "
            "  start_source, end_source, status, confidence, "
            "  series_id, series_order, team_a_code, team_b_code, metadata_confidence, "
            "  detected_at"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP())"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                broadcast_id, game_index, start_offset_sec, end_offset_sec,
                start_source, end_source, status, confidence,
                series_id, series_order, team_a_code, team_b_code, metadata_confidence,
            ))
            game_id = cur.lastrowid
        if commit:
            self._conn.commit()
        return game_id

    def update_boundary(
        self,
        game_id: int,
        *,
        end_offset_sec: float | None = None,
        end_source: str | None = None,
        confidence: float | None = None,
        commit: bool = True,
    ) -> None:
        """同一場 game 邊界微調（next_bp_fallback → 抓到真 nexus）。"""
        sets, vals = [], []
        if end_offset_sec is not None:
            sets.append("end_offset_sec=%s"); vals.append(end_offset_sec)
        if end_source is not None:
            sets.append("end_source=%s"); vals.append(end_source)
        if confidence is not None:
            sets.append("confidence=%s"); vals.append(confidence)
        if not sets:
            return
        vals.append(game_id)
        with self._conn.cursor() as cur:
            cur.execute(
                f"UPDATE broadcast_games SET {', '.join(sets)} WHERE game_id=%s",
                tuple(vals),
            )
        if commit:
            self._conn.commit()

    def update_status(self, game_id: int, status: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcast_games SET status=%s WHERE game_id=%s",
                (status, game_id),
            )
        self._conn.commit()

    def set_cut(self, game_id: int, game_path: str, *, commit: bool = True) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcast_games SET status='cut', game_path=%s, "
                "  cut_at=UTC_TIMESTAMP() WHERE game_id=%s",
                (game_path, game_id),
            )
        if commit:
            self._conn.commit()

    def mark_failed(self, game_id: int, error_message: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcast_games SET status='failed', error_message=%s "
                "WHERE game_id=%s",
                (error_message[:500], game_id),
            )
        self._conn.commit()

    def set_manual_skip(self, game_id: int, reason: str) -> None:
        """標 [MANUAL_SKIP] 進 error_message → naming_finalizer skip 該 row。

        用於 clip_worker 失敗（FATAL_NO_BP / zombie / timeout）自動標。
        不動 status — cut 出來的 .mp4 還在，user 可手動 re-cut。
        不影響 derive_series_for_game_index 的 cumulative 計數（Option A）。
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcast_games SET error_message=%s WHERE game_id=%s",
                (f"[MANUAL_SKIP] {reason}"[:500], game_id),
            )
        self._conn.commit()

    def reset_to_detecting(self, game_id: int) -> None:
        """admin 用：清掉切好的檔讓 worker 重切。"""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcast_games SET status='detecting', "
                "  game_path=NULL, cut_at=NULL, error_message=NULL "
                "WHERE game_id=%s",
                (game_id,),
            )
        self._conn.commit()


class DetectorStateRepo:
    """broadcast_detector_state：每個 broadcast 偵測進度的快照。

    主要用途：
    - debug：上次 boundaries_json 留下，看 worker 算錯時可重看
    - 結束信號：detector_status='finished' 表示這個 broadcast 沒新場次了
    """

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def get(self, broadcast_id: int) -> dict | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM broadcast_detector_state WHERE broadcast_id=%s",
                (broadcast_id,),
            )
            return cur.fetchone()

    def upsert_run(
        self,
        broadcast_id: int,
        *,
        last_scan_until_sec: float | None = None,
        last_boundaries_json: str | None = None,
    ) -> None:
        """每次 worker 跑完一輪都呼叫；status 預設 active。"""
        sql = (
            "INSERT INTO broadcast_detector_state ("
            "  broadcast_id, detector_status, last_run_at, "
            "  last_scan_until_sec, last_boundaries_json"
            ") VALUES (%s, 'active', UTC_TIMESTAMP(), %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "  last_run_at=UTC_TIMESTAMP(), "
            "  last_scan_until_sec=COALESCE(VALUES(last_scan_until_sec), last_scan_until_sec), "
            "  last_boundaries_json=COALESCE(VALUES(last_boundaries_json), last_boundaries_json)"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (broadcast_id, last_scan_until_sec, last_boundaries_json))
        self._conn.commit()

    def set_finished(self, broadcast_id: int) -> None:
        """recording_status_v2='recorded' 且所有 game 都已切完 → finished。"""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO broadcast_detector_state (broadcast_id, detector_status, last_run_at) "
                "VALUES (%s, 'finished', UTC_TIMESTAMP()) "
                "ON DUPLICATE KEY UPDATE "
                "  detector_status='finished', last_run_at=UTC_TIMESTAMP()",
                (broadcast_id,),
            )
        self._conn.commit()

    def set_failed(self, broadcast_id: int, error_message: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO broadcast_detector_state ("
                "  broadcast_id, detector_status, last_run_at, error_message"
                ") VALUES (%s, 'failed', UTC_TIMESTAMP(), %s) "
                "ON DUPLICATE KEY UPDATE "
                "  detector_status='failed', last_run_at=UTC_TIMESTAMP(), "
                "  error_message=VALUES(error_message)",
                (broadcast_id, error_message[:500]),
            )
        self._conn.commit()

    def find_active_broadcasts(self) -> list[dict]:
        """worker 主迴圈用：撈所有「在錄影 / 已錄完但 detector 還沒 finished」的 broadcast。

        條件：
        - recording_status_v2 IN ('recording', 'merging', 'post_recording', 'recorded')
        - detector_status NOT IN ('finished') 或沒 detector_state row
        - raw_segments_dir 不能 NULL

         'post_recording'：watchdog give-up 時 mark 'post_recording'，
        live_split 必須繼續掃完 cumulative 剩餘部分（不能因為 status 變了就停掃）。
        """
        sql = (
            "SELECT b.* FROM broadcasts b "
            "LEFT JOIN broadcast_detector_state ds ON ds.broadcast_id = b.broadcast_id "
            "WHERE b.recording_status_v2 IN ('recording', 'merging', 'post_recording', 'recorded') "
            "  AND b.raw_segments_dir IS NOT NULL "
            "  AND (ds.detector_status IS NULL OR ds.detector_status = 'active') "
            "ORDER BY b.broadcast_id"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql)
            return list(cur.fetchall())

    # ── 狀態機 helpers ─────────────────────────────────────
    def get_or_init(self, broadcast_id: int) -> dict:
        """讀 detector_state；不存在就 INSERT 預設值（idle）並 return。"""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM broadcast_detector_state WHERE broadcast_id=%s",
                (broadcast_id,),
            )
            row = cur.fetchone()
            if row:
                return row
            # init
            cur.execute(
                "INSERT INTO broadcast_detector_state "
                "(broadcast_id, detector_status, last_run_at, "
                " detector_phase, phase_anchor_offset_sec, current_game_index) "
                "VALUES (%s, 'active', UTC_TIMESTAMP(), 'idle', 0.0, 1)",
                (broadcast_id,),
            )
            self._conn.commit()
            cur.execute(
                "SELECT * FROM broadcast_detector_state WHERE broadcast_id=%s",
                (broadcast_id,),
            )
            return cur.fetchone()

    def update_phase(
        self,
        broadcast_id: int,
        new_phase: str,
        anchor_offset_sec: float,
        *,
        increment_game_index: bool = False,
        last_scan_until_sec: float | None = None,
    ) -> None:
        """狀態機切換：寫 detector_phase / phase_anchor_offset_sec / 視需要 +1 game_index。"""
        if increment_game_index:
            sql = (
                "UPDATE broadcast_detector_state SET "
                "  detector_phase=%s, phase_anchor_offset_sec=%s, "
                "  current_game_index=current_game_index+1, last_run_at=UTC_TIMESTAMP()"
            )
            params = [new_phase, anchor_offset_sec]
        else:
            sql = (
                "UPDATE broadcast_detector_state SET "
                "  detector_phase=%s, phase_anchor_offset_sec=%s, last_run_at=UTC_TIMESTAMP()"
            )
            params = [new_phase, anchor_offset_sec]
        if last_scan_until_sec is not None:
            sql += ", last_scan_until_sec=%s"
            params.append(last_scan_until_sec)
        sql += " WHERE broadcast_id=%s"
        params.append(broadcast_id)
        with self._conn.cursor() as cur:
            cur.execute(sql, tuple(params))
        self._conn.commit()


# ============================================================================
#  worker_heartbeats（dashboard 用）
# ============================================================================

class WorkerHeartbeatRepo:
    """worker_heartbeats：常駐 worker 每 30 秒 upsert 一筆。

    - dashboard 讀此表判斷 worker 是否活著
    - last_heartbeat_at < NOW() - 90s → orange（疑似卡住）
    - last_heartbeat_at < NOW() - 300s → red（死掉）
    """

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def beat(
        self,
        worker_name: str,
        *,
        pid: int | None = None,
        host: str | None = None,
        status: str = "running",
        message: str | None = None,
    ) -> None:
        sql = (
            "INSERT INTO worker_heartbeats "
            "(worker_name, host, pid, last_heartbeat_at, status, message) "
            "VALUES (%s, %s, %s, UTC_TIMESTAMP(), %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "  host=VALUES(host), pid=VALUES(pid), "
            "  last_heartbeat_at=UTC_TIMESTAMP(), "
            "  status=VALUES(status), message=VALUES(message)"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (worker_name, host, pid, status, message))
        self._conn.commit()

    def find_all(self) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT *, "
                "  TIMESTAMPDIFF(SECOND, last_heartbeat_at, UTC_TIMESTAMP()) AS age_sec "
                "FROM worker_heartbeats ORDER BY worker_name"
            )
            return list(cur.fetchall())


# ────────────────────────────────────────────────────────────────────────────
#  hupu 資料源 + 隊伍別名（lolesports 慢更新 fallback）
# ────────────────────────────────────────────────────────────────────────────
class HupuScoreRepo:
    """hupu_match_scores：虎撲 web API 快取表（每 5 min cron 更新）。

    用途：naming_finalizer.refetch_for_broadcast 失敗時拓 hupu 比分救命。
    LCK 通常跟 lolesports 一致；LPL hupu 比 lolesports 快 1-3 hr。
    LCP / LCS hupu 不報 → fallback 仍要靠 lolesports。

    寫入時機：scheduler cron 每 5 min UPSERT 整批；不在請求路徑寫。
    讀取時機：naming_finalizer 嘗試補比分時 find_for_series。
    """

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def upsert(
        self,
        *,
        hupu_match_id: str,
        league_code: str | None,
        match_date,
        team_a_name: str,
        team_b_name: str,
        team_a_code: str | None,
        team_b_code: str | None,
        score_a: int,
        score_b: int,
        series_status: str | None,
        match_introduction: str | None,
        raw_json: str,
    ) -> None:
        """UPSERT 一筆 hupu match。重複跑同 hupu_match_id 會更新比分。"""
        sql = (
            "INSERT INTO hupu_match_scores "
            "(hupu_match_id, league_code, match_date, team_a_name, team_b_name, "
            " team_a_code, team_b_code, score_a, score_b, series_status, "
            " match_introduction, raw_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE "
            "  league_code=VALUES(league_code), "
            "  team_a_code=VALUES(team_a_code), "
            "  team_b_code=VALUES(team_b_code), "
            "  score_a=VALUES(score_a), score_b=VALUES(score_b), "
            "  series_status=VALUES(series_status), "
            "  match_introduction=VALUES(match_introduction), "
            "  raw_json=VALUES(raw_json)"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                hupu_match_id, league_code, match_date, team_a_name, team_b_name,
                team_a_code, team_b_code, score_a, score_b, series_status,
                match_introduction, raw_json,
            ))
        self._conn.commit()

    def find_for_series(
        self,
        *,
        league_code: str,
        match_date,
        team_a_code: str,
        team_b_code: str,
    ) -> dict | None:
        """根據 league + date + team pair 找對應 hupu match。

        雙向匹配：(A vs B) 跟 (B vs A) 都算 match（GPT 提醒，hupu 隊伍順序可能反）。
        允許 match_date ±1 day（時區跨日）。
        回最新 fetched_at 那筆。
        """
        sql = (
            "SELECT * FROM hupu_match_scores "
            "WHERE league_code = %s "
            "  AND match_date BETWEEN DATE_SUB(%s, INTERVAL 1 DAY) "
            "                     AND DATE_ADD(%s, INTERVAL 1 DAY) "
            "  AND ( "
            "       (team_a_code=%s AND team_b_code=%s) "
            "    OR (team_a_code=%s AND team_b_code=%s) "
            "  ) "
            "ORDER BY fetched_at DESC LIMIT 1"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                league_code, match_date, match_date,
                team_a_code, team_b_code,
                team_b_code, team_a_code,
            ))
            row = cur.fetchone()
        if not row:
            return None
        # 標 reversed 給 caller 知道（要不要對調 score）
        row["_reversed"] = (
            row["team_a_code"] == team_b_code
            and row["team_b_code"] == team_a_code
        )
        return row


class TeamAliasRepo:
    """team_aliases：對應虎撲 / lolesports / 民間隊名 → teams.code。

    v1 主要用來解析虎撲隊名。資料表初始為空，遇到 mismatch 才補。
    """

    def __init__(self, conn: pymysql.connections.Connection) -> None:
        self._conn = conn

    def resolve(self, alias: str) -> str | None:
        """別名 → teams.code。先 case-insensitive 比 alias，再 fallback 比 team_code。

        Returns:
            team_code 或 None（找不到對應）
        """
        if not alias:
            return None
        alias = alias.strip()
        with self._conn.cursor() as cur:
            # Priority 1: alias 完整 match（case-insensitive）
            cur.execute(
                "SELECT team_code FROM team_aliases "
                "WHERE LOWER(alias) = LOWER(%s) LIMIT 1",
                (alias,),
            )
            row = cur.fetchone()
            if row:
                return row["team_code"]
            # Priority 2: 看 alias 本身就是有效 teams.code（hupu 通常已經是對的）
            cur.execute(
                "SELECT code FROM teams WHERE LOWER(code) = LOWER(%s) LIMIT 1",
                (alias,),
            )
            row = cur.fetchone()
            if row:
                return row["code"]
        return None

    def add(self, team_code: str, alias: str, source: str = "manual") -> None:
        """新增 alias mapping。重複時忽略。"""
        sql = (
            "INSERT INTO team_aliases (team_code, alias, source) "
            "VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE source=VALUES(source)"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (team_code, alias, source))
        self._conn.commit()

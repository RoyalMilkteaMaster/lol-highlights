"""lolesports.com 非官方 API 來源實作。

設計原則：
- 純 requests + JSON，無需瀏覽器
- 含 timeout / retry / 指數退避 / User-Agent / 請求間 sleep
- 每次成功 fetch 後將 raw response 落地存檔（cache/raw/）
- 失敗時透過 logging 記錄，不直接 print

API endpoint（lolesports 公開 GraphQL gateway）：
- getLeagues
- getTeams
- getSchedule（含 events/matches）

⚠️ 第一版只實作「路線 A：純 requests」，
    路線 B（fake-useragent）與路線 C（selenium）在 config.yaml fetch.strategy
    切換為 'auto' 時才啟用，目前先保留 stub。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from automation.sources.base import (
    AbstractSource,
    LeagueDict,
    SeriesDict,
    TeamDict,
)

logger = logging.getLogger(__name__)

# ── 路徑：以 automation/ 為基準的絕對路徑 ─────────────────────────────────
_PKG_ROOT = Path(__file__).resolve().parent.parent
_CACHE_DIR = _PKG_ROOT / "cache" / "raw"

# lolesports 公開 GraphQL gateway 的固定 API key（前端 JS 中可見）
_DEFAULT_API_KEY = "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"
_DEFAULT_BASE_URL = "https://esports-api.lolesports.com/persisted/gw"

# 一個正常的桌面瀏覽器 UA（避免被當 bot；同行程不變動）
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class LolEsportsSource(AbstractSource):
    """lolesports.com 主來源。

    用法：
        src = LolEsportsSource
        leagues = src.fetch_leagues
        schedule = src.fetch_schedule('LCK', days_ahead=14)
    """

    def __init__(
        self,
        api_key: str = _DEFAULT_API_KEY,
        base_url: str = _DEFAULT_BASE_URL,
        timeout: tuple[float, float] = (3.0, 10.0),
        max_retries: int = 3,
        request_interval_sec: float = 0.8,
        user_agent: str = _DEFAULT_UA,
        hl: str = "en-US",
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._interval = request_interval_sec
        self._hl = hl

        self._session = requests.Session()
        self._session.headers.update({
            "x-api-key": self._api_key,
            "User-Agent": user_agent,
            "Accept": "application/json",
        })

        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # ── 公開 API：實作 AbstractSource ────────────────────────────────────

    def fetch_leagues(self) -> list[LeagueDict]:
        """拉全部聯賽清單。"""
        payload = self._get("/getLeagues", params={"hl": self._hl})
        self._dump_cache("leagues", payload)

        leagues_raw = payload.get("data", {}).get("leagues", [])
        return [self._parse_league(item) for item in leagues_raw]

    def fetch_teams(self, league_code: str) -> list[TeamDict]:
        """拉指定聯賽的隊伍清單。

        lolesports 的 getTeams 是全域 endpoint，必須事後過濾：
        - homeLeague.name == 指定聯賽
        - status == 'active'（排除退役隊伍 / 學院隊）
        """
        payload = self._get("/getTeams", params={"hl": self._hl})
        self._dump_cache(f"teams_{league_code}", payload)

        teams_raw = payload.get("data", {}).get("teams", [])
        result: list[TeamDict] = []
        for item in teams_raw:
            if item.get("status") != "active":
                continue
            home = item.get("homeLeague") or {}
            if home.get("name", "").upper() != league_code.upper():
                continue
            result.append(self._parse_team(item, league_code))
        return result

    def fetch_schedule(
        self, league_code: str, days_ahead: int, days_back: int = 0,
    ) -> list[SeriesDict]:
        """拉指定聯賽 [now - days_back, now + days_ahead] 區間的賽程。

        getSchedule 必須帶 leagueId（lolesports 內部 ID），
        所以先 fetch_leagues 找對應 id。
        """
        # 1) 先取得 lolesports 的 league external_id
        league_ext_id = self._resolve_league_id(league_code)
        if not league_ext_id:
            logger.warning("找不到聯賽 %s 對應的 lolesports league id", league_code)
            return []

        payload = self._get(
            "/getSchedule",
            params={"hl": self._hl, "leagueId": league_ext_id},
        )
        self._dump_cache(f"schedule_{league_code}", payload)

        events = payload.get("data", {}).get("schedule", {}).get("events", [])
        now_utc = datetime.now(timezone.utc)
        cutoff_upper = now_utc + timedelta(days=days_ahead)
        cutoff_lower = now_utc - timedelta(days=days_back)

        result: list[SeriesDict] = []
        for evt in events:
            if evt.get("type") != "match":
                continue

            try:
                series = self._parse_series(
                    evt, league_code, cutoff_upper, cutoff_lower,
                )
            except Exception as e:
                logger.warning("解析 event 失敗，已跳過：%s（%s）", e, evt.get("id"))
                continue

            if series is not None:
                result.append(series)

        return result

    # ── 內部：HTTP 請求（含 retry / backoff / sleep）──────────────────────

    def _get(self, path: str, params: dict) -> dict[str, Any]:
        """GET 請求；失敗時指數退避重試。"""
        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                resp = self._session.get(url, params=params, timeout=self._timeout)

                # 4xx（除 429）直接拋錯，不重試
                if 400 <= resp.status_code < 500 and resp.status_code != 429:
                    resp.raise_for_status()

                # 429 / 5xx 進入重試
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise requests.HTTPError(
                        f"HTTP {resp.status_code}", response=resp
                    )

                resp.raise_for_status()
                data = resp.json()

                # 禮貌爬蟲：請求間 sleep
                time.sleep(self._interval)
                return data

            except (requests.RequestException, ValueError) as e:
                last_exc = e
                wait = 2 ** (attempt - 1)  # 1s, 2s, 4s
                logger.warning(
                    "請求失敗（嘗試 %d/%d）：%s；%ds 後重試",
                    attempt, self._max_retries, e, wait,
                )
                if attempt < self._max_retries:
                    time.sleep(wait)

        raise RuntimeError(f"GET {url} 重試 {self._max_retries} 次仍失敗") from last_exc

    def _dump_cache(self, key: str, payload: dict) -> None:
        """把 raw response 落地存檔（debug 用）。

        檔名：YYYYMMDD_<key>.json
        """
        today = datetime.now().strftime("%Y%m%d")
        path = _CACHE_DIR / f"{today}_{key}.json"
        try:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("raw cache 寫入失敗：%s（%s）", e, path)

    # ── 內部：lolesports JSON → 中介格式 ──────────────────────────────────

    @staticmethod
    def _parse_league(item: dict) -> LeagueDict:
        return {
            "code":        (item.get("name") or "").upper(),
            "name":        item.get("displayName") or item.get("name") or "",
            "region":      item.get("region") or "",
            "external_id": str(item.get("id") or ""),
        }

    @staticmethod
    def _parse_team(item: dict, league_code: str) -> TeamDict:
        return {
            "code":        (item.get("code") or "").upper(),
            "name":        item.get("name") or "",
            "league_code": league_code.upper(),
            "logo_url":    item.get("image") or "",
            "external_id": str(item.get("id") or ""),
        }

    def _parse_series(
        self,
        event: dict,
        league_code: str,
        cutoff_upper: datetime,
        cutoff_lower: datetime,
    ) -> SeriesDict | None:
        """解析單筆 schedule event → SeriesDict。

        過濾條件：
        - 開賽時間若超出 [cutoff_lower, cutoff_upper] 區間 → 跳過
        - 缺少必要欄位（teams / startTime / strategy）→ raise，由上層捕捉跳過
        """
        start_str = event.get("startTime")
        if not start_str:
            raise ValueError("缺少 startTime")

        # lolesports 回傳 ISO8601 UTC（如 '2026-05-04T09:00:00Z'）
        utc_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))

        if utc_dt > cutoff_upper or utc_dt < cutoff_lower:
            return None

        match = event.get("match") or {}
        teams = match.get("teams") or []
        if len(teams) != 2:
            raise ValueError(f"team 數不為 2：{len(teams)}")
        if not all(isinstance(t, dict) for t in teams):
            raise ValueError(f"teams 含 non-dict 元素")

        strategy = (match.get("strategy") or {}).get("count")
        if strategy not in (1, 3, 5):
            raise ValueError(f"未知 best_of：{strategy}")

        # 在地時區（lolesports event 不直接給；用 league code 推斷）
        tz_name = _league_timezone(league_code)
        local_dt = utc_dt.astimezone(ZoneInfo(tz_name))

        # 比分（result 可能是 null，用 or {} 防 None.get() 爆炸）
        score_a = (teams[0].get("result") or {}).get("gameWins")
        score_b = (teams[1].get("result") or {}).get("gameWins")

        # 決定 status：lolesports 用 state='unstarted' / 'inProgress' / 'completed'
        state = (event.get("state") or "").lower()
        status_map = {
            "unstarted":   "scheduled",
            "inprogress":  "live",
            "completed":   "completed",
        }
        status = status_map.get(state, "scheduled")

        # 勝者（completed 時才有）
        winner_code: str | None = None
        if status == "completed":
            for t in teams:
                if (t.get("result") or {}).get("outcome") == "win":
                    winner_code = (t.get("code") or "").upper() or None
                    break

        # 直播連結（取第一個英文官方流，若無則任一）
        stream_url = _pick_stream_url(event.get("streams") or [])

        return {
            "external_id":        str(match.get("id") or event.get("id") or ""),
            "league_code":        league_code.upper(),
            "match_date":         local_dt.date(),
            "match_time":         local_dt.strftime("%H:%M:%S"),
            "timezone":           tz_name,
            "match_datetime_utc": utc_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "team_a_code":        (teams[0].get("code") or "").upper(),
            "team_b_code":        (teams[1].get("code") or "").upper(),
            "team_a_external_id": str(teams[0].get("id") or ""),
            "team_b_external_id": str(teams[1].get("id") or ""),
            "best_of":            int(strategy),
            "stage":              event.get("blockName") or "",
            "status":             status,
            "score_a":            score_a,
            "score_b":            score_b,
            "winner_team_code":   winner_code,
            "stream_url":         stream_url,
            "vod_url":            None,  # VOD 由 getEventDetails 才有，第一版略
        }

    # ── 內部：聯賽 code → external id 反查 ───────────────────────────────

    _league_id_cache: dict[str, str] = {}

    def _resolve_league_id(self, league_code: str) -> str | None:
        """快取 league code → external_id 對應。"""
        key = league_code.upper()
        if key in self._league_id_cache:
            return self._league_id_cache[key]

        for lg in self.fetch_leagues():
            self._league_id_cache[lg["code"]] = lg.get("external_id", "")

        return self._league_id_cache.get(key)


# ── Helpers（模組級純函式）────────────────────────────────────────────────

# 各聯賽的常見在地時區；找不到時 fallback 到 UTC
_LEAGUE_TZ = {
    "LCK":         "Asia/Seoul",
    "LPL":         "Asia/Shanghai",
    "LCP":         "Asia/Taipei",
    "LCS":         "America/Los_Angeles",
    "LEC":         "Europe/Berlin",
    "MSI":         "UTC",
    "WORLDS":      "UTC",
    "FIRST_STAND": "UTC",
}


def _league_timezone(league_code: str) -> str:
    """依聯賽 code 推斷在地時區。"""
    return _LEAGUE_TZ.get(league_code.upper(), "UTC")


def _pick_stream_url(streams: list[dict]) -> str | None:
    """從 streams 列表挑一個直播連結。

    優先順序：英文官方 → 中文 → 任一可用。
    """
    if not streams:
        return None

    def _build(s: dict) -> str | None:
        provider = (s.get("provider") or "").lower()
        param = s.get("parameter") or ""
        if not param:
            return None
        if provider == "youtube":
            return f"https://www.youtube.com/watch?v={param}"
        if provider == "twitch":
            return f"https://www.twitch.tv/{param}"
        if provider == "bilibili":
            return f"https://live.bilibili.com/{param}"
        return param

    # 英文流
    for s in streams:
        if (s.get("locale") or "").startswith("en"):
            url = _build(s)
            if url:
                return url

    # 中文
    for s in streams:
        locale = (s.get("locale") or "").lower()
        if locale.startswith("zh"):
            url = _build(s)
            if url:
                return url

    # 任一
    for s in streams:
        url = _build(s)
        if url:
            return url

    return None

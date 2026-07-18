"""資料來源抽象介面。

所有來源都實作這套介面，pipeline 不關心是哪一家（目前只有 lolesports；
未來想加 leaguepedia / 第三方爬蟲只要 implements AbstractSource 就接得上）。
回傳的 dict 必須符合下方 schema，供 transformers 後續處理。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import TypedDict


class LeagueDict(TypedDict, total=False):
    """聯賽資料 schema。"""
    code:        str   # 'LCK','LPL'
    name:        str
    region:      str
    external_id: str   # 來源端的 league id


class TeamDict(TypedDict, total=False):
    """隊伍資料 schema。"""
    code:        str   # 'T1','GEN'
    name:        str
    league_code: str   # 主屬聯賽 code
    logo_url:    str
    external_id: str


class SeriesDict(TypedDict, total=False):
    """系列賽資料 schema（pipeline 寫入 DB 前的中介格式）。"""
    external_id:        str           # 來源端 match id（識別「同一場」的真依據）
    league_code:        str
    match_date:         date          # 在地日期
    match_time:         str           # 'HH:MM:SS' 在地時間
    timezone:           str           # 'Asia/Seoul'
    match_datetime_utc: str           # ISO8601 UTC
    team_a_code:        str
    team_b_code:        str
    team_a_external_id: str
    team_b_external_id: str
    best_of:            int
    stage:              str
    status:             str           # 'scheduled','live','completed','cancelled'
    score_a:            int | None
    score_b:            int | None
    winner_team_code:   str | None
    stream_url:         str | None
    vod_url:            str | None


class AbstractSource(ABC):
    """資料來源介面（目前只有 lolesports，未來可擴充）。"""

    @abstractmethod
    def fetch_leagues(self) -> list[LeagueDict]:
        """拉所有聯賽。"""

    @abstractmethod
    def fetch_teams(self, league_code: str) -> list[TeamDict]:
        """拉指定聯賽的隊伍。"""

    @abstractmethod
    def fetch_schedule(
        self, league_code: str, days_ahead: int, days_back: int = 0,
    ) -> list[SeriesDict]:
        """拉指定聯賽 [now - days_back, now + days_ahead] 區間的賽程。"""

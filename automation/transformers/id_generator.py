"""流水編號生成（純函式，易於單元測試）。

規則：14 位 BIGINT
    YYYY MM DD LL NNN G
       │  │  │  │   │ └─ 局數 1~9（0 = 整個系列賽 series_id）
       │  │  │  │   └─── 當天第幾場 001~999
       │  │  │  └─────── 聯賽碼 01~99
       │  │  └────────── 日 01~31
       │  └──────────── 月 01~12
       └─────────────── 西元年 1900~9999

範例：
    series_id = 20260504_01_003_0  → 2026-05-04 LCK 當天第 3 場 BO5 系列賽
    game_id   = 20260504_01_003_2  → 同系列賽的第 2 局
"""

from __future__ import annotations

from datetime import date


# ── 各欄位寬度（位數）────────────────────────────────────────────────────
_W_YEAR    = 4
_W_MONTH   = 2
_W_DAY     = 2
_W_LEAGUE  = 2
_W_SEQ     = 3   # 001 ~ 999
_W_GAME    = 1   # 0 = series, 1~9 = game

# 寬度上限（用於驗證）
_MAX_LEAGUE = 99
_MAX_SEQ    = 999
_MAX_GAME   = 9


def make_series_id(match_date: date, league_id: int, daily_seq: int) -> int:
    """組出系列賽的 14 位流水編號（末位 G=0）。

    Args:
        match_date: 在地比賽日期。
        league_id : 1~99 的聯賽 ID（會 zero-pad 到 2 位）。
        daily_seq : 該天該聯賽的第幾場（1~999）。

    Raises:
        ValueError: 任一輸入超出規則容量。
    """
    _validate_league(league_id)
    _validate_seq(daily_seq)

    return int(
        f"{match_date.year:0{_W_YEAR}d}"
        f"{match_date.month:0{_W_MONTH}d}"
        f"{match_date.day:0{_W_DAY}d}"
        f"{league_id:0{_W_LEAGUE}d}"
        f"{daily_seq:0{_W_SEQ}d}"
        f"0"  # G=0 表示系列賽
    )


def make_game_id(series_id: int, game_number: int) -> int:
    """從 series_id 推導第 N 局的 game_id（末位 G=1~9）。

    series_id 末位必為 0；本函式只替換末位。
    """
    if game_number < 1 or game_number > _MAX_GAME:
        raise ValueError(f"game_number 必須是 1~{_MAX_GAME}，收到 {game_number}")

    if series_id % 10 != 0:
        raise ValueError(f"series_id 末位必為 0（series_id={series_id}）")

    return series_id + game_number


def split_series_id(series_id: int) -> dict:
    """把 series_id 拆回原始欄位（除錯／驗證用）。

    回傳 dict 包含 year / month / day / league_id / daily_seq / game_number。
    """
    s = str(series_id)
    if len(s) != 14:
        raise ValueError(f"series_id 必須為 14 位，收到 {len(s)} 位（{series_id}）")

    return {
        "year":         int(s[0:4]),
        "month":        int(s[4:6]),
        "day":          int(s[6:8]),
        "league_id":    int(s[8:10]),
        "daily_seq":    int(s[10:13]),
        "game_number":  int(s[13]),
    }


def make_match_code(
    match_date: date,
    league_code: str,
    daily_seq: int,
    team_a_code: str,
    team_b_code: str,
) -> str:
    """組出人類可讀的 match_code。

    格式：YYYYMMDD_LEAGUE_NNN_AvsB
    範例：20260504_LCK_003_T1vsGEN

    含 daily_seq 是為了避免同日同隊伍 tiebreaker / rematch 撞號。
    """
    _validate_seq(daily_seq)
    return (
        f"{match_date.strftime('%Y%m%d')}"
        f"_{league_code.upper()}"
        f"_{daily_seq:0{_W_SEQ}d}"
        f"_{team_a_code.upper()}vs{team_b_code.upper()}"
    )


# ── 內部驗證 ────────────────────────────────────────────────────────────
def _validate_league(league_id: int) -> None:
    if league_id < 1 or league_id > _MAX_LEAGUE:
        raise ValueError(f"league_id 必須是 1~{_MAX_LEAGUE}，收到 {league_id}")


def _validate_seq(daily_seq: int) -> None:
    if daily_seq < 1 or daily_seq > _MAX_SEQ:
        raise ValueError(f"daily_seq 必須是 1~{_MAX_SEQ}，收到 {daily_seq}")

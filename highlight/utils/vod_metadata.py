"""VOD 識別工具：從一個 mp4 檔反查它對應哪個 series / broadcast。

【架構邊界】
此模組允許 import automation.db（單向例外）。
但 detectors/、selection/、rendering/、training/ **永遠不准** import automation。

【lazy import 防循環依賴】
所有 automation.* import 一律寫進函式內部（不放檔頂），
避免 automation 反向 import highlight.utils.* 時觸發 partially initialized module error。

使用：
    from highlight.utils.vod_metadata import extract_metadata, generate_split_filename

    meta = extract_metadata(Path("E:/videos/split/LCK_20260520_g1_T1vsGEN.mp4"))
    print(meta.confidence, meta.series_id, meta.match_date, meta.teams)

    # 給未來 split_vod 整合用：
    fname = generate_split_filename(meta, game_index=1, teams=("T1","GEN"),
                                    fallback_stem="原始檔名")
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class VodMetadata:
    """VOD 反查結果。

    confidence 由「鏈式判斷」過程中能填到多少欄位決定：
        high    : league + match_date + teams + series_id 全到（DB 有對應）
        medium  : league + match_date + teams 有，但沒對應 series（DB 缺資料）
        low     : 只有 match_date / 部分隊伍
        fallback: 啥也沒有，下游用原始 stem
    """
    league:           str | None       = None
    league_timezone:  str | None       = None
    match_date:       date | None      = None
    teams:            list[str]        = field(default_factory=list)
    series_id:        int | None       = None
    broadcast_id:     int | None       = None
    confidence:       str              = "fallback"
    used_sources:     list[str]        = field(default_factory=list)


def _fetch_db_row(query: str, params: tuple) -> dict | None:
    """唯一允許的 highlight -> automation DB 讀取邊界。"""
    from automation.db.connection import mysql_conn

    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()


def lookup_broadcast_for_highlight(broadcast_id: int) -> dict | None:
    return _fetch_db_row(
        """
        SELECT broadcast_id, recording_path, league_code,
               recording_status_v2, broadcast_date
        FROM broadcasts WHERE broadcast_id = %s
        """,
        (broadcast_id,),
    )


def lookup_game_for_highlight(game_id: int) -> dict | None:
    return _fetch_db_row(
        """
        SELECT g.game_id, g.game_index, g.game_path, g.status,
               b.league_code, b.broadcast_date
        FROM broadcast_games g
        JOIN broadcasts b ON b.broadcast_id = g.broadcast_id
        WHERE g.game_id = %s
        """,
        (game_id,),
    )


def lookup_game_timeline_by_id(game_id: int) -> dict | None:
    return _fetch_db_row(
        """
        SELECT bg.game_id, bg.timeline_anchor_sec, bg.timeline_game_duration_sec,
               bg.timeline_source, bg.timeline_external_id, bg.game_path,
               b.broadcast_date, b.league_code
        FROM broadcast_games bg
        JOIN broadcasts b ON b.broadcast_id = bg.broadcast_id
        WHERE bg.game_id = %s
        """,
        (game_id,),
    )


def lookup_game_timeline_by_path(vod_path: Path) -> dict | None:
    return _fetch_db_row(
        """
        SELECT bg.game_id, bg.timeline_anchor_sec, bg.timeline_game_duration_sec,
               bg.timeline_source, bg.timeline_external_id, bg.game_path,
               b.broadcast_date, b.league_code
        FROM broadcast_games bg
        JOIN broadcasts b ON b.broadcast_id = bg.broadcast_id
        WHERE bg.game_path = %s
        """,
        (str(vod_path.resolve()),),
    )


def update_game_timeline_anchor(
    game_id: int,
    anchor: float | None,
    *,
    verified: bool,
) -> None:
    from automation.db.connection import mysql_conn

    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcast_games SET timeline_anchor_sec=%s, "
                "timeline_verified=%s WHERE game_id=%s",
                (anchor, int(verified), game_id),
            )
        conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# regex：從檔名解析 LEAGUE 前綴（嚴格 → 寬鬆）
_LEAGUE_REGEX = r"LCK|LPL|LCP|LEC|LCS|WCS|MSI|FIRSTSTAND|WORLDS"

_FILENAME_PATTERNS_STRICT = [
    # 'LCK_20260506_g1_T1vsGEN' / 'LCK_20260506_youtube_xxx' 等
    re.compile(rf"^(?P<league>{_LEAGUE_REGEX})_(?P<date>\d{{8}})", re.I),
    # 'LCK 2026-05-06 ...'
    re.compile(rf"^(?P<league>{_LEAGUE_REGEX})\s+(?P<date>\d{{4}}[-_]\d{{2}}[-_]\d{{2}})", re.I),
]
# 寬鬆：只要檔名第一段是 league code 就採用（沒日期 fallback 到 mtime）
_FILENAME_PATTERN_LOOSE = re.compile(rf"^(?P<league>{_LEAGUE_REGEX})(?:_|\s|$)", re.I)

_LEAGUE_TZ = {
    "LCK":        "Asia/Seoul",
    "LPL":        "Asia/Shanghai",
    "LCP":        "Asia/Taipei",
    "LEC":        "Europe/Berlin",
    "LCS":        "America/Los_Angeles",
    "MSI":        "UTC",
    "WCS":        "UTC",
    "FIRSTSTAND": "UTC",
}


# ─────────────────────────────────────────────────────────────────────────────
def extract_metadata(
    vod_path: Path,
    bp_teams_hint: list[str] | None = None,
) -> VodMetadata:
    """鏈式判斷：filename → info.json sidecar → mtime → DB 反查。

    Args:
        vod_path: VOD 檔絕對路徑
        bp_teams_hint: 由 caller（如未來 split_vod）傳入的隊伍縮寫（從 BP 偵測拿到）。
                       本模組不直接呼叫 detectors/ 跑 BP，避免反向依賴 detectors。

    Returns:
        VodMetadata（confidence 反映成功填到多少欄位）
    """
    meta = VodMetadata()

    # ── Step 1: 解析檔名 ────────────────────────────────────────────────
    _parse_filename(vod_path.stem, meta)

    # ── Step 2: 找 info.json sidecar（yt-dlp 下載時才有）────────────────
    if meta.match_date is None or not meta.teams:
        _parse_info_json(vod_path, meta)

    # ── Step 3: 用檔案 mtime 推日期（fallback）──────────────────────────
    if meta.match_date is None:
        _use_mtime(vod_path, meta)

    # ── Step 4: 用 DB teams 白名單從檔名 token 抓隊伍 ────────────────────
    if meta.league and not meta.teams:
        _extract_teams_from_stem(vod_path.stem, meta)

    # ── Step 5: 加入 bp_teams_hint（caller 傳進來的隊伍）─────────────────
    if bp_teams_hint:
        for code in bp_teams_hint:
            if code and code.upper() not in meta.teams:
                meta.teams.append(code.upper())
        if "bp_hint" not in meta.used_sources:
            meta.used_sources.append("bp_hint")

    # ── Step 6: DB 反查 series_id（lazy import — Gemini #5 防循環依賴）──
    _enrich_from_db(meta)

    # ── 評定 confidence ────────────────────────────────────────────────
    meta.confidence = _evaluate_confidence(meta)

    return meta


# ─────────────────────────────────────────────────────────────────────────────
# Phase 49-2 評審 #6：檔名 sanitize（防 Windows 非法字元）
_INVALID_FN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')


def sanitize_filename(name: str, max_len: int = 200) -> str:
    """移除 Windows 非法字元 + 空格 → `_` + 長度上限。

    對應 Phase 49-2 評審 #6（檔名 sanitize）。

    >>> sanitize_filename('LCK 2026-05-06 g1: T1?vs:GEN')
    'LCK_2026-05-06_g1_T1vs_GEN'
    """
    if not name:
        return "untitled"
    # 把控制字元 + Windows 非法字元拿掉
    cleaned = _INVALID_FN_CHARS.sub("", name)
    # 空格 → _（避免在路徑使用時要 quote）
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._")
    if not cleaned:
        return "untitled"
    return cleaned[:max_len]


def generate_split_filename(
    metadata: VodMetadata,
    game_index: int,
    teams: tuple[str, str],
    fallback_stem: str,
) -> str:
    """根據 metadata 產生標準 split 檔名（含 sanitize，評審 #6）。

    完整 metadata → '<LEAGUE>_<YYYYMMDD>_g<n>_<A>vs<B>.mp4'
    不完整 → '<fallback_stem>_g<n>_<A>vs<B>.mp4'（沿用既有命名）
    隊伍空時 → 不加 _<A>vs<B> 後綴（避免出現 _vs_.mp4 怪檔名）

    Args:
        metadata     : extract_metadata() 回傳值
        game_index   : 第幾局（1-based）
        teams        : (隊伍A, 隊伍B) 縮寫；空字串代表未知
        fallback_stem: metadata 不完整時用的原始檔名 stem
    """
    a_raw, b_raw = (teams[0] or ""), (teams[1] or "")
    a = a_raw.upper().strip()
    b = b_raw.upper().strip()
    teams_suffix = f"_{a}vs{b}" if a and b else ""

    if metadata.league and metadata.match_date:
        date_str = metadata.match_date.strftime("%Y%m%d")
        name = f"{metadata.league}_{date_str}{teams_suffix}_g{game_index}.mp4"
    else:
        name = f"{fallback_stem}{teams_suffix}_g{game_index}.mp4"

    # 套 sanitize 確保安全（保留 .mp4 副檔名分開處理）
    stem, _, ext = name.rpartition(".")
    return f"{sanitize_filename(stem)}.{ext}"


# ─────────────────────────────────────────────────────────────────────────────
def lookup_broadcast_series_by_recording_path(
    vod_path: Path,
) -> list[tuple[int, str, str]] | None:
    """用 vod 絕對路徑反查 broadcasts.recording_path → broadcast_series。

    對應 Phase 49-2 Step 1：split_vod 用此查 game→series 對應。

    回傳 list of (series_order, team_a_code, team_b_code)，依 series_order 排序；
    找不到對應 broadcast 回 None。

    【lazy import — 防循環依賴】
    """
    try:
        from automation.db.connection import mysql_conn
    except ImportError as e:
        logger.debug("automation.db 不可用（%s）", e)
        return None

    abs_path = str(vod_path.resolve())
    try:
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                # Step 1: 找 broadcast
                cur.execute(
                    "SELECT broadcast_id FROM broadcasts WHERE recording_path = %s",
                    (abs_path,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                broadcast_id = int(row["broadcast_id"])

                # Step 2: JOIN broadcast_series + teams 拿 (order, A, B)
                cur.execute(
                    """
                    SELECT bs.series_order, ta.code AS team_a, tb.code AS team_b
                    FROM broadcast_series bs
                    JOIN series s    ON s.series_id = bs.series_id
                    JOIN teams  ta   ON ta.team_id = s.team_a_id
                    JOIN teams  tb   ON tb.team_id = s.team_b_id
                    WHERE bs.broadcast_id = %s
                    ORDER BY bs.series_order
                    """,
                    (broadcast_id,),
                )
                return [
                    (int(r["series_order"]), r["team_a"], r["team_b"])
                    for r in cur.fetchall()
                ]
    except Exception as e:
        logger.debug("反查 broadcast_series 失敗：%s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 內部：各步驟
# ─────────────────────────────────────────────────────────────────────────────
def _parse_filename(stem: str, meta: VodMetadata) -> None:
    """從檔名解析 league（必）+ date（盡量）。

    嚴格模式（LCK_20260506...）→ 抓 league + date
    寬鬆模式（LCK_Carry_xxx）→ 只抓 league
    """
    # 嚴格優先
    for pat in _FILENAME_PATTERNS_STRICT:
        m = pat.match(stem)
        if m:
            league = m.group("league").upper()
            date_raw = m.group("date").replace("-", "").replace("_", "")
            try:
                d = datetime.strptime(date_raw, "%Y%m%d").date()
            except ValueError:
                continue
            meta.league = league
            meta.league_timezone = _LEAGUE_TZ.get(league)
            meta.match_date = d
            meta.used_sources.append("filename_strict")
            return

    # 寬鬆：只抓 league
    m = _FILENAME_PATTERN_LOOSE.match(stem)
    if m:
        league = m.group("league").upper()
        meta.league = league
        meta.league_timezone = _LEAGUE_TZ.get(league)
        meta.used_sources.append("filename_loose")


def _parse_info_json(vod_path: Path, meta: VodMetadata) -> None:
    """yt-dlp 下載時可帶 .info.json sidecar（同檔名）。"""
    sidecar = vod_path.with_suffix(".info.json")
    if not sidecar.is_file():
        return
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("讀 %s 失敗：%s", sidecar, e)
        return

    # upload_date 格式 'YYYYMMDD'
    if meta.match_date is None:
        upload_date = data.get("upload_date")
        if upload_date:
            try:
                meta.match_date = datetime.strptime(upload_date, "%Y%m%d").date()
            except Exception:
                pass

    # title 留給 caller，可選
    if "info_json" not in meta.used_sources:
        meta.used_sources.append("info_json")


def _extract_teams_from_stem(stem: str, meta: VodMetadata) -> None:
    """用 DB teams.code 白名單從檔名 token 抓隊伍（lazy import）。

    例：'LPL_BLG_vs_JDG_p1' + LPL → 找出 ['BLG', 'JDG']
    """
    try:
        from automation.db.connection import mysql_conn
    except ImportError:
        return

    # 從 stem 拆 token：用分隔符（_ - . 空白）切，避免 'LoLEsportsTW' 誤切出 'TW'
    tokens = [t for t in re.split(r"[\s_.\-]+", stem) if t]
    if not tokens:
        return

    try:
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                # 查指定 league 的所有 team codes
                cur.execute(
                    """
                    SELECT t.code FROM teams t
                    JOIN leagues l ON l.league_id = t.league_id
                    WHERE l.code = %s
                    """,
                    (meta.league,),
                )
                team_codes = {r["code"].upper() for r in cur.fetchall()}
    except Exception as e:
        logger.debug("查 league=%s 的 teams 失敗：%s", meta.league, e)
        return

    if not team_codes:
        return

    # 取交集（依檔名出現順序）
    found: list[str] = []
    seen: set[str] = set()
    for tk in tokens:
        u = tk.upper()
        if u in team_codes and u not in seen:
            found.append(u)
            seen.add(u)

    if found:
        meta.teams = found
        meta.used_sources.append("stem_teams_lookup")


def _use_mtime(vod_path: Path, meta: VodMetadata) -> None:
    """檔案 mtime 當日期（最後 fallback）。"""
    try:
        mtime = vod_path.stat().st_mtime
    except OSError:
        return

    # 用 league 時區轉本地日期；無 league 時用 UTC
    tz_name = meta.league_timezone or "UTC"
    try:
        from zoneinfo import ZoneInfo
        local_dt = datetime.fromtimestamp(mtime, tz=ZoneInfo(tz_name))
    except Exception:
        local_dt = datetime.fromtimestamp(mtime, tz=timezone.utc)

    meta.match_date = local_dt.date()
    meta.used_sources.append("mtime")


def _enrich_from_db(meta: VodMetadata) -> None:
    """用 (date, teams) 反查 series 表，補 series_id / broadcast_id。

    【lazy import】只在這個函式裡 import automation，
    避免模組載入時形成循環依賴。
    """
    if meta.match_date is None:
        return

    try:
        # ── lazy import（防循環依賴）─────────────────────────────────────
        from automation.db.connection import mysql_conn
    except ImportError as e:
        logger.debug("automation.db 不可用（%s），跳過 DB 反查", e)
        return

    try:
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                # 用 match_date + 已知隊伍縮寫反查 series
                if meta.teams:
                    placeholders = ",".join(["%s"] * len(meta.teams))
                    cur.execute(
                        f"""
                        SELECT s.series_id, s.team_a_id, s.team_b_id,
                               ta.code AS a_code, tb.code AS b_code, l.code AS lg
                        FROM series s
                        JOIN teams ta ON ta.team_id = s.team_a_id
                        JOIN teams tb ON tb.team_id = s.team_b_id
                        JOIN leagues l ON l.league_id = s.league_id
                        WHERE s.match_date = %s
                          AND (ta.code IN ({placeholders}) OR tb.code IN ({placeholders}))
                        LIMIT 5
                        """,
                        (meta.match_date, *meta.teams, *meta.teams),
                    )
                else:
                    cur.execute(
                        """
                        SELECT s.series_id, s.team_a_id, s.team_b_id,
                               ta.code AS a_code, tb.code AS b_code, l.code AS lg
                        FROM series s
                        JOIN teams ta ON ta.team_id = s.team_a_id
                        JOIN teams tb ON tb.team_id = s.team_b_id
                        JOIN leagues l ON l.league_id = s.league_id
                        WHERE s.match_date = %s
                        LIMIT 5
                        """,
                        (meta.match_date,),
                    )

                rows = cur.fetchall()
                if not rows:
                    return

                # 若僅 1 場 → 直接採用
                if len(rows) == 1:
                    row = rows[0]
                    meta.series_id = int(row["series_id"])
                    if not meta.league:
                        meta.league = row["lg"]
                        meta.league_timezone = _LEAGUE_TZ.get(row["lg"])
                    if not meta.teams:
                        meta.teams = [row["a_code"], row["b_code"]]
                    meta.used_sources.append("db_lookup")
                    return

                # 多場：嘗試找 teams 完全吻合的 row
                if len(meta.teams) >= 2:
                    teams_set = {t.upper() for t in meta.teams}
                    for row in rows:
                        pair = {row["a_code"].upper(), row["b_code"].upper()}
                        if pair.issubset(teams_set):
                            meta.series_id = int(row["series_id"])
                            if not meta.league:
                                meta.league = row["lg"]
                                meta.league_timezone = _LEAGUE_TZ.get(row["lg"])
                            meta.used_sources.append("db_lookup")
                            return

                # 多場 + 無隊伍 hint → 不能下定論
    except Exception as e:
        logger.debug("DB 反查失敗：%s", e)
        return


def _evaluate_confidence(meta: VodMetadata) -> str:
    """信心評級。"""
    has_league = bool(meta.league)
    has_date = meta.match_date is not None
    has_teams = len(meta.teams) >= 1
    has_pair = len(meta.teams) >= 2
    has_series = meta.series_id is not None

    if has_league and has_date and has_pair and has_series:
        return "high"
    if has_league and has_date and has_teams:
        return "medium"
    if has_date:
        return "low"
    return "fallback"

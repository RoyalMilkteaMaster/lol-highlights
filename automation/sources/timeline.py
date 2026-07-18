"""Game timeline fetcher。

兩條來源：
  1. Leaguepedia V5 timeline（LCK + LCP + 國際賽 + LCS + LEC）
     - Cargo query MatchScheduleGame → RiotPlatformGameId
     - V5_data:<RiotPlatformGameId>/Timeline wiki page (parse via MediaWiki API)
     - 完整 Riot 原始 timeline JSON（1-1.5 MB / game，1500-2100 events）
     - 含 CHAMPION_KILL / ELITE_MONSTER_KILL / BUILDING_KILL / GAME_END 等全部 events

  2. Bilibili player/v2 view_points（LPL，騰訊不發 RPGId 不能用 Leaguepedia）
     - 從 broadcasts.recording_path 對應的 BV → aid + cid
     - api.bilibili.com/x/player/v2 → view_points 陣列
     - ~15 events / game，物件大事（dragon/baron/voidgrub/herald/nexus）+ 第一滴血 + 團滅
     - 中文事件名 + team_name + 截圖縮圖

raw JSON 存：E:/videos/timelines/<YYYYMMDD>/<source>_<external_id>.json
  Leaguepedia: 2026-05-16/leaguepedia_LOLTMNT01_387874.json
  Bilibili:    2026-05-16/bilibili_BV1AhL_38382865738.json

CLI：
  python -m automation.sources.timeline --date 2026-05-16 --league LCK
  python -m automation.sources.timeline --date 2026-05-16 --league LPL
  python -m automation.sources.timeline --game-id 85   # 用 DB game_id 對到 source
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date as date_cls, datetime
from pathlib import Path

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from . import proxy_pool

logger = logging.getLogger(__name__)


# Phase 54：free proxy on/off。env LEAGUEPEDIA_USE_PROXY=1 啟用。
# 啟用後 _get_json 對 lol.fandom.com 走 proxy_pool（rotation on failure）。
import os as _os
_USE_PROXY = _os.environ.get("LEAGUEPEDIA_USE_PROXY", "0").strip() == "1"
# 同 query 換 proxy retry 上限。free proxy ratelimited 比例 ~50%，5 次累積成功率 ~97%。
_PROXY_MAX_ATTEMPTS = 5


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
LEAGUEPEDIA_API = "https://lol.fandom.com/api.php"
BILIBILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
BILIBILI_PLAYER_API = "https://api.bilibili.com/x/player/v2"

# Leaguepedia 對沒 contact 資訊的 UA 有 rate limit
# 改用 MediaWiki API 慣例 UA（含 contact），有時候差很大
_LEAGUEPEDIA_UA = (
    "lol-highlights-scraper/1.0 (https://github.com/he00298902/lol-highlights; "
    "he00298902@gmail.com)"
)

# 給 Bilibili 用一般 Chrome UA
_FIXED_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

# Leagues that use Leaguepedia / Bilibili / unavailable
LEAGUEPEDIA_LEAGUES = {"LCK", "LCP", "MSI", "WCS", "LCS", "LEC", "PCS"}
BILIBILI_LEAGUES = {"LPL"}



# ─────────────────────────────────────────────────────────────────────────────
# Rate limit handling
# ─────────────────────────────────────────────────────────────────────────────
class LeaguepediaRateLimited(Exception):
    """Cargo HTTP 200 但 body error.code=ratelimited 時 raise。

    NOT subclass of requests.RequestException → 不會被 _get_json 外層 @retry 吃掉重試。
    Caller (cargo_query_games) 看見此 exception 就 cool down，30 min 內 skip 同 source 所有 query。
    """


_COOLDOWN_DIR = Path(__file__).resolve().parent.parent / "cache" / "cooldown"
_COOLDOWN_TTL_SEC = 1800.0  # 30 min 內所有 same-source query skip


def _cooldown_path(source: str) -> Path:
    _COOLDOWN_DIR.mkdir(parents=True, exist_ok=True)
    return _COOLDOWN_DIR / f"{source}.txt"


def _write_cooldown_marker(source: str) -> None:
    """寫 timestamp 到 disk 標 source rate-limited。失敗不影響主流程。"""
    try:
        _cooldown_path(source).write_text(str(time.time()), encoding="utf-8")
    except OSError as e:
        logger.warning("[cooldown] 寫 %s marker 失敗: %s", source, e)


def _is_in_cooldown(source: str, ttl_sec: float = _COOLDOWN_TTL_SEC) -> tuple[bool, float]:
    """回 (still_in_cooldown, remaining_sec)。"""
    p = _cooldown_path(source)
    if not p.is_file():
        return False, 0.0
    try:
        ts = float(p.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False, 0.0
    age = time.time() - ts
    if age < ttl_sec:
        return True, ttl_sec - age
    return False, 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Common utils
# ─────────────────────────────────────────────────────────────────────────────
def _get_ua() -> str:
    return random.choice(_FIXED_UAS)


def _polite_sleep(base: float = 1.0, jitter: float = 1.0) -> None:
    time.sleep(base + random.uniform(0, jitter))


def _leaguepedia_sleep() -> None:
    """Leaguepedia 對 anonymous query rate limit **非常嚴**：
    - 5/17 試 sleep 3-5s 還是被 ban
    - 加長到 8-12s（連續 fetch 5-10 個 timeline JSON 都不會被擋）

    對 user 而言：5 個 LCK game = 1 cargo + 5 V5 = 6 query × 10s = ~60s 等待。
    對 LPL Bilibili / 其他來源不適用。
    """
    time.sleep(8.0 + random.uniform(0, 4.0))


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    retry=retry_if_exception_type((requests.RequestException,)),
    reraise=True,
)
def _get_json(url: str, params: dict, headers: dict | None = None) -> dict | None:
    """GET + parse JSON, 3 次 retry exp backoff。4xx 不 retry，5xx retry。

    Leaguepedia API (lol.fandom.com) 用 MediaWiki 慣例 UA（含 contact）+ 可選 proxy（Phase 54）。
    其他來源用一般 Chrome UA、直連。
    """
    if "lol.fandom.com" in url:
        _leaguepedia_sleep()
    else:
        _polite_sleep()
    if "lol.fandom.com" in url:
        h = {"User-Agent": _LEAGUEPEDIA_UA, "Accept": "application/json"}
    else:
        h = {"User-Agent": _get_ua(), "Accept": "application/json"}
    if headers:
        h.update(headers)

    # Phase 54：proxy 模式下對 same query 換 proxy 重試 3 次。直連模式則 1 次。
    use_proxy = _USE_PROXY and "lol.fandom.com" in url
    attempts = _PROXY_MAX_ATTEMPTS if use_proxy else 1
    last_exc: Exception | None = None

    for i in range(attempts):
        proxy = proxy_pool.get_current() if use_proxy else None
        proxies = {"http": proxy, "https": proxy} if proxy else None
        if use_proxy:
            logger.info("[proxy] %s attempt %d/%d via %s",
                        url.split("/")[2], i + 1, attempts, proxy or "DIRECT (pool empty)")
        try:
            r = requests.get(url, params=params, headers=h, timeout=15, proxies=proxies)
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.SSLError) as e:
            if proxy:
                proxy_pool.mark_dead(proxy)
                last_exc = e
                continue
            raise
        if 400 <= r.status_code < 500:
            logger.warning("timeline GET %s → %d (4xx，不 retry)", url, r.status_code)
            return None
        if proxy and 500 <= r.status_code < 600:
            logger.warning("[proxy] %s 回 %d，mark dead 換下一個", proxy, r.status_code)
            proxy_pool.mark_dead(proxy)
            continue
        r.raise_for_status()
        try:
            data = r.json()
        except ValueError:
            logger.error("timeline JSON parse 失敗 url=%s body[:200]=%s", url, r.text[:200])
            return None
        # MediaWiki rate-limit：HTTP 200 但 body error.code=ratelimited
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            err = data["error"]
            code = err.get("code")
            if code == "ratelimited":
                if proxy:
                    logger.warning("[proxy] %s ratelimited，mark dead 換下一個（%d/%d）",
                                   proxy, i + 1, attempts)
                    proxy_pool.mark_dead(proxy)
                    continue
                # 直連模式：寫 cool-down + 退出（NOT RequestException → 外層 @retry 不重試）
                logger.error("[Leaguepedia RATE LIMITED] %s — 寫 cool down marker，30 min 內 skip",
                             err.get("info"))
                _write_cooldown_marker("leaguepedia")
                raise LeaguepediaRateLimited(err.get("info") or "ratelimited")
            logger.warning("MediaWiki API error code=%s info=%s", code, err.get("info"))
        return data

    # proxy 全 fail
    logger.error("[proxy] 所有 %d attempt 都失敗 → 寫 cool down，等下次 fetch 走 fallback", attempts)
    _write_cooldown_marker("leaguepedia")
    if last_exc:
        raise last_exc
    raise LeaguepediaRateLimited("all proxies failed/ratelimited")


def _timelines_dir() -> Path:
    """timelines 儲存根目錄。可被 ENV TIMELINES_DIR override，否則走 paths.timelines_dir()（吃 VIDEO_DIR）。"""
    import os
    env = os.environ.get("TIMELINES_DIR")
    if env:
        return Path(env)
    from highlight.utils import paths
    return paths.timelines_dir()


def _save_raw(raw: dict, source: str, external_id: str, match_date: date_cls) -> Path:
    """寫 raw JSON 到 E:/videos/timelines/<YYYYMMDD>/<source>_<id>.json。"""
    day_dir = _timelines_dir() / match_date.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    # 替換 external_id 內檔名不能用字元
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", external_id)
    out = day_dir / f"{source}_{safe_id}.json"
    with out.open("w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Data class
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class GameTimeline:
    """正規化後的 game timeline。"""

    source: str                    # 'leaguepedia' / 'bilibili'
    external_id: str               # 'LOLTMNT01_387874' or 'BV1AhL_38382865738'
    match_date: date_cls
    game_duration_ms: int          # GAME_END timestamp ms (Leaguepedia) or 最後 event from*1000 (Bilibili)
    events_count: int              # 事件總數（debug 用）
    raw_path: Path | None = None   # raw JSON 存哪
    raw: dict = field(default_factory=dict)  # 原始 response (不一定要序列化)


# ─────────────────────────────────────────────────────────────────────────────
# Leaguepedia (Riot V5 timeline) — LCK / LCP / MSI / WCS / LCS / LEC
# ─────────────────────────────────────────────────────────────────────────────
_CARGO_CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "cargo"
_CARGO_CACHE_TTL_SEC = 1800  # 30 min（Leaguepedia cargo cache 不穩，但 30 min 內結果不該變）


def _cargo_cache_path(match_date: date_cls, league_prefix: str) -> Path:
    _CARGO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CARGO_CACHE_DIR / f"{match_date.strftime('%Y-%m-%d')}_{league_prefix}.json"


def cargo_query_games(
    match_date: date_cls,
    league_prefix: str,
    *,
    use_cache: bool = True,
    cache_ttl_sec: float = _CARGO_CACHE_TTL_SEC,
) -> list[dict]:
    """Cargo query Leaguepedia MatchScheduleGame + MatchSchedule。

    加 disk cache 30 min — Leaguepedia cargo cache 不穩定（同 query 反覆 0 vs 17 hits），
    第一次拿到資料就存 disk，之後從 disk 讀避免 cache miss 跳針。

    Returns:
        list of dicts {RiotPlatformGameId, Team1, Team2, DateTime UTC,
                       OverviewPage, N GameInMatch}
    """
    cache_path = _cargo_cache_path(match_date, league_prefix)
    # 先讀 disk cache
    if use_cache and cache_path.is_file():
        import time as _time
        age = _time.time() - cache_path.stat().st_mtime
        if age < cache_ttl_sec:
            try:
                with cache_path.open(encoding="utf-8") as f:
                    cached = json.load(f)
                logger.debug("[cargo] disk cache hit (%s, age=%.0fs, %d games)",
                             cache_path.name, age, len(cached))
                return cached
            except Exception as e:
                logger.warning("[cargo] disk cache 讀失敗 %s: %s", cache_path, e)

    # Phase 54：0-hits short marker（5 min TTL）— Leaguepedia indexing 慢時，
    # naming_finalizer 每 5 min cron 對同 date 重打浪費 quota/proxy。
    # 第一次 0 hits 後標 marker，5 min 內看到就 skip，等過了再真打一次。
    zero_path = cache_path.with_suffix(cache_path.suffix + ".0hits")
    if use_cache and zero_path.is_file():
        import time as _time
        zage = _time.time() - zero_path.stat().st_mtime
        if zage < 300.0:
            logger.info(
                "[cargo] %s_%s 5min 內已試過 0 hits（age=%.0fs），skip 不打",
                match_date.strftime("%Y-%m-%d"), league_prefix, zage,
            )
            return []

    # Cool-down check：被 rate-limit 過 30 min 內 skip 不打 API
    in_cd, remaining = _is_in_cooldown("leaguepedia")
    if in_cd:
        logger.warning(
            "[cargo] Leaguepedia cool down 中（剩 %.0fs），skip 不打 → 回 []",
            remaining,
        )
        return []

    params = {
        "action": "cargoquery",
        "format": "json",
        "limit": "60",
        "tables": "MatchScheduleGame=MSG, MatchSchedule=MS",
        "fields": ("MSG.RiotPlatformGameId, MS.Team1, MS.Team2, "
                   "MS.DateTime_UTC, MS.OverviewPage, MSG.N_GameInMatch"),
        "where": (f'DATE(MS.DateTime_UTC)="{match_date.strftime("%Y-%m-%d")}" '
                  f'AND MS.OverviewPage LIKE "{league_prefix}%"'),
        "join_on": "MSG.MatchId=MS.MatchId",
        "order_by": "MS.DateTime_UTC, MSG.N_GameInMatch",
    }
    try:
        data = _get_json(LEAGUEPEDIA_API, params)
    except LeaguepediaRateLimited:
        # _get_json 已寫 cool-down marker，這邊直接回空
        return []
    if not data:
        return []
    items = [item["title"] for item in data.get("cargoquery", [])]
    # 寫 disk cache（只在拿到資料時 — 避免 cache miss 寫進 0 結果）
    if use_cache and items:
        try:
            with cache_path.open("w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False)
            logger.debug("[cargo] disk cache written (%s, %d games)", cache_path.name, len(items))
        except Exception as e:
            logger.warning("[cargo] disk cache 寫失敗 %s: %s", cache_path, e)
        # 拿到 hits 後清掉舊的 0-hits marker
        if zero_path.exists():
            try:
                zero_path.unlink()
            except OSError:
                pass
    elif use_cache and not items:
        # Phase 54：0-hits 寫 5 min marker，避免 naming_finalizer cron 連環打
        try:
            zero_path.touch()
            logger.info("[cargo] %s_%s 真打回 0 hits，寫 5min marker",
                        match_date.strftime("%Y-%m-%d"), league_prefix)
        except OSError as e:
            logger.warning("[cargo] 0-hits marker 寫失敗 %s: %s", zero_path, e)
    return items


def fetch_leaguepedia_timeline(
    riot_platform_game_id: str,
) -> dict | None:
    """從 V5_data:<RiotPlatformGameId>/Timeline wiki page 拿 JSON。

    Returns: dict（Riot V5 timeline schema：endOfGameResult, frameInterval, frames, ...）
             或 None（page 不存在 / parse 失敗）
    """
    params = {
        "action": "parse",
        "page": f"V5_data:{riot_platform_game_id}/Timeline",
        "format": "json",
        "prop": "wikitext",
    }
    # Cool-down check：被 rate-limit 過 30 min 內 skip
    in_cd, remaining = _is_in_cooldown("leaguepedia")
    if in_cd:
        logger.warning("[leaguepedia] cool down 中（剩 %.0fs），skip V5 fetch", remaining)
        return None
    try:
        data = _get_json(LEAGUEPEDIA_API, params)
    except LeaguepediaRateLimited:
        return None
    if not data or "parse" not in data:
        if data and "error" in data:
            err = data["error"].get("info", "?")
            logger.info("[leaguepedia] %s timeline 不存在: %s", riot_platform_game_id, err)
        return None
    txt = data["parse"]["wikitext"]["*"]
    try:
        return json.loads(txt)
    except json.JSONDecodeError as e:
        logger.warning("[leaguepedia] %s wikitext JSON parse 失敗: %s", riot_platform_game_id, e)
        return None


def _extract_game_end_ms(timeline_raw: dict) -> int:
    """從 V5 timeline 找 GAME_END event timestamp（毫秒）。
    沒找到時 fallback 最後 frame 的最後 event timestamp。"""
    frames = timeline_raw.get("frames") or []
    for frame in reversed(frames):
        for ev in reversed(frame.get("events", [])):
            if ev.get("type") == "GAME_END":
                return int(ev.get("timestamp", 0))
    # fallback: last frame last event
    if frames and frames[-1].get("events"):
        return int(frames[-1]["events"][-1].get("timestamp", 0))
    return 0


def _count_events(timeline_raw: dict) -> int:
    return sum(len(f.get("events", [])) for f in timeline_raw.get("frames") or [])


def fetch_leaguepedia_for_date(
    match_date: date_cls,
    league: str,
    save: bool = True,
) -> list[GameTimeline]:
    """主入口（Leaguepedia）：對某日某聯賽拿所有 game timeline。

    league: 'LCK' / 'LCP' / 'MSI' / 'WCS' / 'LCS' / 'LEC'（會用 LIKE 前綴比對）
    """
    games = cargo_query_games(match_date, league)
    logger.info("[leaguepedia] Cargo: %d games for %s on %s", len(games), league, match_date)

    results: list[GameTimeline] = []
    for g in games:
        rpgi = g.get("RiotPlatformGameId") or ""
        if not rpgi:
            logger.debug("[leaguepedia] skip empty RPGId: %s", g)
            continue
        raw = fetch_leaguepedia_timeline(rpgi)
        if raw is None:
            logger.warning("[leaguepedia] %s 拿不到 timeline，skip", rpgi)
            continue
        gt = GameTimeline(
            source="leaguepedia",
            external_id=rpgi,
            match_date=match_date,
            game_duration_ms=_extract_game_end_ms(raw),
            events_count=_count_events(raw),
            raw=raw,
        )
        if save:
            gt.raw_path = _save_raw(raw, "leaguepedia", rpgi, match_date)
        results.append(gt)
        logger.info(
            "[leaguepedia] ✓ %s %s vs %s g%s: %d events GAME_END=%ds → %s",
            rpgi, g.get("Team1", "?")[:15], g.get("Team2", "?")[:15],
            g.get("N GameInMatch"), gt.events_count, gt.game_duration_ms // 1000,
            gt.raw_path.name if gt.raw_path else "(no save)",
        )
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Bilibili player/v2 view_points — LPL
# ─────────────────────────────────────────────────────────────────────────────
def fetch_bilibili_view(bvid: str) -> dict | None:
    """fetch view API 拿 aid + pages (cid)。"""
    data = _get_json(
        BILIBILI_VIEW_API,
        params={"bvid": bvid},
        headers={"Referer": "https://www.bilibili.com/"},
    )
    if not data or data.get("code") != 0:
        return None
    return data["data"]


def fetch_bilibili_view_points(aid: int, cid: int) -> list[dict]:
    """從 player/v2 拿 view_points 陣列（單一 game = 單一 cid）。"""
    data = _get_json(
        BILIBILI_PLAYER_API,
        params={"aid": aid, "cid": cid},
        headers={"Referer": "https://www.bilibili.com/"},
    )
    if not data or data.get("code") != 0:
        return []
    return data.get("data", {}).get("view_points") or []


def fetch_bilibili_for_bvid(
    bvid: str,
    match_date: date_cls | None = None,
    save: bool = True,
) -> list[GameTimeline]:
    """主入口（Bilibili）：一個 BV 對應多 part（pages），每 part = 1 game。

    回 list[GameTimeline]，每 part 一個。
    """
    view = fetch_bilibili_view(bvid)
    if not view:
        logger.warning("[bilibili] %s view API 失敗", bvid)
        return []

    if match_date is None:
        # 從 upload date 推
        ts = view.get("pubdate")
        if ts:
            match_date = datetime.fromtimestamp(int(ts)).date()
        else:
            match_date = date_cls.today()

    aid = view["aid"]
    pages = view.get("pages") or []
    results: list[GameTimeline] = []
    for p in pages:
        cid = p["cid"]
        part_name = p.get("part", "")
        vps = fetch_bilibili_view_points(aid, cid)
        if not vps:
            logger.info("[bilibili] %s cid=%s view_points 空，skip", bvid, cid)
            continue
        # game_duration ≈ last view_point["to"]
        last_t = max((vp.get("to", vp.get("from", 0)) for vp in vps), default=0)
        raw_packed = {
            "bvid": bvid,
            "aid": aid,
            "cid": cid,
            "part": part_name,
            "duration": p.get("duration"),
            "view_points": vps,
        }
        external_id = f"{bvid}_{cid}"
        gt = GameTimeline(
            source="bilibili",
            external_id=external_id,
            match_date=match_date,
            game_duration_ms=int(last_t) * 1000,
            events_count=len(vps),
            raw=raw_packed,
        )
        if save:
            gt.raw_path = _save_raw(raw_packed, "bilibili", external_id, match_date)
        results.append(gt)
        logger.info(
            "[bilibili] ✓ %s cid=%s part='%s': %d events, last=%ds → %s",
            bvid, cid, part_name[:30], gt.events_count, gt.game_duration_ms // 1000,
            gt.raw_path.name if gt.raw_path else "(no save)",
        )
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Main entry：依 league 自動選 source
# ─────────────────────────────────────────────────────────────────────────────
def fetch_for_date_and_league(
    match_date: date_cls,
    league: str,
    save: bool = True,
    bilibili_bvids: list[str] | None = None,
) -> list[GameTimeline]:
    """主入口：依 league 選 source。

    league 在 LEAGUEPEDIA_LEAGUES → Cargo + V5 timeline
    league 在 BILIBILI_LEAGUES → 需要 bvids 參數（從 broadcasts 對應 BV 來）

    bilibili_bvids：LPL 用，list of BV ids；None 則 skip。
    """
    league_upper = league.upper()
    if league_upper in LEAGUEPEDIA_LEAGUES:
        return fetch_leaguepedia_for_date(match_date, league_upper, save=save)
    if league_upper in BILIBILI_LEAGUES:
        if not bilibili_bvids:
            logger.warning(
                "[timeline] %s 走 Bilibili，但沒傳 BV ids。請從 DB broadcasts 拿。",
                league_upper,
            )
            return []
        all_results: list[GameTimeline] = []
        for bvid in bilibili_bvids:
            all_results.extend(fetch_bilibili_for_bvid(bvid, match_date=match_date, save=save))
        return all_results
    logger.warning("[timeline] 不支援的 league: %s（沒有 timeline 來源）", league)
    return []


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="Game timeline fetcher (Leaguepedia + Bilibili)")
    parser.add_argument("--date", type=str, required=True, help="YYYY-MM-DD")
    parser.add_argument("--league", type=str, required=True,
                        help="LCK / LCP / LPL / MSI / WCS / LCS / LEC / PCS")
    parser.add_argument("--bvid", type=str, action="append",
                        help="LPL 用：BV id (可重複指定)")
    parser.add_argument("--no-save", action="store_true",
                        help="不寫 disk（只 print）")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        d = datetime.strptime(args.date, "%Y-%m-%d").date()
    except ValueError:
        print(f"日期格式錯：{args.date}（要 YYYY-MM-DD）")
        return 2

    league = args.league.upper()
    print(f"=== Fetching timeline for {league} on {d} ===")
    results = fetch_for_date_and_league(
        d, league,
        save=(not args.no_save),
        bilibili_bvids=args.bvid,
    )
    print(f"\n=== {len(results)} timelines fetched ===")
    for gt in results:
        dur_min = gt.game_duration_ms // 60000
        dur_sec = (gt.game_duration_ms // 1000) % 60
        path_str = str(gt.raw_path) if gt.raw_path else "(no save)"
        print(
            f"  [{gt.source:11}] {gt.external_id:30} {gt.events_count:5} events "
            f"GAME_END={dur_min}:{dur_sec:02d}  → {path_str}"
        )
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

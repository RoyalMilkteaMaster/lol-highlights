"""從 Bilibili 官方號（哔哩哔哩赛事 uid=50329118）找 LPL 官方剪好的場次 VOD。

技術選擇 — 為什麼用 yt-dlp + Firefox cookies：
  Bilibili API 對直接 HTTP 反爬蟲嚴格（412 anti-bot, 352 風控）。
  Selenium headless Chrome 也被偵測（Bilibili 直接不渲染 video data）。
  → 用 yt-dlp + 「Firefox 瀏覽器登入過 bilibili」的 cookies + fake-UA + sleep_requests
     yt-dlp 內建 Bilibili extractor，cookies 帶上後可正常跑。

兩階段 fetch（節省時間）：
  --flat-playlist 拉 user space 前 N 個 BV id（~5 秒，沒 title）
  對每個 BV 個別拉 title（~5 秒/個）→ filter LPL + 日期 + 隊伍
  找到 match → 拉完整 metadata 含 parts duration（用來篩 game vs 採訪）

Filter（user 確認）：
  - title 含 "LPL"
  - title 含 "{month}月{day}日"
  - title 同時含 team_a_code 跟 team_b_code（case-insensitive）
  - 多個候選取第一個（user space 預設 publish desc 排序）
  - Part filter: duration > 1800 秒（30 min）

User setup：
  1. 開 Firefox（Mozilla.org 下載）
  2. Firefox 開 bilibili.com → 登入（任意方式）
  3. config.yaml 設 lpl_cookies_from_browser: "firefox"
  → yt-dlp 自動讀 Firefox cookies.sqlite，繞 anti-bot
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from datetime import date

logger = logging.getLogger(__name__)

DEFAULT_UID = 50329118     # 哔哩哔哩赛事
_UA_INSTANCE = None


# ─────────────────────────────────────────────────────────────────────────────
def _get_random_ua() -> str:
    """fake-useragent 拿隨機 Chrome UA。失敗則用 fixed Chrome UA。"""
    global _UA_INSTANCE
    try:
        if _UA_INSTANCE is None:
            from fake_useragent import UserAgent
            _UA_INSTANCE = UserAgent(browsers=["chrome"], os=["windows"])
        return _UA_INSTANCE.random
    except Exception as e:
        logger.debug("fake-useragent 失敗（%s），fallback 固定 UA", e)
        return ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def _ytdlp_cmd_base(cookies_from_browser: str | None,
                    cookies_file: str | None,
                    sleep_requests: float = 2.0) -> list[str]:
    """yt-dlp 共用參數：cookies + fake-UA + sleep + no-warnings。"""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-warnings",
        "--user-agent", _get_random_ua(),
        "--sleep-requests", str(sleep_requests),
        "--sleep-interval", "1",
        "--max-sleep-interval", "3",
    ]
    if cookies_from_browser:
        cmd.extend(["--cookies-from-browser", cookies_from_browser])
    elif cookies_file:
        cmd.extend(["--cookies", cookies_file])
    return cmd


# ─────────────────────────────────────────────────────────────────────────────
# fast flat-playlist BV id list
# ─────────────────────────────────────────────────────────────────────────────
def _ytdlp_flat_list_bv_ids(
    uid: int,
    max_videos: int = 30,
    *,
    cookies_from_browser: str | None = "firefox",
    cookies_file: str | None = None,
) -> list[str]:
    """快速拿 user space 的 BV id list（沒 title，~5 秒）。"""
    cmd = _ytdlp_cmd_base(cookies_from_browser, cookies_file, sleep_requests=1.0)
    cmd.extend([
        "--flat-playlist",
        "--print", "%(id)s",
        "--playlist-end", str(max_videos),
        f"https://space.bilibili.com/{uid}/video",
    ])
    r = subprocess.run(cmd, capture_output=True, text=True,
                        check=False, encoding="utf-8")
    if r.returncode != 0:
        err_tail = (r.stderr or "")[-300:]
        logger.error("yt-dlp flat list 失敗 rc=%s stderr=%s", r.returncode, err_tail)
        if "412" in err_tail or "352" in err_tail or "blocked" in err_tail.lower():
            logger.error("Bilibili anti-bot 擋了 — 確認 Firefox 已登入 bilibili.com")
        return []
    bvids = [ln.strip() for ln in r.stdout.splitlines() if ln.strip().startswith("BV")]
    logger.info("抓到 %d 個 BV ids（user_uid=%s）", len(bvids), uid)
    return bvids


# ─────────────────────────────────────────────────────────────────────────────
# title only（給 filter 用）
# ─────────────────────────────────────────────────────────────────────────────
def _ytdlp_get_title(
    bvid: str,
    *,
    cookies_from_browser: str | None = "firefox",
    cookies_file: str | None = None,
) -> str | None:
    """拉單一 BV 的 title（不解 parts，~5 秒/個）。"""
    cmd = _ytdlp_cmd_base(cookies_from_browser, cookies_file, sleep_requests=1.0)
    cmd.extend([
        "--print", "%(title)s",
        "--playlist-end", "1",   # 多 part BV 也只看第一個 entry 的 title（含 series 完整 title）
        "--no-download",
        f"https://www.bilibili.com/video/{bvid}",
    ])
    r = subprocess.run(cmd, capture_output=True, text=True,
                        check=False, encoding="utf-8")
    if r.returncode != 0:
        logger.debug("yt-dlp title %s 失敗：%s", bvid, (r.stderr or "")[-200:])
        return None
    title = (r.stdout.strip().splitlines() or [""])[0].strip()
    return title or None


# ─────────────────────────────────────────────────────────────────────────────
# full metadata 拿 parts duration
# ─────────────────────────────────────────────────────────────────────────────
def _ytdlp_get_full_info(
    bvid: str,
    *,
    cookies_from_browser: str | None = "firefox",
    cookies_file: str | None = None,
) -> dict | None:
    """拉單一 BV 完整 metadata（含 entries / duration）。較慢（~10-30 秒視 part 數）。"""
    cmd = _ytdlp_cmd_base(cookies_from_browser, cookies_file, sleep_requests=2.0)
    cmd.extend([
        "--dump-single-json",
        "--no-download",
        f"https://www.bilibili.com/video/{bvid}",
    ])
    r = subprocess.run(cmd, capture_output=True, text=True,
                        check=False, encoding="utf-8")
    if r.returncode != 0:
        logger.warning("yt-dlp full info %s 失敗：%s", bvid, (r.stderr or "")[-300:])
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        logger.warning("yt-dlp full info %s JSON parse 失敗：%s", bvid, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
def find_bv_for_series(
    team_a_code: str,
    team_b_code: str,
    match_date: date,
    *,
    uid: int = DEFAULT_UID,
    max_videos: int = 20,
    cookies_from_browser: str | None = "firefox",
    cookies_file: str | None = None,
) -> dict | None:
    """從哔哩哔哩赛事找對應的 LPL series BV。

    回傳 {bvid, title, duration_sec, created_epoch} 或 None。
    """
    # BV ids
    bvids = _ytdlp_flat_list_bv_ids(
        uid, max_videos,
        cookies_from_browser=cookies_from_browser, cookies_file=cookies_file,
    )
    if not bvids:
        return None

    # iter check title，找到 match 立刻 break
    date_str = f"{match_date.month}月{match_date.day}日"
    ta = team_a_code.upper()
    tb = team_b_code.upper()

    matched_bvid = None
    matched_title = None
    n_lpl = 0
    for i, bvid in enumerate(bvids):
        time.sleep(0.5)   # politeness（除了 yt-dlp 內 sleep_requests）
        title = _ytdlp_get_title(
            bvid,
            cookies_from_browser=cookies_from_browser,
            cookies_file=cookies_file,
        )
        if not title:
            continue
        upper = title.upper()
        if "LPL" in upper:
            n_lpl += 1
        if ("LPL" in upper and date_str in title
                and ta in upper and tb in upper):
            matched_bvid = bvid
            matched_title = title
            logger.info(
                "第 %d/%d 個 match — bvid=%s title=%s",
                i + 1, len(bvids), bvid, title[:80],
            )
            break

    if matched_bvid is None:
        logger.info(
            "%d 個 BV 中 %d 個含 LPL，但都不符合 date=%s + teams=%s vs %s",
            len(bvids), n_lpl, date_str, ta, tb,
        )
        return None

    # 拉 matched BV 的完整 metadata（給 list_bv_parts 用）
    info = _ytdlp_get_full_info(
        matched_bvid,
        cookies_from_browser=cookies_from_browser,
        cookies_file=cookies_file,
    )
    return {
        "bvid": matched_bvid,
        "title": matched_title,
        "duration_sec": int((info or {}).get("duration") or 0),
        "created_epoch": int((info or {}).get("timestamp") or 0),
        "_full_info": info,   # 內部用，list_bv_parts 重複利用避免再 fetch
    }


def list_bv_parts(
    bvid: str,
    *,
    cookies_from_browser: str | None = "firefox",
    cookies_file: str | None = None,
    cached_info: dict | None = None,
) -> list[dict]:
    """列 BV 所有 part with duration。

    `cached_info` 可從 find_bv_for_series 的 _full_info 傳入，避免重複 fetch。

    回傳 [{url, title, duration_sec}, ...]。對單 part BV：list of 1。
    """
    info = cached_info
    if info is None:
        info = _ytdlp_get_full_info(
            bvid,
            cookies_from_browser=cookies_from_browser,
            cookies_file=cookies_file,
        )
    if not info:
        return []

    out = []
    base_url = f"https://www.bilibili.com/video/{bvid}"
    if "entries" in info and info["entries"]:
        for i, e in enumerate(info["entries"], start=1):
            out.append({
                "url": e.get("webpage_url") or f"{base_url}?p={i}",
                "title": e.get("title") or "",
                "duration_sec": int(e.get("duration") or 0),
            })
    else:
        out.append({
            "url": base_url,
            "title": info.get("title") or "",
            "duration_sec": int(info.get("duration") or 0),
        })
    return out

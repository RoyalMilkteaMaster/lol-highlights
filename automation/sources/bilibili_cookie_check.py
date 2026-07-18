"""Bilibili cookies 過期檢查。

讀 Firefox cookies.sqlite 找 SESSDATA cookie 的 expiry。
< 7 天過期 → 跳 Windows toast 提醒 user 重新登入 Firefox。

Firefox cookies 結構：
  ~/AppData/Roaming/Mozilla/Firefox/Profiles/<random>.default-release/cookies.sqlite
  table moz_cookies: name, host, expiry (unix ts seconds), value, ...

SESSDATA 是 Bilibili 登入主 cookie，過期就要重登。

不破壞 Firefox cookies db：先 copy 到 temp 再 sqlite open（Firefox 雖然 WAL 不鎖，
但開著時可能 schema migration 中，copy 比較保險）。
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
def _find_firefox_cookies_db() -> Path | None:
    """找 Firefox 預設 profile 的 cookies.sqlite。"""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    profiles_root = Path(appdata) / "Mozilla" / "Firefox" / "Profiles"
    if not profiles_root.is_dir():
        return None

    # 找 default-release 或 default 結尾的 profile（user 主 profile）
    candidates = []
    for p in profiles_root.iterdir():
        if not p.is_dir():
            continue
        cdb = p / "cookies.sqlite"
        if cdb.is_file():
            score = 0
            if "default-release" in p.name:
                score = 3
            elif p.name.endswith(".default"):
                score = 2
            else:
                score = 1
            candidates.append((score, cdb))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (-x[0], -x[1].stat().st_mtime))
    return candidates[0][1]


def _parse_expiry(expiry_value: int) -> datetime | None:
    """expiry 在 Firefox moz_cookies 可能是 sec / ms / us，依大小判斷。

    epoch 2030 對應：sec ≈ 1.9e9 / ms ≈ 1.9e12 / us ≈ 1.9e15
    """
    if not expiry_value or expiry_value <= 0:
        return None
    v = int(expiry_value)
    if v >= 10**14:
        seconds = v / 1_000_000        # microseconds
    elif v >= 10**11:
        seconds = v / 1_000            # milliseconds
    else:
        seconds = v                    # seconds
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def get_bilibili_sessdata_expiry() -> datetime | None:
    """讀 Firefox cookies 找 bilibili.com SESSDATA cookie 的過期時間。

    回傳 timezone-aware UTC datetime；找不到 cookie 或讀檔失敗回 None。
    """
    db_path = _find_firefox_cookies_db()
    if db_path is None:
        logger.warning("找不到 Firefox cookies.sqlite（user 沒裝 Firefox 或沒 default profile？）")
        return None

    # copy 到 temp 避免動到 Firefox 正在用的 db
    tmp = Path(tempfile.gettempdir()) / "_lol_ff_cookies_copy.sqlite"
    try:
        shutil.copy2(db_path, tmp)
    except OSError as e:
        logger.warning("Firefox cookies copy 失敗：%s", e)
        return None

    try:
        conn = sqlite3.connect(str(tmp))
        cur = conn.execute(
            "SELECT name, host, expiry FROM moz_cookies "
            "WHERE host LIKE ? AND name = ?",
            ("%bilibili.com", "SESSDATA"),
        )
        row = cur.fetchone()
        conn.close()
    except sqlite3.Error as e:
        logger.warning("讀 cookies.sqlite 失敗：%s", e)
        return None
    finally:
        try: tmp.unlink()
        except OSError: pass

    if not row:
        logger.warning("Firefox cookies 內找不到 bilibili.com SESSDATA — user 可能還沒登入")
        return None
    return _parse_expiry(row[2])


# ─────────────────────────────────────────────────────────────────────────────
def show_cookie_expiry_toast(days_left: int, expires_at: datetime) -> None:
    """Windows toast 提醒 user 重新登入 Firefox。"""
    # winotify 用 PowerShell，PATH 補一下避免 subprocess 找不到
    sys_root = os.environ.get("SystemRoot", r"C:\Windows")
    ps_dir = os.path.join(sys_root, "System32", "WindowsPowerShell", "v1.0")
    if ps_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = os.environ.get("PATH", "") + os.pathsep + ps_dir

    try:
        from winotify import Notification, audio
        if days_left <= 0:
            title = "Bilibili Cookies 已過期"
            msg = "LPL 自動下載已暫停。請開 Firefox 重新登入 bilibili.com"
        else:
            title = f"Bilibili Cookies {days_left} 天後過期"
            expire_str = expires_at.astimezone.strftime("%Y-%m-%d %H:%M")
            msg = f"預計 {expire_str} 過期。請開 Firefox 重新登入 bilibili.com 取得新 cookies"
        t = Notification(
            app_id="LoL Highlights",
            title=title,
            msg=msg,
            duration="long",
        )
        # 加 「打開 Bilibili」按鈕
        t.add_actions(label="打開 bilibili.com", launch="https://www.bilibili.com/")
        t.set_audio(audio.Default, loop=False)
        t.show()
        logger.info("已跳 Windows toast：%s", title)
    except Exception:
        logger.exception("跳 toast 失敗（fallback log）")


def check_and_alert(*, alert_threshold_days: int = 7) -> dict:
    """check + alert 主入口。

    回傳 {expires_at, days_left, alerted, status}。
    status: 'ok' / 'expiring' / 'expired' / 'no_cookie'
    """
    expires_at = get_bilibili_sessdata_expiry()
    if expires_at is None:
        # 沒登入或讀不到
        result = {"status": "no_cookie", "expires_at": None,
                  "days_left": None, "alerted": True}
        show_cookie_expiry_toast(0, datetime.now(timezone.utc))
        return result

    now = datetime.now(timezone.utc)
    delta = expires_at - now
    days_left = int(delta.total_seconds() // 86400)

    if delta.total_seconds() <= 0:
        status = "expired"
        show_cookie_expiry_toast(0, expires_at)
        alerted = True
    elif days_left <= alert_threshold_days:
        status = "expiring"
        show_cookie_expiry_toast(days_left, expires_at)
        alerted = True
    else:
        status = "ok"
        alerted = False

    logger.info(
        "Bilibili SESSDATA cookie：expires_at=%s, days_left=%d, status=%s",
        expires_at.isoformat(), days_left, status,
    )
    return {
        "status": status,
        "expires_at": expires_at,
        "days_left": days_left,
        "alerted": alerted,
    }

"""虎撲爬蟲共用底層（UA / sleep / retry / headers）。

設計重點：
- 虎撲 web 版 mini-app API 是公開 endpoint（無 sign / 無 cookie），但仍走「真實使用者風格」：
  * 固定 5 組真實 Chrome UA，每次 random.choice
  * fake_useragent 套件作為 fallback（pilot 期 user 強制要求）
  * 每次 request 前 sleep 2~3 秒 + random 0~1.5 秒
  * tenacity 3 次 retry，exponential backoff（網路抖動 / 5xx 容錯）
- 不要用 `fake_useragent.random` 當主 UA — pilot 試過會抽到 iPhone 18.3.2 之類怪 UA
  （比 hupu 本身還新，反而像 bot）

References：
- pilot 已確認 200 OK + JSON：`https://match-api.hupu.com/1/8.2.10/matchallapi/bff/standard/getScheduleListByTagForH5`
- Origin / Referer 用 `https://bbsactivity.hupu.com/`（hupu mini-app frontend host）
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


# 5 組真實 Chrome UA（PC + mobile），保持版本接近 hupu APP 同期主流瀏覽器
_FIXED_UAS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
]

_FAKE_UA_INSTANCE = None


def get_ua() -> str:
    """主路線：固定 UA list 隨機選。
    Fallback：fake_useragent 套件（pilot 期 user 強制要求要有）。
    """
    try:
        return random.choice(_FIXED_UAS)
    except Exception as e:  # pragma: no cover
        logger.debug("固定 UA 失敗（%s），fallback fake_useragent", e)
        global _FAKE_UA_INSTANCE
        if _FAKE_UA_INSTANCE is None:
            from fake_useragent import UserAgent
            _FAKE_UA_INSTANCE = UserAgent(browsers=["chrome"], os=["windows"])
        return _FAKE_UA_INSTANCE.random


def get_common_headers(ua: str | None = None) -> dict[str, str]:
    """瀏覽器風格的完整 headers。Origin / Referer 模擬從 mini-app frontend 過來。"""
    return {
        "User-Agent": ua or get_ua(),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Origin": "https://bbsactivity.hupu.com",
        "Referer": "https://bbsactivity.hupu.com/",
    }


def polite_sleep(base: float = 2.0, jitter: float = 1.5) -> None:
    """每次 request 前 sleep（base + random[0, jitter]）。
    用 random 是避免規律性（看起來像真實 user 而非 bot）。"""
    delay = base + random.uniform(0, jitter)
    time.sleep(delay)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    retry=retry_if_exception_type((requests.RequestException,)),
    reraise=True,
)
def request_get(
    url: str,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 10.0,
) -> requests.Response:
    """GET with retry（最多 3 次，exp backoff 2~20 秒）。
    抖動 / 5xx / connection error 會自動 retry，4xx 直接拋（不浪費 retry）。
    """
    polite_sleep()
    if headers is None:
        headers = get_common_headers()
    resp = requests.get(url, params=params, headers=headers, timeout=timeout)
    # 4xx 視為 client 問題，不 retry（譬如 endpoint 變了）
    if 400 <= resp.status_code < 500:
        logger.warning("hupu GET %s 回 %d（4xx 不 retry）", url, resp.status_code)
        return resp
    # 5xx raise → tenacity 接住 retry
    resp.raise_for_status()
    return resp


def fetch_json(url: str, params: dict | None = None) -> Any:
    """便利包裝：GET + parse JSON。Status 不 OK 回 None。"""
    try:
        r = request_get(url, params=params)
    except requests.RequestException as e:
        logger.error("hupu fetch_json 最終失敗：%s（url=%s）", e, url)
        return None
    if r.status_code != 200:
        logger.warning("hupu fetch_json status=%d body=%s", r.status_code, r.text[:200])
        return None
    try:
        return r.json()
    except ValueError as e:
        logger.error("hupu JSON parse 失敗：%s body=%s", e, r.text[:200])
        return None

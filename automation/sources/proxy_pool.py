"""Free proxy pool for Leaguepedia Cargo (繞 per-IP rate limit)。

來源：https://github.com/proxifly/free-proxy-list
- HTTP proxies only（不裝 PySocks）
- 5 min refresh + liveness check（socket + lol.fandom.com HEAD）
- Rotation on failure（mark dead, fallback next）

Phase 54 (2026-05-22)：5/22 LCK g1 撞 Leaguepedia 帳號級 rate limit，
單一台灣家用 IP 無解 → 用 free proxy 換 IP 走 Cargo。
"""
from __future__ import annotations

import logging
import random
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# 多 source 並聯（dedup 後 alive 機率提升）
PROXY_LIST_URLS = [
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
]
# 用最便宜的 MediaWiki query 當 liveness target — 直接驗證 proxy 對 Fandom API 是否可用
TEST_TARGET = "https://lol.fandom.com/api.php?action=query&meta=siteinfo&format=json&siprop=general"
POOL_MAX_ALIVE = 10        # 10 個 alive 達標就停 check
RAW_CHECK_LIMIT = 1500     # 全部都 check（Fandom block 嚴重，必須多查）
CHECK_PARALLELISM = 40     # 同時 40 thread 平行 check
REFRESH_INTERVAL_SEC = 300
SOCKET_TIMEOUT = 3.0
HTTP_TEST_TIMEOUT = 6.0
DEAD_MARKER_TTL_SEC = 600  # 10 min 後可重試

_lock = threading.Lock()
_alive: list[str] = []          # ["http://1.2.3.4:8080", ...]
_dead: dict[str, float] = {}    # proxy_url → mark_dead_timestamp
_last_refresh: float = 0.0


def _fetch_raw_list() -> list[str]:
    """並聯多 source 抓 HTTP proxy list，dedup 後回。每行支援：

    - http://ip:port
    - ip:port (沒 schema → 補 http://)
    """
    seen: set[str] = set()
    out: list[str] = []
    for src_url in PROXY_LIST_URLS:
        try:
            r = requests.get(src_url, timeout=10)
            r.raise_for_status()
            count_src = 0
            for line in r.text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # 缺 schema 補上
                if not line.startswith("http"):
                    if ":" in line and line.split(":")[0].count(".") == 3:
                        line = "http://" + line
                    else:
                        continue
                if line in seen:
                    continue
                seen.add(line)
                out.append(line)
                count_src += 1
            logger.info("[proxy_pool] fetched %d new from %s",
                        count_src, src_url.split("/")[-3] + "/" + src_url.split("/")[-2])
        except Exception as e:
            logger.warning("[proxy_pool] %s fetch 失敗: %s", src_url, e)
    logger.info("[proxy_pool] total raw (dedup): %d proxies", len(out))
    return out


def _check_alive(proxy_url: str) -> bool:
    """先 socket connect（快速排除 dead），再 HEAD request 確認可達 target。"""
    try:
        p = urlparse(proxy_url)
        if not p.hostname or not p.port:
            return False
        with socket.create_connection((p.hostname, p.port), timeout=SOCKET_TIMEOUT):
            pass
    except (socket.error, socket.timeout, OSError):
        return False

    try:
        # 改 GET（API 對 HEAD 可能 403/405）；只 verify status + 可解析 JSON
        r = requests.get(
            TEST_TARGET,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=HTTP_TEST_TIMEOUT,
            allow_redirects=False,
            headers={"User-Agent": "lol-highlights/1.0 (he00298902@gmail.com)"},
        )
        if r.status_code != 200:
            return False
        # 確認回 MediaWiki JSON（避免某些 proxy 攔截改成 HTML 公告頁）
        try:
            data = r.json()
            return isinstance(data, dict) and "query" in data
        except ValueError:
            return False
    except Exception:
        return False


def refresh() -> int:
    """並行 filter alive，達到 POOL_MAX_ALIVE 就停。回新增的 alive 個數。"""
    global _alive, _last_refresh
    t0 = time.time()
    raw = _fetch_raw_list()
    if not raw:
        with _lock:
            _last_refresh = time.time()
        return 0
    random.shuffle(raw)
    now = time.time()
    candidates = [u for u in raw[:RAW_CHECK_LIMIT]
                  if u not in _dead or now - _dead[u] >= DEAD_MARKER_TTL_SEC]

    alive_new: list[str] = []
    stop_flag = threading.Event()

    def _try_one(url: str) -> str | None:
        if stop_flag.is_set():
            return None
        return url if _check_alive(url) else None

    with ThreadPoolExecutor(max_workers=CHECK_PARALLELISM) as pool:
        futures = {pool.submit(_try_one, u): u for u in candidates}
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception:
                res = None
            if res:
                alive_new.append(res)
                logger.info("[proxy_pool] alive: %s（pool=%d）", res, len(alive_new))
                if len(alive_new) >= POOL_MAX_ALIVE:
                    stop_flag.set()
                    break
    with _lock:
        _alive = alive_new
        _last_refresh = time.time()
    logger.info("[proxy_pool] refresh 完成：%d alive proxies（耗時 %.0fs，掃 %d）",
                len(alive_new), time.time() - t0, len(candidates))
    return len(alive_new)


def get_current() -> str | None:
    """拿 pool 第一個 alive proxy。空就 None（→ caller 直連 or fallback）。"""
    with _lock:
        need_refresh = not _alive or time.time() - _last_refresh > REFRESH_INTERVAL_SEC
    if need_refresh:
        refresh()
    with _lock:
        return _alive[0] if _alive else None


def mark_dead(proxy_url: str) -> None:
    """proxy 用爛了標 dead + 從 alive list 移除。"""
    with _lock:
        if proxy_url in _alive:
            _alive.remove(proxy_url)
        _dead[proxy_url] = time.time()
        remaining = len(_alive)
    logger.warning("[proxy_pool] mark dead: %s（剩 %d alive）", proxy_url, remaining)


def stats() -> dict:
    """debug / dashboard 用：回 pool 狀態。"""
    with _lock:
        return {
            "alive_count": len(_alive),
            "dead_count": len(_dead),
            "last_refresh_age_sec": time.time() - _last_refresh if _last_refresh else None,
            "alive_first": _alive[0] if _alive else None,
        }

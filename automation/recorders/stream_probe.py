"""輪詢直播流是否可用。

行為：
- 定期跑 `streamlink --json <url>`，解析回傳的 streams dict
- 任一可用 → 立刻回 True
- 超過 max_grace 仍不可用 → False
- 期間有 callback hook 給呼叫端跑 heartbeat


- 不能只看 'streams' in stdout（容易誤判）
- 要 json.loads + dict 非空 + 處理 JSONDecodeError / TimeoutExpired / UnicodeDecodeError
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
from datetime import timedelta
from typing import Callable

from automation.infra.time_utils import utc_now

logger = logging.getLogger(__name__)


def wait_for_stream_available(
    url: str,
    max_grace_min: float = 20.0,
    poll_interval_s: float = 30.0,
    *,
    on_poll: Callable[[], None] | None = None,
    probe_timeout: float = 15.0,
) -> bool:
    """每 poll_interval_s 嘗試 streamlink，直到 stream 可用或超 grace。

    Args:
        url            : 直播 URL
        max_grace_min  : 最多等多少分鐘
        poll_interval_s: 每次嘗試間隔
        on_poll        : 每次 poll 前呼叫的 hook（用來做 heartbeat 更新）
        probe_timeout  : 單次 streamlink 呼叫的 timeout
    """
    deadline = utc_now() + timedelta(minutes=max_grace_min)
    poll_count = 0
    while utc_now() < deadline:
        if on_poll:
            try:
                on_poll()
            except Exception as e:
                logger.warning("stream_probe on_poll hook 失敗：%s", e)

        poll_count += 1
        try:
            r = subprocess.run(
                [sys.executable, "-m", "streamlink", "--json", url],
                capture_output=True, timeout=probe_timeout, check=False,
            )
            if r.returncode == 0 and r.stdout:
                try:
                    text = r.stdout.decode("utf-8", errors="replace")
                    data = json.loads(text)
                    streams = data.get("streams", {})
                    if isinstance(streams, dict) and streams:
                        logger.info(
                            "stream_probe: %d 個 stream 可用（%s），第 %d 次 poll 命中",
                            len(streams), ",".join(list(streams.keys())[:3]),
                            poll_count,
                        )
                        return True
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    logger.debug("stream_probe parse 失敗：%s", e)
            else:
                logger.debug(
                    "stream_probe rc=%d, stderr=%r",
                    r.returncode, (r.stderr or b"")[:200],
                )
        except subprocess.TimeoutExpired:
            logger.debug("stream_probe timeout")
        except Exception as e:
            logger.debug("stream_probe 例外：%s", e)

        time.sleep(poll_interval_s)

    logger.warning(
        "stream_probe: %s 在 %.1f 分鐘內仍不可用（poll %d 次）",
        url, max_grace_min, poll_count,
    )
    return False

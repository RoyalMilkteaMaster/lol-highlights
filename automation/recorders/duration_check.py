"""ffprobe 取 mp4 時長 + min_valid_duration 檢查。

避免 streamlink 10 秒就退出 → ffmpeg 合併出超短 mp4 被當成「錄完」。
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def get_duration_sec(mp4_path: Path | str, timeout: float = 30.0) -> float:
    """ffprobe 拿 mp4 時長（秒）。失敗回 -1。"""
    p = Path(mp4_path) if isinstance(mp4_path, str) else mp4_path
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                str(p),
            ],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        if r.returncode != 0:
            return -1.0
        out = r.stdout.strip()
        if not out:
            return -1.0
        return float(out)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return -1.0


def is_valid_recording(mp4_path: Path | str, min_seconds: float = 300) -> tuple[bool, float]:
    """檢查錄影 mp4 是否達最低時長。

    Returns: (is_valid, duration_sec)
    """
    duration = get_duration_sec(mp4_path)
    if duration < 0:
        return False, duration
    return duration >= min_seconds, duration

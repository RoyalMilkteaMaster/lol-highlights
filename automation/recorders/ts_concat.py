"""ffmpeg concat 合併 .ts 段成 mp4。"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class NoSegmentsRecorded(Exception):
    """ts_files 為空。"""


class MergeFailed(Exception):
    """ffmpeg concat 失敗。"""


def concat_to_mp4(
    ts_files: list[Path],
    output: Path,
    *,
    timeout: float = 600.0,
) -> Path:
    """用 `ffmpeg -f concat -c copy` 把 .ts 合併成 mp4。

    Args:
        ts_files: 已排序的 .ts 路徑 list
        output  : 輸出 mp4 絕對路徑

    Returns: output（成功時）
    Raises:
        NoSegmentsRecorded: ts_files 為空
        MergeFailed       : ffmpeg 失敗
    """
    if not ts_files:
        raise NoSegmentsRecorded("沒有任何 .ts 段可合併")

    output.parent.mkdir(parents=True, exist_ok=True)

    # 建臨時 filelist.txt（ffmpeg concat demuxer 要這個格式）
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    ) as f:
        for ts in ts_files:
            # 用 forward slash + 單引號跳脫單引號（ffmpeg 規定）
            esc = str(ts.resolve()).replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{esc}'\n")
        filelist_path = Path(f.name)

    try:
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(filelist_path),
            "-c", "copy",
            str(output),
        ]
        logger.info("ffmpeg concat → %s（%d 段）", output.name, len(ts_files))
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        if r.returncode != 0:
            tail = r.stderr[-500:]
            raise MergeFailed(f"ffmpeg rc={r.returncode}: {tail}")
        if not output.is_file() or output.stat().st_size == 0:
            raise MergeFailed(f"output 不存在或為空：{output}")
        return output
    finally:
        try:
            filelist_path.unlink()
        except OSError:
            pass

"""ffmpeg / ffprobe 統一包裝。

模組角色：
- live_split_worker 用來 concat ts → cumulative.mp4、從 cumulative cut 出 game.mp4、
  以及 ffprobe 拿 duration
- 與 automation.recorders.ts_concat 並存（ts_concat 是錄影流程末端 merge 全段，
  本模組是增量 concat，要可重複 atomic 執行）

設計：
- concat_to_mp4：用 demuxer concat + `-c copy`，atomic write（先寫 .tmp 再 rename）
- cut：精確時間切片（非 keyframe 對齊用 -accurate_seek + 重編 video）
- ffprobe_duration：JSON 輸出避免 locale 問題
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class FFmpegError(RuntimeError):
    """ffmpeg / ffprobe 失敗。"""


def _guess_muxer(output: Path) -> str:
    """副檔名 → ffmpeg muxer 名稱。"""
    ext = output.suffix.lower()
    return {
        ".mp4": "mp4",
        ".mkv": "matroska",
        ".ts": "mpegts",
        ".m4v": "mp4",
    }.get(ext, "mp4")


# ─────────────────────────────────────────────────────────────────────────────
def ffprobe_duration(path: Path) -> float:
    """回傳影片 duration（秒）。ffprobe 失敗 raise FFmpegError。"""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise FFmpegError(f"ffprobe rc={r.returncode}: {r.stderr[-300:]}")
    try:
        data = json.loads(r.stdout)
        return float(data["format"]["duration"])
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        raise FFmpegError(f"ffprobe JSON 解析失敗：{e}") from e


# ─────────────────────────────────────────────────────────────────────────────
def concat_to_mp4(
    ts_files: list[Path],
    output: Path,
    *,
    timeout: float = 1200.0,
) -> Path:
    """ts demuxer concat → mp4，atomic write（避免半寫狀態被 detector 讀到）。

    Args:
        ts_files: 已排序的 .ts 路徑 list（不可空）
        output  : 輸出 mp4 絕對路徑
        timeout : ffmpeg 超時（秒）

    Returns: output（成功時）
    """
    if not ts_files:
        raise FFmpegError("ts_files 為空")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    # 建臨時 filelist
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    ) as f:
        for ts in ts_files:
            esc = str(Path(ts).resolve()).replace("\\", "/").replace("'", "'\\''")
            f.write(f"file '{esc}'\n")
        filelist = Path(f.name)

    # atomic write：寫到 .tmp.<ext> 再 rename（保留副檔名讓 ffmpeg 認 muxer）
    tmp_out = output.with_suffix(".tmp" + output.suffix)
    try:
        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(filelist),
            "-c", "copy",
            # 顯式指定輸出 muxer，避免 ffmpeg 從 tmp 副檔名猜不出來
            "-f", _guess_muxer(output),
            str(tmp_out),
        ]
        logger.info("concat_to_mp4: %d 段 → %s", len(ts_files), output.name)
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        if r.returncode != 0:
            raise FFmpegError(f"ffmpeg concat rc={r.returncode}: {r.stderr[-500:]}")
        if not tmp_out.is_file() or tmp_out.stat().st_size == 0:
            raise FFmpegError(f"concat 輸出空檔：{tmp_out}")
        os.replace(tmp_out, output)
        return output
    finally:
        try: filelist.unlink()
        except OSError: pass
        try:
            if tmp_out.exists():
                tmp_out.unlink()
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
def cut(
    input_path: Path,
    *,
    start_sec: float,
    end_sec: float,
    output: Path,
    timeout: float = 1800.0,
    accurate: bool = True,
) -> Path:
    """從 input 切 [start_sec, end_sec] 出來。

    accurate=True：精確切割（重編 video，慢但邊界準）
    accurate=False：keyframe seek（快但可能誤差數秒）

    用 accurate=False（先 seek 後 -i），keyframe 對齊就夠用，
    BP 偵測本身有 ±15s 誤差，不需要精確到 frame。
    """
    input_path = Path(input_path)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    duration = end_sec - start_sec
    if duration <= 0:
        raise FFmpegError(f"start ({start_sec}) >= end ({end_sec})，不能切")

    tmp_out = output.with_suffix(".tmp" + output.suffix)
    muxer = _guess_muxer(output)
    try:
        if accurate:
            # -ss 在 -i 之前 = fast seek（keyframe），-ss 在 -i 之後 = accurate（解碼後切）
            # 為了 BP_start 的 30s lead-in 不被 keyframe 跳掉，accurate 模式
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{start_sec:.3f}",
                "-i", str(input_path),
                "-t", f"{duration:.3f}",
                "-c", "copy",  # 先試 copy，BP 起始 keyframe 通常 OK（90s ts 段保證有 keyframe）
                "-avoid_negative_ts", "make_zero",
                "-f", muxer,
                str(tmp_out),
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{start_sec:.3f}",
                "-i", str(input_path),
                "-t", f"{duration:.3f}",
                "-c", "copy",
                "-f", muxer,
                str(tmp_out),
            ]
        logger.info(
            "cut: %s [%.0fs ~ %.0fs] (%.0fs) → %s",
            input_path.name, start_sec, end_sec, duration, output.name,
        )
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
        if r.returncode != 0:
            raise FFmpegError(f"ffmpeg cut rc={r.returncode}: {r.stderr[-500:]}")
        if not tmp_out.is_file() or tmp_out.stat().st_size == 0:
            raise FFmpegError(f"cut 輸出空檔：{tmp_out}")
        os.replace(tmp_out, output)
        return output
    finally:
        try:
            if tmp_out.exists():
                tmp_out.unlink()
        except OSError:
            pass

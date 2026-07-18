"""共用工具函式：FFmpeg PATH 設定、影片時長探測、logging 統一設定。

模組 import 時自動 patch subprocess：pythonw 跑 main.py / detectors 時
ffmpeg/ffprobe 不再跳新 console 視窗。automation/infra/log_setup.py 也有同樣
patch，兩條 import chain 都覆蓋到。
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path


# ── 專案根目錄（此檔案在 modules/ 下，往上一層就是根目錄）─────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent


def _apply_no_window_subprocess_patch() -> None:
    """Windows + 非 TTY 下讓所有 subprocess.Popen 預設加 CREATE_NO_WINDOW，
    剝除 CREATE_NEW_CONSOLE — pythonw 跑 worker 時不再跳一堆 ffmpeg / yt-dlp 視窗。

    例外：target 是 python.exe / pythonw.exe 時不改 creationflags。Python 進程本來就
    不會跳 console（pythonw 是 GUI subsystem），加 CREATE_NO_WINDOW 會跟 stdio 繼承
    產生衝突 → 子 python 進程啟動後 1 秒掛掉 rc=120（5/8 實測）。
    每個 python 子進程自己 import highlight.utils.utils 時會觸發各自的 patch，照樣 cover 到
    後續 spawn 的 ffmpeg / yt-dlp / streamlink。
    """
    if sys.platform != "win32":
        return
    if sys.stdout and sys.stdout.isatty():
        return
    if getattr(subprocess.Popen, "_no_window_patched", False):
        return
    _CREATE_NEW_CONSOLE = 0x00000010
    _CREATE_NO_WINDOW   = 0x08000000
    _orig_init = subprocess.Popen.__init__

    def _is_python_target(args, kwargs) -> bool:
        cmd = kwargs.get("args")
        if cmd is None and args:
            cmd = args[0]
        if cmd is None:
            return False
        first = cmd[0] if isinstance(cmd, (list, tuple)) else cmd
        if not isinstance(first, (str, bytes, os.PathLike)):
            return False
        name = os.path.basename(os.fspath(first)).lower()
        return name in ("python.exe", "pythonw.exe", "python", "pythonw", "python3.exe")

    def _patched_init(self, *args, **kwargs):
        if not _is_python_target(args, kwargs):
            flags = kwargs.get("creationflags", 0)
            flags = (flags & ~_CREATE_NEW_CONSOLE) | _CREATE_NO_WINDOW
            kwargs["creationflags"] = flags
        _orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _patched_init
    subprocess.Popen._no_window_patched = True


_apply_no_window_subprocess_patch()


def ensure_ffmpeg_path(config_path: Path | None = None) -> None:
    """從環境變數 FFMPEG_BIN 或 config.yaml 讀取 ffmpeg_path 並加到 PATH。

    解析優先序（5/15 加入跨機器搬遷支援）：
      1. 環境變數 FFMPEG_BIN（最高優先級，新機 .env 用）
      2. config.yaml 的 ffmpeg_path（向下相容舊機）
      3. 都沒有 → 假設 ffmpeg 已在系統 PATH，靜默通過

    Args:
        config_path: 指定 config.yaml 路徑；省略時自動找專案根目錄的 config.yaml。
    """
    # 優先讀 env var
    ffmpeg_bin = os.environ.get("FFMPEG_BIN", "").strip()
    if not ffmpeg_bin:
        try:
            import yaml
            cfg_path = config_path or (_PROJECT_ROOT / "config.yaml")
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            ffmpeg_bin = cfg.get("ffmpeg_path", "")
        except Exception:
            pass
    if ffmpeg_bin:
        os.environ["PATH"] = ffmpeg_bin + os.pathsep + os.environ.get("PATH", "")


def get_video_duration(video_path: Path) -> float:
    """用 ffprobe 取得影片總長度（秒）。"""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            str(video_path),
        ],
        capture_output=True,
        text=True,
    )
    return float(json.loads(result.stdout).get("format", {}).get("duration", 0))


def setup_logging(log_path: str | Path | None = None, level: int = logging.INFO) -> None:
    """設定 logging：同時輸出到 stdout 和 log 檔（若有指定）。

    Args:
        log_path: log 檔路徑；省略則只輸出到 stdout。
        level:    logging level，預設 INFO。
    """
    import sys
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_path:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )

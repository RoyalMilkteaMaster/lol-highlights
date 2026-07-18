"""Rotating log helper。

設計：
- TimedRotatingFileHandler：每天 1 檔，保留 14 天
- UTF-8 編碼
- 同時輸出 stdout（INFO+）
- **開檔前先 mkdir logs/**
- 模組 import 時自動 patch subprocess：pythonw 跑 worker 時 ffmpeg/yt-dlp/
  streamlink 不再跳新 console 視窗。highlight/utils/utils.py 也有同樣 patch，cover main.py 路徑。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_DEFAULT_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _apply_no_window_subprocess_patch() -> None:
    """Windows + 非 TTY（pythonw 或 output redirect）下，所有 subprocess.Popen
    自動加 CREATE_NO_WINDOW + 剝除 CREATE_NEW_CONSOLE。

    為什麼：pythonw.exe 沒 console，每個 console 子程式（ffmpeg/yt-dlp/streamlink）
    Windows 預設會自動建一個新 console 視窗 → 螢幕被視窗淹沒。CREATE_NO_WINDOW 把
    新 console 隱藏起來，stdout/stderr 仍可透過 PIPE / 檔案接住。

    為什麼剝 CREATE_NEW_CONSOLE：scheduler.py 之前顯式指定要新 console（給 recorder
    自己一個視窗看 log），但 user 改 pythonw 後不想看到視窗 → 強制覆蓋意圖。

    例外：target 是 python.exe / pythonw.exe 時不改 creationflags。Python 進程本來就
    不會跳 console，加 CREATE_NO_WINDOW 會跟 stdio 繼承衝突 → 子 python 啟動後 1 秒
    掛掉 rc=120。
    每個 python 子進程自己 import log_setup / highlight.utils.utils 時會觸發各自 patch。
    """
    import os
    if sys.platform != "win32":
        return
    # console 模式（python.exe + 互動）保留原行為，不 patch
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


def setup_rotating_log(
    name: str,
    log_path: Path,
    *,
    level: int = logging.INFO,
    backup_count: int = 14,
    fmt: str = _DEFAULT_FMT,
    add_stdout: bool = True,
) -> logging.Logger:
    """建立 rotating log。

    Args:
        name        : logger name（也可用 module __name__）
        log_path    : log 檔絕對路徑（會自動 mkdir 父目錄）
        level       : log 等級
        backup_count: 保留多少天的舊檔
        fmt         : log format
        add_stdout  : 是否同時輸出 stdout
    """
    # 開檔前 mkdir
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    # 避免 reload 時重複加 handler
    if any(getattr(h, "_rotating_log_setup", False) for h in logger.handlers):
        return logger

    # automation.run 已把同一個檔案掛在 root logger 時，直接向上傳遞即可。
    target = log_path.resolve()
    if any(
        Path(getattr(h, "baseFilename", "")).resolve() == target
        for h in logging.getLogger().handlers
        if getattr(h, "baseFilename", None)
    ):
        logger.propagate = True
        return logger

    formatter = logging.Formatter(fmt)

    # 每天 1 檔，半夜 rotate
    fh = TimedRotatingFileHandler(
        str(log_path),
        when="midnight",
        backupCount=backup_count,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    fh.setLevel(level)
    fh._rotating_log_setup = True
    logger.addHandler(fh)

    if add_stdout and sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(formatter)
        sh.setLevel(level)
        sh._rotating_log_setup = True
        logger.addHandler(sh)

    logger.propagate = False
    return logger

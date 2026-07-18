"""集中化路徑解析 — 5/15 加入，為跨機器搬遷做準備。

設計：每個常用路徑都優先讀環境變數，沒設則 fall back 到舊機硬編路徑。
這樣舊機（沒 .env 內路徑變數）行為完全不變；新機 .env 設了 VIDEO_DIR/OUTPUT_DIR
就會自動指過去，源碼不用因換機而修改。

使用：
    from highlight.utils import paths
    split_dir = paths.split_dir()           # 預設 E:/videos/split，env 設了就用新值
    ffmpeg = paths.ffmpeg_exe()             # 預設 winget 路徑 + ffmpeg.exe

環境變數：
    VIDEO_DIR     —— 影片輸入根 (預設 E:/videos)
    OUTPUT_DIR    —— 輸出根 (預設 F:/lol-highlights/output)
    FFMPEG_BIN    —— ffmpeg 所在資料夾 (預設舊機 winget 路徑)
"""
import os
from pathlib import Path

# 載入專案根 .env，讓 VIDEO_DIR / OUTPUT_DIR / FFMPEG_BIN 覆寫生效（跨機器搬遷用）。
# 原設計假設 .env 會被載入，但 highlight 進入點沒做；放這裡確保所有 consumer 都吃得到。
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
except Exception:
    pass

# ── 預設值（舊機路徑，相當於 .env 沒設時的 fall back）─────────────────────────
_DEFAULT_VIDEO_DIR = "E:/videos"
_DEFAULT_OUTPUT_DIR = "F:/lol-highlights/output"
_DEFAULT_FFMPEG_BIN = (
    "C:/Users/lesli/AppData/Local/Microsoft/WinGet/Packages/"
    "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1-full_build/bin"
)


# ── 根目錄 ────────────────────────────────────────────────────────────────────
def videos_dir() -> Path:
    """影片輸入根（split, scan, live_recordings, lol_games_vods...）。"""
    return Path(os.environ.get("VIDEO_DIR", _DEFAULT_VIDEO_DIR))


def output_dir() -> Path:
    """剪輯輸出根（raw, final, finals, *.log）。"""
    return Path(os.environ.get("OUTPUT_DIR", _DEFAULT_OUTPUT_DIR))


def ffmpeg_bin() -> Path:
    """ffmpeg / ffprobe 所在資料夾。"""
    return Path(os.environ.get("FFMPEG_BIN", _DEFAULT_FFMPEG_BIN))


# ── videos/ 子目錄 ─────────────────────────────────────────────────────────────
def split_dir() -> Path:
    return videos_dir() / "split"


def scan_dir() -> Path:
    return videos_dir() / "scan"


def lol_vods_dir() -> Path:
    return videos_dir() / "lol_vods"


def live_recordings_dir() -> Path:
    return videos_dir() / "live_recordings"


def lol_games_vods_dir() -> Path:
    return videos_dir() / "lol_games_vods"


def finals_dir() -> Path:
    return videos_dir() / "finals"


def manual_inbox_dir() -> Path:
    return videos_dir() / "manual_inbox"


def timelines_dir() -> Path:
    return videos_dir() / "timelines"


# ── output/ 子目錄 ─────────────────────────────────────────────────────────────
def raw_dir() -> Path:
    return output_dir() / "raw"


def final_dir() -> Path:
    return output_dir() / "final"


def cut_progress_log() -> Path:
    return output_dir() / "cut_progress.log"


# ── ffmpeg 執行檔 ─────────────────────────────────────────────────────────────
def ffmpeg_exe() -> Path:
    """跨平台：Windows 加 .exe，Linux 直接 ffmpeg。"""
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    return ffmpeg_bin() / name


def ffprobe_exe() -> Path:
    name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    return ffmpeg_bin() / name


# ── debug helper ──────────────────────────────────────────────────────────────
def dump_resolved() -> dict:
    """印出目前所有解析後的路徑，給 check_env.bat 用。"""
    return {
        "VIDEO_DIR": str(videos_dir()),
        "OUTPUT_DIR": str(output_dir()),
        "FFMPEG_BIN": str(ffmpeg_bin()),
        "split_dir": str(split_dir()),
        "scan_dir": str(scan_dir()),
        "finals_dir": str(finals_dir()),
        "manual_inbox_dir": str(manual_inbox_dir()),
        "timelines_dir": str(timelines_dir()),
        "raw_dir": str(raw_dir()),
        "final_dir": str(final_dir()),
        "ffmpeg_exe": str(ffmpeg_exe()),
        "ffprobe_exe": str(ffprobe_exe()),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(dump_resolved(), indent=2, ensure_ascii=False))

"""check_env.bat 的實作。獨立 Python 檔，避開 cmd inline 引號地獄。

跑 7 項健診，每項都印 OK 或 FAIL + 建議。最後印總結 + 退出碼。

直接執行：
    cd <project-directory>
    python deployment\\_check_env_runner.py

正常用法是經由 deployment\\check_env.bat 包裝（會幫你設好 env）。
"""
import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

PASS_COUNT = 0
FAIL_COUNT = 0
RESULTS = []


def check(name: str, fn):
    global PASS_COUNT, FAIL_COUNT
    print(f"\n[{name}]")
    try:
        msg = fn()
        print(f"  OK: {msg}")
        RESULTS.append((name, True, msg))
        PASS_COUNT += 1
    except Exception as e:
        print(f"  FAIL: {e}")
        RESULTS.append((name, False, str(e)))
        FAIL_COUNT += 1


# ── 1. Python 是 lol-env 3.10 ─────────────────────────────────────────────────
def check_python():
    v = sys.version_info
    if v.major != 3 or v.minor != 10:
        raise RuntimeError(
            f"Python {v.major}.{v.minor} (要 3.10) — 跑錯 interpreter？"
            f"\n      應該用：lol-env 內的 python.exe"
        )
    return f"Python {v.major}.{v.minor}.{v.micro} @ {sys.executable}"


# ── 2. CUDA / PyTorch ─────────────────────────────────────────────────────────
def check_cuda():
    import torch
    if not torch.cuda.is_available():
        raise RuntimeError(
            "torch.cuda.is_available() = False — 重灌 NVIDIA driver"
        )
    return f"torch {torch.__version__}, CUDA {torch.version.cuda}, GPU: {torch.cuda.get_device_name(0)}"


# ── 3. ffmpeg / ffprobe ───────────────────────────────────────────────────────
def check_ffmpeg():
    from highlight.utils.utils import ensure_ffmpeg_path
    import shutil
    import subprocess
    ensure_ffmpeg_path()
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "ffmpeg 不在 PATH — 設 .env 內 FFMPEG_BIN，或裝 ffmpeg 到 C:\\tools\\ffmpeg\\bin"
        )
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe 不在 PATH（通常跟 ffmpeg 同資料夾）")
    out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
    first_line = out.stdout.split("\n")[0]
    return first_line


# ── 4. MySQL 連線 ─────────────────────────────────────────────────────────────
def check_mysql():
    from automation.db.connection import mysql_conn
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM broadcasts")
            row = cur.fetchone()
            n = row["c"] if isinstance(row, dict) else row[0]
    return f"MySQL 通，broadcasts 表有 {n} 筆"


# ── 5. 必要資料夾 ─────────────────────────────────────────────────────────────
def check_dirs():
    from highlight.utils import paths
    # lol_vods_dir 是 legacy「手動下載整天 VOD」入口；全自動流程走 live_recordings/ + lol_games_vods/
    # 不再強制要求存在
    targets = [
        ("split_dir", paths.split_dir()),
        ("scan_dir", paths.scan_dir()),
        ("live_recordings_dir", paths.live_recordings_dir()),
        ("lol_games_vods_dir", paths.lol_games_vods_dir()),
        ("finals_dir", paths.finals_dir()),
        ("manual_inbox_dir", paths.manual_inbox_dir()),
        ("raw_dir", paths.raw_dir()),
        ("final_dir", paths.final_dir()),
    ]
    missing = [name for name, p in targets if not p.exists()]
    if missing:
        raise RuntimeError(
            f"缺資料夾：{missing}（VIDEO_DIR={paths.videos_dir()}, OUTPUT_DIR={paths.output_dir()}）"
        )
    return f"全部 {len(targets)} 個資料夾都在（VIDEO_DIR={paths.videos_dir()}, OUTPUT_DIR={paths.output_dir()}）"


# ── 6. YOLO 模型檔 ────────────────────────────────────────────────────────────
def check_models():
    root = PROJECT_ROOT / "highlight" / "assets" / "yolo_models"
    required = [
        "lol_detector.pt",
        "kill_feed_yolo11s.pt",
        "end_graph_yolov8n.pt",
        "champ_hp_detector.pt",
    ]
    missing = [m for m in required if not (root / m).exists()]
    if missing:
        raise RuntimeError(f"缺模型：{missing}")
    sizes_mb = {m: (root / m).stat().st_size / 1024 / 1024 for m in required}
    sizes_str = ", ".join(f"{m.split('.')[0]}={sz:.0f}MB" for m, sz in sizes_mb.items())
    return f"4 個模型都在（{sizes_str}）"


# ── 7. 真的跑 YOLO inference（防 silent CPU fallback）────────────────────────
def check_yolo_gpu():
    from ultralytics import YOLO
    model_path = PROJECT_ROOT / "highlight" / "assets" / "yolo_models" / "kill_feed_yolo11s.pt"
    m = YOLO(str(model_path))
    m.to("cuda:0")
    dev = str(next(m.model.parameters()).device)
    if "cuda" not in dev:
        raise RuntimeError(f"模型 fall back 到 CPU（device={dev}）")
    return f"kill_feed model 載入 GPU（device={dev}）"


def main():
    print("=" * 55)
    print("  LoL Highlights 環境健診")
    print(f"  專案：{PROJECT_ROOT}")
    print("=" * 55)

    check("1/7 Python 解譯器", check_python)
    check("2/7 CUDA / PyTorch", check_cuda)
    check("3/7 ffmpeg / ffprobe", check_ffmpeg)
    check("4/7 MySQL 連線", check_mysql)
    check("5/7 VIDEO_DIR / OUTPUT_DIR 資料夾", check_dirs)
    check("6/7 YOLO 模型檔", check_models)
    check("7/7 YOLO GPU inference", check_yolo_gpu)

    print()
    print("=" * 55)
    if FAIL_COUNT == 0:
        print(f"  通過 {PASS_COUNT}/7 — 環境健康，可以開工")
    else:
        print(f"  通過 {PASS_COUNT}/7，失敗 {FAIL_COUNT} — 看上面 FAIL 行")
    print("=" * 55)

    return 0 if FAIL_COUNT == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

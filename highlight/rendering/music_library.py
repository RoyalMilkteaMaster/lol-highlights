"""
music_library.py — 音樂庫管理工具

用途：
  1. 批次從 YouTube 下載音樂到本地
  2. 掃描目錄中所有音樂，偵測 BPM 並建立 catalog.json
  3. 按 BPM 範圍列出符合的曲目

使用方式：
  python -m highlight.rendering.music_library download --urls assets/music_urls.txt [--out assets/music/]
  python -m highlight.rendering.music_library scan     [--dir assets/music/]
  python -m highlight.rendering.music_library list     [--dir assets/music/] [--bpm-min 120] [--bpm-max 130]

urls.txt 格式（預設位置：assets/music_urls.txt）：每行一個 YouTube URL，# 開頭為註解，空行跳過。

catalog.json 格式（儲存在 --dir 目錄下）：
  {
    "bgm_1.m4a": {"bpm": 124.3, "duration": 241.5, "title": "bgm_1", "mtime": 1700000000.0},
    ...
  }
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

# 讓 import modules 找得到（tools/ 的上一層就是專案根目錄）
sys.path.insert(0, str(Path(__file__).parent.parent))

from highlight.utils.utils import ensure_ffmpeg_path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CATALOG_FILENAME = "catalog.json"
AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".flac", ".ogg"}


# ─────────────────────────────────────────────────────────────────────────────
# download 子命令
# ─────────────────────────────────────────────────────────────────────────────

def cmd_download(args):
    urls_file = Path(args.urls)
    if not urls_file.exists():
        logger.error(f"找不到 URL 清單：{urls_file}")
        sys.exit(1)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    urls = []
    for line in urls_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)

    if not urls:
        logger.warning("URL 清單是空的，無事可做")
        return

    logger.info(f"準備下載 {len(urls)} 首音樂 → {out_dir}")
    ensure_ffmpeg_path()

    for i, url in enumerate(urls, 1):
        logger.info(f"[{i}/{len(urls)}] 下載：{url}")
        cmd = [
            "yt-dlp",
            "-x",
            "--audio-format", "m4a",
            "--audio-quality", "0",
            "-o", str(out_dir / "%(title)s.%(ext)s"),
            "--no-playlist",
            "--no-overwrites",
            url,
        ]
        result = subprocess.run(cmd, capture_output=False, text=True)
        if result.returncode != 0:
            logger.warning(f"  下載失敗（exit {result.returncode}），跳過")

    logger.info(f"\n下載完成，檔案儲存在：{out_dir}")
    logger.info("建議接著執行：python tools/music_library.py scan")


# ─────────────────────────────────────────────────────────────────────────────
# scan 子命令
# ─────────────────────────────────────────────────────────────────────────────

def cmd_scan(args):
    ensure_ffmpeg_path()
    music_dir = Path(args.dir)
    if not music_dir.exists():
        logger.error(f"目錄不存在：{music_dir}")
        sys.exit(1)

    catalog_path = music_dir / CATALOG_FILENAME
    catalog = {}
    if catalog_path.exists():
        try:
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("catalog.json 讀取失敗，重新建立")
            catalog = {}

    audio_files = [
        f for f in music_dir.iterdir()
        if f.suffix.lower() in AUDIO_EXTS and f.name != CATALOG_FILENAME
    ]

    if not audio_files:
        logger.warning(f"在 {music_dir} 找不到任何音訊檔案")
        return

    logger.info(f"找到 {len(audio_files)} 個音訊檔案，開始掃描 BPM...")

    try:
        import librosa
    except ImportError:
        logger.error(
            "librosa 未安裝。請執行：pip install librosa soundfile\n"
            "（若 conda 環境可用：conda install -c conda-forge librosa）"
        )
        sys.exit(1)

    updated = 0
    for f in sorted(audio_files):
        current_mtime = f.stat().st_mtime
        existing = catalog.get(f.name, {})

        if existing and abs(existing.get("mtime", 0) - current_mtime) < 1.0:
            logger.info(f"  [OK] {f.name:50s}  BPM={existing['bpm']:.1f}  (已有紀錄，跳過)")
            continue

        logger.info(f"  [...] 掃描 {f.name}...")
        try:
            bpm, duration = _detect_bpm(f, librosa)
            catalog[f.name] = {
                "bpm":      round(bpm, 1),
                "duration": round(duration, 1),
                "title":    f.stem,
                "mtime":    current_mtime,
            }
            logger.info(f"  [OK] {f.name:50s}  BPM={bpm:.1f}  時長={duration:.0f}s")
            updated += 1
        except Exception as e:
            logger.warning(f"  [X] {f.name}：偵測失敗 ({e})")

    catalog_path.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"\n掃描完成，更新 {updated} 首，catalog 已寫入：{catalog_path}")


def _detect_bpm(audio_path: Path, librosa) -> tuple[float, float]:
    """
    用 FFmpeg pipe + librosa 偵測音訊 BPM 與時長。
    只分析前 90 秒，速度快。
    """
    import numpy as np

    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", str(audio_path)],
        capture_output=True,
    )
    probe_text = probe.stdout.decode("utf-8", errors="replace")
    total_duration = float(
        json.loads(probe_text).get("format", {}).get("duration", 0)
    )

    sr = 22050
    analyse_sec = min(90.0, total_duration)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(audio_path),
        "-t", str(analyse_sec),
        "-ac", "1",
        "-ar", str(sr),
        "-f", "f32le",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0 or len(proc.stdout) == 0:
        stderr_msg = proc.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"FFmpeg 解碼失敗: {stderr_msg}")

    y = np.frombuffer(proc.stdout, dtype=np.float32)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)

    if hasattr(tempo, "__len__"):
        tempo = float(tempo[0])
    else:
        tempo = float(tempo)

    if tempo < 80:
        tempo *= 2.0
    elif tempo > 160:
        tempo /= 2.0

    return tempo, total_duration


# ─────────────────────────────────────────────────────────────────────────────
# list 子命令
# ─────────────────────────────────────────────────────────────────────────────

def cmd_list(args):
    music_dir = Path(args.dir)
    catalog_path = music_dir / CATALOG_FILENAME

    if not catalog_path.exists():
        logger.error("找不到 catalog.json，請先執行：python tools/music_library.py scan")
        sys.exit(1)

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))

    bpm_min = args.bpm_min
    bpm_max = args.bpm_max

    matches = {
        name: info for name, info in catalog.items()
        if bpm_min <= info["bpm"] <= bpm_max
    }

    print(f"\nBPM 範圍 {bpm_min}~{bpm_max} 的曲目（共 {len(matches)}/{len(catalog)} 首）：")
    print("-" * 70)
    for name, info in sorted(matches.items(), key=lambda x: x[1]["bpm"]):
        mins, secs = divmod(int(info["duration"]), 60)
        print(f"  {info['bpm']:6.1f} BPM  {mins}:{secs:02d}  {name}")
    print("-" * 70)

    all_bpms = sorted(catalog.values(), key=lambda x: x["bpm"])
    print(f"\n全部 {len(catalog)} 首的 BPM 分布：")
    for info in all_bpms:
        bar = "█" * int(info["bpm"] / 10)
        print(f"  {info['bpm']:6.1f}  {bar}  {info['title']}")


# ─────────────────────────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="音樂庫管理工具：下載 / BPM 掃描 / 篩選",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例：
  python tools/music_library.py download --urls assets/music_urls.txt
  python tools/music_library.py scan
  python tools/music_library.py list --bpm-min 120 --bpm-max 128
        """,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_dl = sub.add_parser("download", help="從 YouTube 下載音樂")
    p_dl.add_argument("--urls", required=True, help="URL 清單文字檔（每行一個 URL）")
    p_dl.add_argument("--out", default="assets/music", help="輸出目錄（預設 assets/music）")

    p_scan = sub.add_parser("scan", help="掃描目錄中的音樂並偵測 BPM")
    p_scan.add_argument("--dir", default="assets/music", help="音樂目錄（預設 assets/music）")

    p_list = sub.add_parser("list", help="列出符合 BPM 範圍的曲目")
    p_list.add_argument("--dir",     default="assets/music", help="音樂目錄")
    p_list.add_argument("--bpm-min", type=float, default=0,    dest="bpm_min", help="BPM 下限")
    p_list.add_argument("--bpm-max", type=float, default=9999, dest="bpm_max", help="BPM 上限")

    args = parser.parse_args()

    if args.command == "download":
        cmd_download(args)
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "list":
        cmd_list(args)


if __name__ == "__main__":
    main()

"""
從 VOD 批量截取訓練用圖片

使用方式：
  python training/extract_frames.py --video path/to/game.mp4 --out training/data/images/train --every 2

參數：
  --video  : 來源影片路徑
  --out    : 輸出圖片目錄
  --every  : 每幾秒截一張（預設 2 秒）
  --start  : 從第幾秒開始（預設 0）
  --end    : 到第幾秒結束（預設 整部影片）
"""

import argparse
import subprocess
from pathlib import Path


def extract(video: Path, out_dir: Path, every: float, start: float, end: float | None):
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = video.stem

    cmd = [
        "ffmpeg",
        "-ss", str(start),
    ]
    if end:
        cmd += ["-to", str(end)]
    cmd += [
        "-i", str(video),
        "-vf", f"fps=1/{every}",
        "-q:v", "2",         # JPEG 品質（1=最高，2~3 夠用）
        str(out_dir / f"{stem}_%05d.jpg"),
    ]
    print(f"執行：{' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    count = len(list(out_dir.glob(f"{stem}_*.jpg")))
    print(f"截取完成，共 {count} 張圖片 → {out_dir}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", required=True)
    parser.add_argument("--out", default="training/data/images/train")
    parser.add_argument("--every", type=float, default=2.0)
    parser.add_argument("--start", type=float, default=0.0)
    parser.add_argument("--end", type=float, default=None)
    args = parser.parse_args()

    extract(
        video=Path(args.video),
        out_dir=Path(args.out),
        every=args.every,
        start=args.start,
        end=args.end,
    )

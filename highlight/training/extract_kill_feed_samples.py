"""把 kill_feed_times 對應的 ROI 截圖，給人工檢視 / 補負樣本訓練集用。

用法：
  python -m highlight.training.extract_kill_feed_samples <video.mp4> <scene.json> [--count 80] [--out-dir ...]

輸出：ROI 圖（499×864，與訓練模型 static-crop 一致），檔名 t{秒數}_{idx}.png 方便回對 scene.json。
"""

import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path

# Windows cp950 console 不支援 ✓ 等字元，強制 utf-8
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

# ROI 必須跟 modules/kill_feed_detector.py 一致
ROI_X_START = 1421   # X 74% of 1920
ROI_X_END   = 1920
ROI_Y_START = 0
ROI_Y_END   = 864    # Y 80% of 1080
ROI_W = ROI_X_END - ROI_X_START
ROI_H = ROI_Y_END - ROI_Y_START


def sample_timestamps(times: list[float], n: int) -> list[tuple[int, float]]:
    """從 times 均勻抽 n 個（含 idx）。若 times <= n，全部取。"""
    if len(times) <= n:
        return list(enumerate(times))
    stride = len(times) / n
    out: list[tuple[int, float]] = []
    for i in range(n):
        idx = int(round(i * stride))
        if idx >= len(times):
            idx = len(times) - 1
        out.append((idx, times[idx]))
    return out


def extract_frame(
    video: Path,
    t_sec: float,
    out_path: Path,
    crop_roi: bool = True,
) -> bool:
    """用 ffmpeg seek 抽單幀。

    crop_roi=True  → 輸出 499×864 的 kill_feed ROI（給人類檢視用）
    crop_roi=False → 輸出完整 1920×1080 原始幀（給 Roboflow 上傳用，
                     讓 Roboflow 的 static-crop preprocessing 自己切）
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{t_sec:.3f}",
        "-i", str(video),
        "-vframes", "1",
    ]
    if crop_roi:
        cmd += ["-vf", f"crop={ROI_W}:{ROI_H}:{ROI_X_START}:{ROI_Y_START}"]
    cmd += ["-loglevel", "error", str(out_path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0 and out_path.exists()


def main():
    parser = argparse.ArgumentParser(description="抽 kill_feed_times ROI 給人工檢視")
    parser.add_argument("video", help="來源 MP4")
    parser.add_argument("scene_json", help="scene.json")
    parser.add_argument("--count", "-n", type=int, default=80, help="抽幾張（預設 80）")
    parser.add_argument(
        "--out-dir", "-o",
        default="E:/lol-highlights/output/kill_feed_review",
        help="輸出資料夾",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="輸出完整 1920×1080 原始幀（給 Roboflow 上傳，let static-crop 自己切）。"
             "不加此 flag 會輸出 499×864 ROI crop（給人類檢視用）",
    )
    args = parser.parse_args()

    video = Path(args.video)
    scene = Path(args.scene_json)
    if not video.exists():
        print(f"找不到影片：{video}", file=sys.stderr); sys.exit(1)
    if not scene.exists():
        print(f"找不到 scene.json：{scene}", file=sys.stderr); sys.exit(1)

    # 確保 ffmpeg 在 PATH
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from highlight.utils.utils import ensure_ffmpeg_path
    ensure_ffmpeg_path()

    data = json.loads(scene.read_text(encoding="utf-8"))
    kill_times = sorted(data.get("kill_feed_times", []) or [])
    if not kill_times:
        print("scene.json 沒有 kill_feed_times！", file=sys.stderr); sys.exit(1)

    print(f"scene.json 總共 {len(kill_times)} 個 kill_feed_times")
    print(f"均勻抽 {args.count} 個樣本")

    samples = sample_timestamps(kill_times, args.count)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"輸出目錄：{out_dir.absolute()}")
    if args.full:
        print(f"模式：完整 1920×1080 原始幀（給 Roboflow 上傳）")
    else:
        print(f"模式：ROI crop  X[{ROI_X_START}~{ROI_X_END}] Y[{ROI_Y_START}~{ROI_Y_END}] "
              f"({ROI_W}×{ROI_H}px)（給人類檢視）")
    print()

    ok = fail = 0
    for idx, t in samples:
        m, s = divmod(int(t), 60)
        out_name = f"t{int(t):04d}_idx{idx:03d}_{m:02d}m{s:02d}s.png"
        out_path = out_dir / out_name
        if extract_frame(video, t, out_path, crop_roi=not args.full):
            ok += 1
            print(f"  ✓ [{idx:3d}/{len(kill_times)}] @ {t:7.1f}s ({m:02d}:{s:02d}) → {out_name}")
        else:
            fail += 1
            print(f"  ✗ [{idx:3d}/{len(kill_times)}] @ {t:7.1f}s 抽幀失敗")

    print()
    print(f"完成：{ok} 張 / 失敗 {fail} 張")
    print(f"請打開 {out_dir} 檢查每張是否真的是 kill_feed 圖示")


if __name__ == "__main__":
    main()

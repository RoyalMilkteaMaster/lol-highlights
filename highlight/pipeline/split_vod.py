"""把多場 VOD（4~6 小時的整天比賽）按場次分割成單場 MP4。

流程：
  1. 用 YOLO 掃描 bp_ui 找出每場 BP 區間
  2. 找每場的 game_end_screen 確定遊戲結束時間
  3. 用 FFmpeg -c copy 快速裁切（不重新編碼）
  4. 輸出到 --out-dir
     檔名格式（Phase 49-2 起）：
       - 從 broadcasts.recording_path 反查到對應 series → <LEAGUE>_<YYYYMMDD>_g<n>_<A>vs<B>.mp4
       - 反查不到但檔名可解析 league/date → <LEAGUE>_<YYYYMMDD>_g<n>.mp4
       - 都不行 → fallback {原 stem}_g{n}.mp4（既有行為）

使用方式：
  python pipeline/split_vod.py <影片路徑> [--out-dir ...] [--stride 20]
"""

import argparse
import logging
import os
import sys
from pathlib import Path

# 讓 import detectors / core 等找得到（pipeline/ 的上一層就是專案根目錄）
sys.path.insert(0, str(Path(__file__).parent.parent))

# 修復 PyTorch + EasyOCR 的 OpenMP DLL 衝突（libiomp5md.dll 被載入兩次）
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

from highlight.utils.utils import ensure_ffmpeg_path, get_video_duration
from highlight.utils.vod_metadata import (
    extract_metadata,
    generate_split_filename,
    lookup_broadcast_series_by_recording_path,
)
from highlight.detectors.yolo_detector import YOLODetector
from highlight.utils import paths

import subprocess

logger = logging.getLogger(__name__)


def ffmpeg_copy_segment(
    src: Path,
    dst: Path,
    start_sec: float,
    end_sec: float,
) -> bool:
    """用 FFmpeg -c copy 裁切片段，快速不重新編碼。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start_sec:.3f}",
        "-to", f"{end_sec:.3f}",
        "-i", str(src),
        "-c", "copy",
        str(dst),
    ]
    logger.info(f"  FFmpeg: {start_sec:.0f}s ~ {end_sec:.0f}s -> {dst.name}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"  FFmpeg 失敗: {result.stderr[-300:]}")
        return False
    return True


def split_vod(
    video_path: Path,
    out_dir: Path,
    stride_sec: float = 30.0,
    bp_buffer_before: float = 30.0,
    game_end_buffer: float = 60.0,
) -> list[Path]:
    """
    主流程：偵測場次邊界 → 裁切 → 回傳輸出檔案列表。

    參數：
      bp_buffer_before : BP 開始前保留的秒數（抓到開場轉換），預設 30s
      game_end_buffer  : 若 game_end 未偵測到，從 VOD 結尾往前留的秒數
    """
    ensure_ffmpeg_path()
    duration = get_video_duration(video_path)
    if duration <= 0:
        logger.error(f"無法取得影片長度：{video_path}")
        return []

    logger.info(f"影片：{video_path.name}  總長：{duration / 3600:.2f} 小時 ({duration:.0f}s)")

    det = YOLODetector(video_path)
    boundaries = det.get_all_game_boundaries(
        start_sec=0.0,
        end_sec=duration,
        stride_sec=stride_sec,
    )

    if not boundaries:
        logger.warning("未找到任何 BP 區間，無法分割。可能是：\n"
                       "  1. 影片本來就是單場（不需要分割）\n"
                       "  2. YOLO 模型對此影片偵測率不佳（可嘗試 --stride 10）")
        return []

    logger.info(f"\n共偵測到 {len(boundaries)} 場遊戲：")
    for b in boundaries:
        end_str = f"{b['game_end']:.0f}s" if b["game_end"] else "未偵測到"
        logger.info(
            f"  Game {b['game_num']}: BP={b['bp_start']:.0f}~{b['bp_end']:.0f}s  "
            f"遊戲結束={end_str}"
        )

    out_paths: list[Path] = []
    stem = video_path.stem

    # Phase 49-2 Step 1：用 vod_metadata 拿 league/date + broadcast_series 對應
    metadata = extract_metadata(video_path)
    logger.info(
        f"vod_metadata: league={metadata.league} date={metadata.match_date} "
        f"confidence={metadata.confidence} series_id={metadata.series_id}"
    )
    series_mapping = lookup_broadcast_series_by_recording_path(video_path)
    if series_mapping:
        logger.info(
            f"  反查到 broadcast_series：{len(series_mapping)} 場對應，"
            f"用法：g{series_mapping[0][0]}={series_mapping[0][1]}vs{series_mapping[0][2]}"
        )
    else:
        logger.info("  未反查到 broadcast_series（可能是手動 lol_vods VOD）")

    # 修 Z：BP 區間在 yolo_detector 已合併，boundaries 直接是「正確 N 場」，
    # 不需要 is_real_game / real_game_idx 邏輯，逐場切片即可。
    for i, b in enumerate(boundaries):
        game_num   = b["game_num"]
        clip_start = max(0.0, b["bp_start"] - bp_buffer_before)

        # game_end 優先順序：
        #   1. YOLO 偵測到 game_end_screen（最準）
        #   2. 下一場 BP start - 30s（robust，BPs 都偵測到了）
        #   3. 影片尾巴（最後一場專用）
        if b["game_end"] is not None:
            clip_end = b["game_end"]
        elif i + 1 < len(boundaries):
            next_bp_start = boundaries[i + 1]["bp_start"]
            clip_end = max(clip_start + 60.0, next_bp_start - 30.0)
            logger.info(
                f"  Game {game_num}: game_end 未偵測 -> 用下一場 BP start - 30s = {clip_end:.0f}s"
            )
        else:
            clip_end = duration - game_end_buffer
            logger.info(
                f"  Game {game_num}: 最後一場 game_end 未偵測 -> 用 VOD end - buffer = {clip_end:.0f}s"
            )

        if clip_end <= clip_start:
            logger.warning(f"  Game {game_num}: 結束時間異常，跳過")
            continue

        # Phase 49-2：用 broadcast_series 反查 (team_a, team_b)；找不到則用空字串
        team_pair = ("", "")
        if series_mapping:
            if len(series_mapping) == 1:
                # 單一系列賽（如 BO5）：所有局都屬同一組隊伍
                _, ta, tb = series_mapping[0]
                team_pair = (ta, tb)
            else:
                for order, ta, tb in series_mapping:
                    if order == game_num:
                        team_pair = (ta, tb)
                        break

        out_filename = generate_split_filename(
            metadata=metadata,
            game_index=game_num,
            teams=team_pair,
            fallback_stem=stem,
        )
        out_path = out_dir / out_filename
        success = ffmpeg_copy_segment(video_path, out_path, clip_start, clip_end)
        if success:
            out_paths.append(out_path)
            size_mb = out_path.stat().st_size / 1024 / 1024
            logger.info(f"  [OK] Game {game_num} 輸出：{out_path.name} ({size_mb:.0f} MB)")
        else:
            logger.error(f"  [X] Game {game_num} 裁切失敗")

    return out_paths


def main():
    parser = argparse.ArgumentParser(description="將多場 LOL VOD 按場次自動分割")
    parser.add_argument("video", help="影片路徑")
    parser.add_argument(
        "--out-dir",
        default=str(paths.output_dir() / "split"),
        help=f"輸出目錄（預設 {paths.output_dir() / 'split'}）",
    )
    parser.add_argument(
        "--stride",
        type=float,
        default=30.0,
        help="YOLO 掃描間隔秒數（預設 30，越小越準但越慢）",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    video_path = Path(args.video)
    if not video_path.exists():
        logger.error(f"找不到影片：{video_path}")
        sys.exit(1)

    out_dir = Path(args.out_dir)
    results = split_vod(video_path, out_dir, stride_sec=args.stride)

    if results:
        logger.info(f"\n分割完成！共輸出 {len(results)} 個檔案：")
        for p in results:
            logger.info(f"  {p}")
    else:
        logger.warning("未輸出任何檔案。")


if __name__ == "__main__":
    main()

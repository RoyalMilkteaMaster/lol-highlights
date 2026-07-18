"""用 YOLO 偵測右上角 Kill Feed（擊殺廣播）+ Tower。

策略：
  - ROI 裁切：右上角 X:74-100% / Y:0-80%（與 Roboflow static-crop 一致）
  - 採樣率：每 1 秒一幀（Kill Feed 停留 4~5 秒，多次取樣保證命中）
  - 多幀投票：5 秒視窗內任一幀命中 → 算一次擊殺
  - 數學：單幀 90% 命中 → 5 幀全漏 = 0.1^5 = 99.999% recall

Tower 副偵測（class=1）：
  - 用途：遊戲快結束時（基地破壞）會出現大量 tower
  - 副輸出 suspected_game_end：tower 密集出現的最晚時間點 → game_end fallback

獨立執行：
  python -m detectors.kill_feed_detector <video.mp4> --output kills.json
"""

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


# ── 預設參數（Phase 32 規格）──────────────────────────────────────────────────
DEFAULT_ROI_Y = (0, 864)         # Y:0-80% of 1080
DEFAULT_ROI_X = (1421, 1920)     # X:74-100% of 1920
# ↑ 必須與 Roboflow 訓練時的 static-crop 完全一致（74%~100% × 0%~80%）。
#   否則模型看到錯誤 aspect ratio 的 crop，會抓不到 kill_feed。
#   可在 Roboflow 專案的 preprocessing.static-crop 設定確認。
DEFAULT_SAMPLE_INTERVAL = 1.0    # 每秒 1 幀
DEFAULT_MERGE_WINDOW    = 5.0    # 5 秒內合併視為同一擊殺
DEFAULT_KILL_CONF       = 0.55   # Phase 45+：0.4→0.55，搭配 hard negative mining 循環減少誤判
DEFAULT_TOWER_CONF      = 0.3    # tower 信心度門檻（低一點，多抓）

# Tower 密集偵測（用於 game_end fallback）
TOWER_DENSE_WINDOW   = 30.0      # 30 秒視窗
TOWER_DENSE_MIN_HITS = 3         # 視窗內至少 3 次 tower 偵測才算密集
TOWER_MIN_GAME_TIME  = 900.0     # 必須在 game_start + 15 分鐘之後（投降最早時間）


@dataclass
class KillFeedResult:
    kill_feed_times:      list[float]        # 多幀投票後的擊殺時間點
    tower_times:          list[float]        # 原始 tower 偵測時間點（1fps）
    suspected_game_end:   float | None       # tower 密集視窗的最晚時間（None 表示沒抓到）
    sample_count:         int                # 總共處理了幾幀
    elapsed_sec:          float              # 偵測花費秒數


# ─────────────────────────────────────────────────────────────────────────────
# KillFeedDetector
# ─────────────────────────────────────────────────────────────────────────────

class KillFeedDetector:
    """
    YOLO Kill Feed 偵測器。模型須含兩個 class：
      class 0 = kill_feed（右上角擊殺廣播）
      class 1 = tower（球隊塔，輔助 game_end 訊號）
    """

    DEFAULT_MODEL = Path(__file__).parent.parent / "assets" / "yolo_models" / "kill_feed_yolo11s.pt"

    def __init__(
        self,
        model_path:      Path | None = None,
        kill_conf:       float = DEFAULT_KILL_CONF,
        tower_conf:      float = DEFAULT_TOWER_CONF,
        roi_y:           tuple[int, int] = DEFAULT_ROI_Y,
        roi_x:           tuple[int, int] = DEFAULT_ROI_X,
        sample_interval: float = DEFAULT_SAMPLE_INTERVAL,
        merge_window:    float = DEFAULT_MERGE_WINDOW,
        device:          int | str = 0,   # 0 = GPU 0；'cpu' 強制 CPU
    ):
        self.model_path      = Path(model_path) if model_path else self.DEFAULT_MODEL
        self.kill_conf       = kill_conf
        self.tower_conf      = tower_conf
        self.roi_y           = roi_y
        self.roi_x           = roi_x
        self.sample_interval = sample_interval
        self.merge_window    = merge_window
        self.device          = device
        self._model          = None

    # ── 公開 API ─────────────────────────────────────────────────────────────

    def detect(
        self,
        video_path: Path,
        start_sec:  float = 0.0,
        end_sec:    float | None = None,
        seek_workers: int = 8,
        batch_size:   int = 16,
        chunk_size:   int = 200,
    ) -> KillFeedResult:
        """
        掃描整支影片，回傳 KillFeedResult。

        Phase 41：從「cv2 單執行緒 + 單幀推論」改成「FFmpeg 平行 seek + batch 推論」
          - seek_workers 個 ThreadPool 平行 FFmpeg seek+crop（CPU 多核）
          - YOLO batch inference（GPU 批次推論）
          - chunk_size 控制每批次最大幀數（避免 RAM OOM）

        預期加速：原 22 分鐘 → ~3~8 分鐘（視硬碟 IO 而定）
        """
        import subprocess
        from concurrent.futures import ThreadPoolExecutor, as_completed

        self._ensure_model()

        # ── 取得影片資訊（只讀 metadata，不解碼）──────────────────────────────
        import cv2
        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError(f"無法開啟影片：{video_path}")
        fps          = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration     = total_frames / fps if fps > 0 else 0
        width        = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height       = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        cap.release()

        end_sec = end_sec if end_sec is not None else duration
        roi_y, roi_x = self._scaled_roi(width, height)

        # FFmpeg crop filter 參數（以 video 左上為原點）
        # 注意：FFmpeg crop 要求偶數寬高（不然會自動縮減一個 pixel 導致 frame bytes 對不上）
        crop_x1 = roi_x[0]
        crop_y1 = roi_y[0]
        crop_w  = (roi_x[1] - roi_x[0]) & ~1   # 強制偶數（位元 AND ~1 = 去掉末位）
        crop_h  = (roi_y[1] - roi_y[0]) & ~1
        frame_size = crop_w * crop_h * 3

        # 建立取樣時間點
        timestamps: list[float] = []
        t = start_sec
        while t < end_sec:
            timestamps.append(t)
            t += self.sample_interval
        total = len(timestamps)

        logger.info(
            f"[KillFeed] 影片 {width}×{height} @{fps:.1f}fps, "
            f"掃描 {start_sec:.0f}~{end_sec:.0f}s, "
            f"crop={crop_w}×{crop_h}@({crop_x1},{crop_y1}), "
            f"sample={self.sample_interval}s, 總幀數={total} (parallel seek + batch)"
        )

        video_str = str(video_path)

        def seek_one(t: float):
            """FFmpeg seek 到 t，直接 crop 回傳 numpy array（H×W×3 bgr24）。"""
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{t:.3f}",
                "-i", video_str,
                "-frames:v", "1",
                "-vf", f"crop={crop_w}:{crop_h}:{crop_x1}:{crop_y1}",
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "pipe:1",
            ]
            try:
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                raw = proc.stdout
                if len(raw) >= frame_size:
                    return t, np.frombuffer(raw[:frame_size], dtype=np.uint8).reshape(crop_h, crop_w, 3)
            except Exception as e:
                logger.debug(f"[KillFeed] seek 失敗 t={t:.0f}s: {e}")
            return t, None

        min_conf = min(self.kill_conf, self.tower_conf)
        kill_hits:  list[float] = []
        tower_hits: list[float] = []

        def process_batch(frames: list, times: list[float]):
            if not frames:
                return
            results = self._model(
                frames, conf=min_conf, verbose=False,
                device=self.device, imgsz=640,
            )
            for res, rt in zip(results, times):
                has_kill = has_tower = False
                for b in res.boxes:
                    cls  = int(b.cls[0])
                    conf = float(b.conf[0])
                    if cls == 0 and conf >= self.kill_conf:
                        has_kill = True
                    elif cls == 1 and conf >= self.tower_conf:
                        has_tower = True
                    if has_kill and has_tower:
                        break
                if has_kill:
                    kill_hits.append(rt)
                if has_tower:
                    tower_hits.append(rt)

        t0 = time.time()
        done = 0

        # ── 逐 chunk 處理（避免 RAM OOM）─────────────────────────────────────
        for chunk_start in range(0, total, chunk_size):
            chunk_ts = timestamps[chunk_start: chunk_start + chunk_size]

            # Phase 1：平行 seek 提取此 chunk 的幀
            extracted: list = [None] * len(chunk_ts)
            with ThreadPoolExecutor(max_workers=seek_workers) as pool:
                future_map = {pool.submit(seek_one, t): i for i, t in enumerate(chunk_ts)}
                for future in as_completed(future_map):
                    idx = future_map[future]
                    extracted[idx] = future.result()
                    done += 1
                    if done % 80 == 0 or done == total:
                        pct = done * 100 // total
                        logger.info(f"  [KillFeed] seek {pct}%  [{done}/{total}]")

            # Phase 2：批次 YOLO 推論
            batch_frames: list = []
            batch_times:  list[float] = []
            for rt, frame in extracted:
                if frame is None:
                    continue
                batch_frames.append(frame)
                batch_times.append(rt)
                if len(batch_frames) >= batch_size:
                    process_batch(batch_frames, batch_times)
                    batch_frames, batch_times = [], []
            process_batch(batch_frames, batch_times)

        # ── 排序（平行 seek 完後 hits 時間順序不保證）────────────────────────
        kill_hits.sort()
        tower_hits.sort()

        elapsed = time.time() - t0
        logger.info(
            f"[KillFeed] 處理 {total} 幀，耗時 {elapsed:.1f}s "
            f"(raw kill={len(kill_hits)}, raw tower={len(tower_hits)})"
        )

        # 多幀投票合併
        kill_events = self._cluster(kill_hits, self.merge_window)
        suspected_end = self._find_suspected_game_end(tower_hits, start_sec)

        logger.info(
            f"[KillFeed] 投票後：{len(kill_events)} 個擊殺，"
            f"suspected_game_end={suspected_end}"
        )

        return KillFeedResult(
            kill_feed_times=kill_events,
            tower_times=tower_hits,
            suspected_game_end=suspected_end,
            sample_count=total,
            elapsed_sec=elapsed,
        )

    # ── 私有 ──────────────────────────────────────────────────────────────────

    def _ensure_model(self):
        if self._model is not None:
            return
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"找不到 kill_feed 模型：{self.model_path}\n"
                f"  訓練完成後將 best.pt 複製到此路徑。"
            )
        from ultralytics import YOLO
        self._model = YOLO(str(self.model_path))
        logger.info(f"[KillFeed] 模型載入：{self.model_path.name} (device={self.device})")

    def _scaled_roi(self, width: int, height: int) -> tuple[tuple[int, int], tuple[int, int]]:
        """根據實際影片解析度按比例調整 ROI（訓練 baseline = 1920×1080）。"""
        sx = width  / 1920.0
        sy = height / 1080.0
        roi_y = (int(self.roi_y[0] * sy), int(self.roi_y[1] * sy))
        roi_x = (int(self.roi_x[0] * sx), int(self.roi_x[1] * sx))
        return roi_y, roi_x

    def _predict_one(self, crop: np.ndarray) -> tuple[bool, bool]:
        """對單張 crop 跑 YOLO，回傳 (有擊殺?, 有 tower?)。"""
        # 用較低的 conf 跑，再依個別 class threshold 過濾
        min_conf = min(self.kill_conf, self.tower_conf)
        results = self._model.predict(
            crop,
            conf=min_conf,
            verbose=False,
            device=self.device,
            imgsz=640,
        )
        has_kill = has_tower = False
        for r in results:
            for b in r.boxes:
                cls  = int(b.cls[0])
                conf = float(b.conf[0])
                if cls == 0 and conf >= self.kill_conf:
                    has_kill = True
                elif cls == 1 and conf >= self.tower_conf:
                    has_tower = True
                if has_kill and has_tower:
                    return True, True
        return has_kill, has_tower

    @staticmethod
    def _cluster(times: list[float], gap: float) -> list[float]:
        """時間相近的點群聚為一個事件（取群內最早時間）。"""
        if not times:
            return []
        times = sorted(times)
        out = [times[0]]
        for t in times[1:]:
            if t - out[-1] > gap:
                out.append(t)
        return out

    @staticmethod
    def _find_suspected_game_end(
        tower_times:    list[float],
        game_start_sec: float,
        window:         float = TOWER_DENSE_WINDOW,
        min_hits:       int   = TOWER_DENSE_MIN_HITS,
    ) -> float | None:
        """
        找最晚一個「30 秒內 tower ≥ 3 次」的視窗起點時間。
        必須晚於 game_start + 15 分鐘（投降最早時間，避免誤判前期 minion）。
        """
        candidates = sorted(t for t in tower_times if t > game_start_sec + TOWER_MIN_GAME_TIME)
        if len(candidates) < min_hits:
            return None

        latest_dense_start = None
        for i, t in enumerate(candidates):
            count = sum(1 for x in candidates[i:] if x <= t + window)
            if count >= min_hits:
                latest_dense_start = t
        return latest_dense_start


# ─────────────────────────────────────────────────────────────────────────────
# 獨立執行模式
# ─────────────────────────────────────────────────────────────────────────────

def main():
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Kill Feed 偵測（含 tower 輔助）")
    parser.add_argument("video", help="輸入影片路徑")
    parser.add_argument("--model", help="YOLO 模型路徑（預設 assets/yolo_models/kill_feed_yolo11s.pt）")
    parser.add_argument("--start", type=float, default=0.0, help="掃描起點秒數")
    parser.add_argument("--end",   type=float, default=None, help="掃描終點秒數（預設全片）")
    parser.add_argument("--sample-interval", type=float, default=DEFAULT_SAMPLE_INTERVAL)
    parser.add_argument("--merge-window",    type=float, default=DEFAULT_MERGE_WINDOW)
    parser.add_argument("--kill-conf",  type=float, default=DEFAULT_KILL_CONF)
    parser.add_argument("--tower-conf", type=float, default=DEFAULT_TOWER_CONF)
    parser.add_argument("--device", default="0", help="device (0=GPU0, cpu)")
    parser.add_argument("--output", "-o", help="輸出 JSON 路徑（不填則只印 terminal）")
    args = parser.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        print(f"錯誤：找不到影片 {video_path}")
        raise SystemExit(1)

    device = int(args.device) if args.device.isdigit() else args.device

    detector = KillFeedDetector(
        model_path      = Path(args.model) if args.model else None,
        kill_conf       = args.kill_conf,
        tower_conf      = args.tower_conf,
        sample_interval = args.sample_interval,
        merge_window    = args.merge_window,
        device          = device,
    )

    result = detector.detect(video_path, args.start, args.end)

    print(f"\n=== 結果 ===")
    print(f"擊殺事件：{len(result.kill_feed_times)} 個")
    for t in result.kill_feed_times:
        m, s = divmod(t, 60)
        print(f"  {int(m):02d}:{s:05.2f}  ({t:.2f}s)")
    print(f"\nTower 偵測：{len(result.tower_times)} 次")
    print(f"suspected_game_end: {result.suspected_game_end}")
    print(f"處理 {result.sample_count} 幀，{result.elapsed_sec:.1f}s")

    if args.output:
        out = Path(args.output)
        out.write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n已儲存至 {out}")


if __name__ == "__main__":
    main()

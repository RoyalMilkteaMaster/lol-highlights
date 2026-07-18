"""用 YOLO 偵測賽後總表（End Graph）→ 反推 game_end。

策略：
  - 全螢幕推論 imgsz=640（end_graph 是大面積物件，不裁 ROI）
  - 採樣率：每 5 秒一幀（end_graph 出現後停留 30+ 秒，5 秒採樣保證命中）
  - 持續性確認：連續 ≥3 次命中（≥15 秒）才認，避免 replay 短暫總表誤判
  - 找到後立即停止，省 GPU 時間

下游用法（scan_video Step 2a）：
  game_end = min(last_kill_feed + 45, end_graph_first_seen)

獨立執行：
  python -m detectors.end_graph_detector <video.mp4>
"""

import argparse
import json
import logging
import os
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


# ── 預設參數（Phase 34 規格）──────────────────────────────────────────────────
DEFAULT_SAMPLE_INTERVAL  = 5.0     # 每 5 秒一幀（圖表停留 30+ 秒，至少 6 次機會）
DEFAULT_PERSIST_REQUIRED = 3       # 連續 3 次命中（≥15 秒）才確認
DEFAULT_CONF             = 0.5     # end_graph 信心度門檻（高一點避免誤判）
DEFAULT_SCAN_START_PAD   = 600.0   # game_start 之後幾秒才開始掃（10 分鐘前不可能結束）


@dataclass
class EndGraphResult:
    first_seen:        float | None       # 第一次連續確認的時間戳（None = 沒抓到）
    persistent_frames: int                # 確認時連續命中的幀數
    raw_hits:          list[float] = field(default_factory=list)   # 所有命中時間戳（debug）
    sample_count:      int = 0            # 總共處理了幾幀
    elapsed_sec:       float = 0.0        # 偵測花費秒數


# ─────────────────────────────────────────────────────────────────────────────
# EndGraphDetector
# ─────────────────────────────────────────────────────────────────────────────

class EndGraphDetector:
    """
    YOLO End Graph 偵測器。模型只有一個 class：
      class 0 = end_graph（賽後傷害/經濟總表，全螢幕大面積）
    """

    DEFAULT_MODEL = Path(__file__).parent.parent / "assets" / "yolo_models" / "end_graph_yolov8n.pt"

    def __init__(
        self,
        model_path:       Path | None = None,
        conf:             float = DEFAULT_CONF,
        sample_interval:  float = DEFAULT_SAMPLE_INTERVAL,
        persist_required: int   = DEFAULT_PERSIST_REQUIRED,
        device:           int | str = 0,
    ):
        self.model_path       = Path(model_path) if model_path else self.DEFAULT_MODEL
        self.conf             = conf
        self.sample_interval  = sample_interval
        self.persist_required = persist_required
        self.device           = device
        self._model           = None

    # ── 公開 API ─────────────────────────────────────────────────────────────

    def detect(
        self,
        video_path: Path,
        scan_start: float = 0.0,
        scan_end:   float | None = None,
        seek_workers: int = 8,
        batch_size:   int = 16,
    ) -> EndGraphResult:
        """
        從 scan_start 掃到 scan_end，找 end_graph 第一次穩定出現的時間。
        連續 persist_required 幀命中 → first_seen = 首幀時間。

        Phase 41：從 cv2 單執行緒改成 FFmpeg parallel seek + YOLO batch inference
          - 掃 30 分鐘範圍（360 幀）從 cv2 的 ~10 分鐘降到 parallel 的 ~30 秒
          - 改成一次掃完整個範圍（不再 early-exit），平行 seek 讓 early exit 沒意義
        """
        import subprocess
        from concurrent.futures import ThreadPoolExecutor, as_completed

        self._ensure_model()

        # ── 取得影片資訊 ─────────────────────────────────────────────────────
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

        scan_end = scan_end if scan_end is not None else duration

        # end_graph 是全螢幕，直接縮小到 640 即可偵測
        SCALE_W = 640
        w = SCALE_W if SCALE_W < width else width
        h = int(height * w / width / 2) * 2   # 偶數
        frame_size = w * h * 3

        # 建立取樣時間點
        timestamps: list[float] = []
        t = scan_start
        while t < scan_end:
            timestamps.append(t)
            t += self.sample_interval
        total = len(timestamps)

        logger.info(
            f"[EndGraph] 影片 {width}×{height} @{fps:.1f}fps, "
            f"掃描 {scan_start:.0f}~{scan_end:.0f}s, "
            f"sample={self.sample_interval}s, persist={self.persist_required}, "
            f"幀數={total} (parallel seek + batch)"
        )

        video_str = str(video_path)

        def seek_one(t_sec: float):
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{t_sec:.3f}",
                "-i", video_str,
                "-frames:v", "1",
                "-vf", f"scale={w}:{h}",
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "pipe:1",
            ]
            try:
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                raw = proc.stdout
                if len(raw) >= frame_size:
                    return t_sec, np.frombuffer(raw[:frame_size], dtype=np.uint8).reshape(h, w, 3)
            except Exception as e:
                logger.debug(f"[EndGraph] seek 失敗 t={t_sec:.0f}s: {e}")
            return t_sec, None

        # ── Phase 1: parallel seek 提取所有幀 ──────────────────────────────
        t_start = time.time()
        extracted: list = [None] * total
        with ThreadPoolExecutor(max_workers=seek_workers) as pool:
            future_map = {pool.submit(seek_one, ts): i for i, ts in enumerate(timestamps)}
            done = 0
            for future in as_completed(future_map):
                idx = future_map[future]
                extracted[idx] = future.result()
                done += 1
                if done % 60 == 0 or done == total:
                    pct = done * 100 // total
                    logger.info(f"  [EndGraph] seek {pct}%  [{done}/{total}]")

        # ── Phase 2: batch inference（保持時間順序）─────────────────────────
        raw_hits:   list[float]        = []
        hit_map:    dict[float, bool]  = {}
        batch_frames, batch_times = [], []

        def process_batch(frames, times):
            if not frames:
                return
            results = self._model(
                frames, conf=self.conf, verbose=False,
                device=self.device, imgsz=640,
            )
            for res, rt in zip(results, times):
                hit = any(int(b.cls[0]) == 0 and float(b.conf[0]) >= self.conf for b in res.boxes)
                hit_map[rt] = hit
                if hit:
                    raw_hits.append(rt)

        for rt, frame in extracted:
            if frame is None:
                hit_map[rt] = False
                continue
            batch_frames.append(frame)
            batch_times.append(rt)
            if len(batch_frames) >= batch_size:
                process_batch(batch_frames, batch_times)
                batch_frames, batch_times = [], []
        process_batch(batch_frames, batch_times)

        # ── Phase 3: 按時間順序找連續 persist_required 幀命中 ───────────────
        consecutive    = 0
        consecutive_t0 = None
        first_seen     = None
        for ts in timestamps:
            if hit_map.get(ts, False):
                if consecutive == 0:
                    consecutive_t0 = ts
                consecutive += 1
                if consecutive >= self.persist_required:
                    first_seen = consecutive_t0
                    break
            else:
                consecutive = 0
                consecutive_t0 = None

        elapsed = time.time() - t_start
        if first_seen is not None:
            logger.info(
                f"[EndGraph] 確認！first_seen={first_seen:.1f}s "
                f"(連續 {consecutive} 幀) ({elapsed:.1f}s)"
            )
        else:
            logger.info(
                f"[EndGraph] 未抓到（連續命中數不足 {self.persist_required}），"
                f"raw_hits={len(raw_hits)}, sample={total} ({elapsed:.1f}s)"
            )

        return EndGraphResult(
            first_seen=first_seen,
            persistent_frames=consecutive if first_seen is not None else 0,
            raw_hits=sorted(raw_hits),
            sample_count=total,
            elapsed_sec=elapsed,
        )

    # ── 私有 ──────────────────────────────────────────────────────────────────

    def _ensure_model(self):
        if self._model is not None:
            return
        if not self.model_path.exists():
            raise FileNotFoundError(
                f"找不到 end_graph 模型：{self.model_path}\n"
                f"  訓練完成後將 best.pt 複製到此路徑。"
            )
        from ultralytics import YOLO
        self._model = YOLO(str(self.model_path))
        logger.info(f"[EndGraph] 模型載入：{self.model_path.name} (device={self.device})")

    def _predict_one(self, frame: np.ndarray) -> bool:
        """對全螢幕單張 frame 跑 YOLO，回傳是否偵測到 end_graph。"""
        results = self._model.predict(
            frame,
            conf=self.conf,
            verbose=False,
            device=self.device,
            imgsz=640,
        )
        for r in results:
            if len(r.boxes) > 0:
                return True
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 獨立執行模式
# ─────────────────────────────────────────────────────────────────────────────

def main():
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="End Graph 偵測（賽後總表）")
    parser.add_argument("video", help="輸入影片路徑")
    parser.add_argument("--model", help="YOLO 模型路徑（預設 assets/yolo_models/end_graph_yolov8n.pt）")
    parser.add_argument("--start", type=float, default=0.0, help="掃描起點秒數")
    parser.add_argument("--end",   type=float, default=None, help="掃描終點秒數（預設全片）")
    parser.add_argument("--sample-interval",  type=float, default=DEFAULT_SAMPLE_INTERVAL)
    parser.add_argument("--persist-required", type=int,   default=DEFAULT_PERSIST_REQUIRED)
    parser.add_argument("--conf",             type=float, default=DEFAULT_CONF)
    parser.add_argument("--device", default="0", help="device (0=GPU0, cpu)")
    parser.add_argument("--output", "-o", help="輸出 JSON 路徑（不填則只印 terminal）")
    args = parser.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        print(f"錯誤：找不到影片 {video_path}")
        raise SystemExit(1)

    device = int(args.device) if args.device.isdigit() else args.device

    detector = EndGraphDetector(
        model_path       = Path(args.model) if args.model else None,
        conf             = args.conf,
        sample_interval  = args.sample_interval,
        persist_required = args.persist_required,
        device           = device,
    )

    result = detector.detect(video_path, args.start, args.end)

    print(f"\n=== 結果 ===")
    if result.first_seen is not None:
        m, s = divmod(result.first_seen, 60)
        print(f"first_seen: {int(m):02d}:{s:05.2f}  ({result.first_seen:.2f}s)")
        print(f"連續確認幀數: {result.persistent_frames}")
    else:
        print(f"first_seen: 未抓到")
    print(f"原始命中: {len(result.raw_hits)} 次")
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

"""
YOLO Detector — YOLOv8 視覺偵測模組

架構：
  YOLOv8 (ultralytics)：偵測各種遊戲 UI 元素
  FFmpeg pipe          ：高速幀提取（比 OpenCV seek 快 5~10x，不依賴 cv2）

YOLO 偵測 Classes（v5 模型，11 類，詳見 training/）：
  baron_hp_bar        - 巴隆血量條 UI（畫面左側）
  blue_champ_hp_bar   - 藍方英雄血條；消失 = 英雄倒地
  bp_ui               - BP 選角介面；消失點 = bp_end_time；往前推 75s = bp_start_time
  dragon_hp_bar       - 小龍 / 遠古巨龍血量條 UI（合併兩種龍）
  game_end_screen     - 比賽結束全屏畫面（原 victory_screen / defeat_screen 合併）
  herald_banner       - 諭示者全域通知橫幅（目前 pipeline 未消費）
  herald_hp_bar       - 先知先驅血量條 UI
  nexus_explosion     - 主堡爆炸光效（比賽結束前的最早信號）
  red_champ_hp_bar    - 紅方英雄血條；消失 = 英雄倒地
  replay              - REPLAY 浮水印 / 邊框
  voidgrub_hp_bar     - 巢蟲血量條 UI

擊殺偵測（不在本模組）：
  由 detectors/kill_feed_detector.py 處理（右上角 kill_feed 圖示 YOLO 偵測）。

閃現偵測（本模組 get_flash_times，OpenCV 模板比對 + 亮度追蹤，不需 YOLO）：
  Phase 1：用 flash_icon.png 模板比對定位 10 名選手的閃現圖示座標
  Phase 2：全場每 0.5s 取樣那些座標的亮度，驟降 → 閃現被使用
  模板路徑：assets/templates/flash_icon.png

模型路徑：assets/yolo_models/lol_detector.pt（優先 .onnx，fallback .pt）
  若不存在，YOLO 方法回傳空列表，不中斷流程。
"""

import json
import logging
import os
import subprocess
from pathlib import Path


os.environ.setdefault("FLAGS_use_mkldnn", "0")

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

YOLO_CLASSES = [
    "baron_hp_bar",       # 0
    "blue_champ_hp_bar",  # 1  （新增，藍方選手血條 UI）
    "bp_ui",              # 2
    "dragon_hp_bar",      # 3  （含 elder_dragon_hp_bar，合併）
    "game_end_screen",    # 4  （原 victory_screen，含 defeat_screen）
    "herald_banner",      # 5
    "herald_hp_bar",      # 6
    "nexus_explosion",    # 7
    "red_champ_hp_bar",   # 8  （新增，紅方選手血條 UI）
    "replay",             # 9
    "voidgrub_hp_bar",    # 10
]
# nc = 11  （v5 模型：移除 player_of_the_game / post_game_stats / proview / teamfight_recap，新增藍紅方英雄血條）


def cluster_timestamps(times: list[float], gap_sec: float = 3.0) -> list[float]:
    """
    將時間上相鄰（間距 < gap_sec）的時間戳群聚，回傳每群的第一個時間點。
    例：Baron 計時器連續出現數分鐘，gap_sec=60 防止同一個算多次。
    """
    if not times:
        return []
    result = [sorted(times)[0]]
    for t in sorted(times)[1:]:
        if t - result[-1] > gap_sec:
            result.append(t)
    return result


def _bgr_to_gray(frame: np.ndarray) -> np.ndarray:
    """BGR numpy array → 灰階，不依賴 cv2。"""
    return (
        0.114 * frame[:, :, 0]
        + 0.587 * frame[:, :, 1]
        + 0.299 * frame[:, :, 2]
    ).astype(np.uint8)


class YOLODetector:
    """
    YOLOv8 視覺偵測器。

    目前實際使用的方法：
        det = YOLODetector(source_vod)
        # 多類別平行偵測（scan_video.py 主力）
        hits = det._detect_multi_classes(
            ["replay", "game_end_screen", "baron_hp_bar", ...],
            start, end, stride_sec=1.0, chunk_size=200,
        )
        # hp_bar 範圍式細掃（scan_video.py Step 7 hp_disappear 偵測）
        frame_counts = det._detect_multi_classes_in_ranges(
            ["blue_champ_hp_bar", "red_champ_hp_bar"],
            time_ranges, stride_sec=0.8,
        )
        # BP 精確定位（scan_video.py Step 1）
        bp_start, bp_end, interval = det.get_bp_times_by_index(start, end, target_game_num=1)
        # 閃現偵測（scan_video.py Step 6）
        flash_times = det.get_flash_times(start, end)
        # 場次邊界（split_vod.py 使用）
        boundaries = det.get_all_game_boundaries(0, duration)
    """

    DEFAULT_MODEL = Path(__file__).parent.parent / "assets/yolo_models/lol_detector.pt"

    @staticmethod
    def _resolve_model_path(pt_path: Path) -> Path:
        """優先使用 ONNX Runtime（同目錄 .onnx），不存在時 fallback 到 PyTorch .pt。"""
        onnx_path = pt_path.with_suffix(".onnx")
        if onnx_path.exists():
            logger.info(f"[YOLODetector] 使用 ONNX Runtime: {onnx_path.name}")
            return onnx_path
        logger.info(f"[YOLODetector] 使用 PyTorch: {pt_path.name}")
        return pt_path

    def __init__(
        self,
        video_path: Path,
        model_path: Path | None = None,
        conf_threshold: float = 0.5,
        device: int | str | None = None,   # Phase 49-3b 雙顯卡：0 / 1 / 'cpu'
    ):
        self.video_path = Path(video_path)
        _base = Path(model_path) if model_path else self.DEFAULT_MODEL
        self.model_path = self._resolve_model_path(_base)
        self.conf = conf_threshold
        self.device = device   # None = 讓 ultralytics 自行決定（單卡：cuda:0）
        self._model = None
        self._width, self._height, self._fps = self._probe_video()
        self._hwaccel = self._detect_hwaccel()
        self._load_model()

    # ── 模型載入 ─────────────────────────────────────────────────────────────

    def _load_model(self):
        if not self.model_path.exists():
            logger.warning(
                f"[YOLODetector] 模型尚未訓練：{self.model_path}\n"
                "  YOLO 偵測功能停用（BP/replay/objective/hp_bar 等全部失效）。\n"
                "  訓練完成後，將 best.pt 複製到上方路徑即可啟用。"
            )
            return
        try:
            from ultralytics import YOLO
            self._model = YOLO(str(self.model_path))
            logger.info(f"[YOLODetector] 模型載入成功: {self.model_path}")
        except ImportError:
            logger.warning("ultralytics 未安裝，請: pip install ultralytics")
        except Exception as e:
            logger.warning(f"[YOLODetector] 模型載入失敗: {e}")

    def get_bp_times_by_index(
        self,
        start_sec: float,
        end_sec: float,
        target_game_num: int = 1,
        min_duration: float = 80.0,
        disappear_sec: float = 20.0,
        stride_sec: float = 1.0,
    ) -> tuple[float | None, float | None, dict | None]:
        """
        透過 bp_ui 消失點定位 BP 的結束時間，支援 BO3/BO5 多場次索引。

        邏輯：
          1. 掃描全片，找出所有 bp_ui 連續出現區間
          2. 過濾掉持續時間 < min_duration 的殘缺片段（暫停 / remake）
          3. 按 target_game_num 對號入座（game 1 → index 0）
          4. 回傳 (bp_end - 75s, bp_end)

        參數：
          target_game_num : 第幾場（1-based），BO3 第二場填 2
          min_duration    : 有效 BP 最短持續秒數
                            5/16 120s → 80s：LCK live_split 切點 lead_in_sec=30s 不夠涵蓋
                            完整 BP，game_path 只含 BP 末段 90 秒（最後選擇 + 隊伍進場儀式）。
                            120s 過嚴造成 LCK BFX/HLE/GEN g1 全 FATAL_NO_BP。
                            80s 仍能過濾「賽前節目誤判」短 cluster（通常 < 60s）。
                            offline split_vod (get_all_game_boundaries) 保留 120s 防誤切。
          disappear_sec   : bp_ui 消失幾秒才算真正結束
                            5/15 8s → 20s：LPL 轉播在 BP 階段常切走鏡頭拍選手 / 教練席 10-15s，
                            8s 太嚴會把連續 BP 切成多個 < 120s 小片段 → 全 filter → FATAL_NO_BP
                            （clip_job 21/22/32/37/38/40/53/54/61/64 都因此失敗）
        """
        if self._model is None:
            logger.warning("[get_bp_times_by_index] 模型未載入，無法偵測 bp_ui")
            return None, None, None

        # 取得所有 bp_ui 偵測時間戳（平行 seek，取代舊序列式 pipe）
        # 5/16：bp_ui 用 conf≥0.55 砍掉 LPL false positive（隊伍進場 / 舞台空鏡頭）
        hits = self._detect_multi_classes(
            ["bp_ui"], start_sec, end_sec,
            stride_sec=stride_sec,
            chunk_size=200,
            conf_overrides={"bp_ui": 0.55},
        )
        raw_hits = set(hits.get("bp_ui", []))

        # 建立每個時間點的 present/absent 序列
        all_times = [start_sec + i * stride_sec
                     for i in range(int((end_sec - start_sec) / stride_sec) + 1)]

        # Phase 41：追蹤每個區間「最後一個 hit」的真實時間（使用者要求 bp_end = bp_ui 最後 hit）
        bp_intervals: list[tuple[float, float, float]] = []   # (start, absent_since, last_hit)
        current_start: float | None = None
        last_hit_in_current: float | None = None
        absent_since: float | None = None

        for t in all_times:
            present = t in raw_hits
            if present:
                if current_start is None:
                    current_start = t
                last_hit_in_current = t
                absent_since = None          # 重置消失計時
            else:
                if current_start is not None:
                    if absent_since is None:
                        absent_since = t
                    elif t - absent_since >= disappear_sec:
                        # 確認消失夠久，結束此區間
                        duration = absent_since - current_start
                        if duration >= min_duration:
                            bp_intervals.append((current_start, absent_since, last_hit_in_current or current_start))
                            logger.info(
                                f"  [bp_ui] 有效 BP 區間 #{len(bp_intervals)}: "
                                f"{current_start:.0f}~{absent_since:.0f}s "
                                f"({duration:.0f}s)，last_hit={last_hit_in_current:.0f}s"
                            )
                        else:
                            logger.debug(
                                f"  [bp_ui] 過濾殘缺 BP: {current_start:.0f}s ~ "
                                f"{absent_since:.0f}s ({duration:.0f}s < {min_duration}s)"
                            )
                        current_start = None
                        last_hit_in_current = None
                        absent_since = None

        # 影片結尾仍在 BP 的收尾處理
        if current_start is not None:
            last_time = all_times[-1]
            duration = last_time - current_start
            if duration >= min_duration:
                bp_intervals.append((current_start, last_time, last_hit_in_current or current_start))
                logger.info(
                    f"  [bp_ui] 有效 BP 區間 #{len(bp_intervals)} (影片結尾): "
                    f"{current_start:.0f}~{last_time:.0f}s ({duration:.0f}s)，"
                    f"last_hit={last_hit_in_current:.0f}s"
                )

        logger.info(
            f"[get_bp_times_by_index] 共找到 {len(bp_intervals)} 個有效 BP，"
            f"目標場次: game {target_game_num}"
        )

        if len(bp_intervals) >= target_game_num:
            bp_start_raw, absent_since_raw, last_hit_raw = bp_intervals[target_game_num - 1]
            # Phase 41：bp_end = bp_ui 最後 hit 的時間（不再延伸 disappear_sec=8s buffer）
            bp_end = float(last_hit_raw)
            final_start = max(start_sec, bp_end - 75.0)
            logger.info(
                f"  → game {target_game_num}: bp_end (last_hit)={bp_end:.0f}s, "
                f"clip_start={final_start:.0f}s, "
                f"bp_ui_interval={bp_start_raw:.0f}~{bp_end:.0f}s "
                f"(absent_since={absent_since_raw:.0f}s)"
            )
            return final_start, bp_end, {
                "start":         float(bp_start_raw),
                "end":           bp_end,                      # 給 bp_analyzer 當硬 clamp 上限用
                "last_hit":      bp_end,                      # 同 end（Phase 41 後一致）
                "absent_since":  float(absent_since_raw),     # 留個記錄方便 debug
            }
        else:
            logger.warning(
                f"[get_bp_times_by_index] 找不到第 {target_game_num} 場 BP "
                f"（只找到 {len(bp_intervals)} 個）"
            )
            return None, None, None

    def get_all_game_boundaries(
        self,
        start_sec: float,
        end_sec: float,
        stride_sec: float = 30.0,
        min_bp_duration: float = 120.0,
        min_game_duration: float = 900.0,
        merge_gap_sec: float = 600.0,
    ) -> list[dict]:
        """
        掃描多場 VOD（BO3/BO5），自動找出每場遊戲的 BP 起訖與遊戲結束時間。

        回傳範例：
          [
            {"game_num": 1, "bp_start": 300.0, "bp_end": 600.0, "game_end": 4200.0},
            ...
          ]

        說明：
          - bp_start / bp_end：BP 畫面出現的起迄秒數
          - game_end：偵測到 game_end_screen 的時間（加 30s buffer）
                      若該場未偵測到結束畫面，game_end = None
          - stride_sec=30 對 4 小時 VOD 約 480 幀，速度 ~2 分鐘

        修 Z（取代修 W/W2 的 is_real_game filter 策略）：
          - LCP 完整轉播 BP 階段穿插「拍選手鏡頭」會讓 BP_UI 消失 ≥ disappear_sec(60s)
            導致同場 BP 被切成兩段（介紹頁 + 真 BP）。
          - 解法：BP 區間聚合後加合併 pass，相鄰 gap < merge_gap_sec(600s = 10 min)
            視為同場 BP，合併成一個區間。
          - 真實場間距離 ≥ 25 min（一場 25~50 min + 賽間 5~10 min），10 分鐘 cap 安全。
          - 合併後不需要 is_real_game 標記（短假 BP 都被合併進真場）。
          - min_bp_duration 改回 120s（合併邏輯接住短假 BP，不需要 180s 從源頭過濾）。
        """
        if self._model is None:
            logger.warning("[get_all_game_boundaries] 模型未載入")
            return []

        # ── 單次掃描：bp_ui + game_end_screen 一起偵測 ────────────────────────
        logger.info(
            f"[get_all_game_boundaries] 單次掃描全部類別 "
            f"({start_sec:.0f}~{end_sec:.0f}s, stride={stride_sec}s)"
        )
        # 5/16：bp_ui 用 conf≥0.55 砍 LPL false positive。game_end_screen 保持原 0.5
        all_hits = self._detect_multi_classes(
            ["bp_ui", "game_end_screen"],
            start_sec, end_sec, stride_sec,
            conf_overrides={"bp_ui": 0.55},
        )
        bp_times  = set(all_hits["bp_ui"])
        end_times = set(all_hits["game_end_screen"])

        all_times = [
            start_sec + i * stride_sec
            for i in range(int((end_sec - start_sec) / stride_sec) + 1)
        ]

        # ── BP 區間聚合（邏輯不變） ───────────────────────────────────────────
        bp_intervals: list = []
        current_start: float | None = None
        absent_since: float | None = None
        disappear_sec = stride_sec * 2

        for t in all_times:
            present = t in bp_times
            if present:
                if current_start is None:
                    current_start = t
                absent_since = None
            else:
                if current_start is not None:
                    if absent_since is None:
                        absent_since = t
                    elif t - absent_since >= disappear_sec:
                        duration = absent_since - current_start
                        if duration >= min_bp_duration:
                            bp_intervals.append((current_start, absent_since))
                            logger.info(
                                f"  [bp_ui] BP #{len(bp_intervals)}: "
                                f"{current_start:.0f}s ~ {absent_since:.0f}s ({duration:.0f}s)"
                            )
                        current_start = None
                        absent_since = None

        if current_start is not None:
            last_time = all_times[-1]
            if last_time - current_start >= min_bp_duration:
                bp_intervals.append((current_start, last_time))

        logger.info(f"[get_all_game_boundaries] 聚合得 {len(bp_intervals)} 個 BP 原始區間")

        # ── 修 Z：相鄰 BP 區間 gap < merge_gap_sec 合併（同場被拍人鏡頭切斷的修正）
        # 真實場間隔 ≥ 25 min，同場 BP 內被切斷的 gap < 10 min，用 600s 為閾值安全。
        if bp_intervals:
            merged = [bp_intervals[0]]
            for s, e in bp_intervals[1:]:
                prev_s, prev_e = merged[-1]
                if s - prev_e < merge_gap_sec:
                    merged[-1] = (prev_s, e)
                    logger.info(
                        f"  [BP 合併] ({prev_s:.0f}~{prev_e:.0f}) + ({s:.0f}~{e:.0f}) "
                        f"gap={s - prev_e:.0f}s < {merge_gap_sec:.0f}s → ({prev_s:.0f}~{e:.0f})"
                    )
                else:
                    merged.append((s, e))
            bp_intervals = merged

        logger.info(f"[get_all_game_boundaries] 合併後 {len(bp_intervals)} 場 BP")

        # ── 從已掃描的 end_times 中篩選每場的遊戲結束時間（不再重新掃描）────
        results = []
        for i, (bp_start, bp_end) in enumerate(bp_intervals):
            game_num = i + 1
            search_start = bp_end + min_game_duration
            search_end = (
                bp_intervals[i + 1][0] - 60.0
                if i + 1 < len(bp_intervals)
                else end_sec
            )

            game_end = None
            relevant_ends = [t for t in end_times if search_start <= t <= search_end]
            if relevant_ends:
                first_end = min(relevant_ends)
                game_end = first_end + 30.0
                logger.info(f"  [game {game_num}] 結束時間: {game_end:.0f}s")
            else:
                logger.warning(f"  [game {game_num}] 未找到結束畫面，game_end=None")

            results.append({
                "game_num": game_num,
                "bp_start": bp_start,
                "bp_end": bp_end,
                "game_end": game_end,
            })

        # 修 Z：is_real_game / effective_end_estimated 標記邏輯廢棄（合併後不再有短假場）
        return results

    def get_flash_times(
        self,
        game_start_sec: float,
        game_end_sec: float,
        template_path: str | None = None,
        init_window_sec: float = 120.0,
        stride_sec: float = 0.5,
        brightness_drop: float = 0.45,
        max_simultaneous_dark: int = 3,
    ) -> list[float]:
        """
        閃現使用偵測：OpenCV 多尺度模板比對（Phase 1）+ 亮度追蹤（Phase 2）。
        不需要 YOLO 模型，不需要訓練資料。

        Phase 1：遊戲開始後前 init_window_sec 秒，用 flash_icon.png 在 0.6~1.1x 尺度範圍
                 做多尺度模板比對，定位所有 10 名選手的閃現圖示座標（適應 Proview 縮小等變化）。
        Phase 2：全場每 stride_sec 秒取樣那些座標的亮度（使用對應的尺度窗口）。
                 亮度驟降 >= brightness_drop（45%）→ 閃現被使用。
                 同時暗掉的格數 > max_simultaneous_dark → 導播切畫面，忽略。

        參數：
          game_start_sec       : 遊戲開始的時間點（秒）
          game_end_sec         : 遊戲結束的時間點（秒）
          template_path        : flash_icon.png 路徑（預設 assets/templates/flash_icon.png）
          init_window_sec      : Phase 1 掃描的時間範圍（預設前 60 秒）
          stride_sec           : Phase 2 取樣間隔（預設 0.5 秒）
          brightness_drop      : 亮度下降比例門檻（預設 0.45 = 下降 45%）
          max_simultaneous_dark: 同時變暗格數上限，超過視為畫面切換（預設 3）
        """
        try:
            import cv2
        except ImportError:
            logger.warning("[get_flash_times] opencv-python 未安裝，跳過閃現偵測。請: pip install opencv-python")
            return []

        # ── 載入模板 ──────────────────────────────────────────────────────────
        tpl_path = template_path or str(
            Path(__file__).parent.parent / "assets" / "templates" / "flash_icon.png"
        )
        tpl = cv2.imread(tpl_path, cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            logger.warning(f"[get_flash_times] 找不到閃現模板：{tpl_path}")
            return []
        # 某些 PNG 讀進來仍是 3D，強制取第一個 channel
        if tpl.ndim == 3:
            tpl = tpl[:, :, 0]
        tpl_h, tpl_w = tpl.shape

        # ── Phase 1：多尺度模板比對定位閃現圖示座標 ──────────────────────────────
        SCALES = [0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10]
        flash_coords: list[tuple[int, int]] = []  # [(cx, cy), ...]
        best_scale = 1.0
        init_end = min(game_start_sec + init_window_sec, game_end_sec)

        logger.info(f"[get_flash_times] Phase 1: 掃描 {game_start_sec:.0f}~{init_end:.0f}s，尺度範圍 0.6~1.1x")

        for _t, frame in self._iter_frames(game_start_sec, init_end, stride_sec=2.0):
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            threshold = 0.65

            # 多尺度試試：對每個尺度做匹配，找最多候選點的那個
            for scale in SCALES:
                stpl = cv2.resize(tpl, (max(1, int(tpl_w * scale)), max(1, int(tpl_h * scale))))
                res = cv2.matchTemplate(gray, stpl, cv2.TM_CCOEFF_NORMED)
                locs = np.where(res >= threshold)
                if len(locs[0]) == 0:
                    continue

                stpl_h, stpl_w = stpl.shape
                candidates: list[tuple[int, int]] = []
                for y, x in zip(locs[0], locs[1]):
                    cx, cy = x + stpl_w // 2, y + stpl_h // 2
                    if all(abs(cx - ex) > 20 or abs(cy - ey) > 20 for ex, ey in candidates):
                        candidates.append((cx, cy))

                if len(candidates) > len(flash_coords):
                    flash_coords = candidates
                    best_scale = scale

            if len(flash_coords) >= 10:
                break

        if not flash_coords:
            logger.warning("[get_flash_times] Phase 1 未找到任何閃現圖示（所有尺度都試過），跳過閃現偵測")
            return []

        logger.info(f"[get_flash_times] Phase 1 完成：{len(flash_coords)} 個座標，scale={best_scale:.2f}x")

        # ── Phase 2：亮度狀態機（edge-triggered）─────────────────────────────────
        # 狀態定義：每個 flash_coord 維護 prev_state ∈ {"bright", "dim"}
        #   bright → dim 的瞬間才記 hit（閃現使用）
        #   dim 狀態持續時不再重複記（避免冷卻 300s 期間被切畫面反覆計數）
        #   current 回升至 base*(1-brightness_drop*0.5) 以上 → 視為冷卻結束，狀態改回 bright
        STATE_BRIGHT = "bright"
        STATE_DIM    = "dim"
        # dim 判定門檻：current < base * dim_ratio
        dim_ratio    = 1.0 - brightness_drop            # e.g. 0.55
        # 回升門檻：放寬，避免狀態在臨界值抖動
        recover_ratio = 1.0 - brightness_drop * 0.5     # e.g. 0.775

        scaled_tpl_w = max(1, int(tpl_w * best_scale))
        scaled_tpl_h = max(1, int(tpl_h * best_scale))
        half_w, half_h = scaled_tpl_w // 2, scaled_tpl_h // 2

        base_brightness: list[float] = [0.0] * len(flash_coords)
        prev_states: list[str] = [STATE_BRIGHT] * len(flash_coords)
        initialized = False
        hits: list[float] = []

        # 修 AF：base_brightness 用「多幀中位數」初始化
        #   之前用「第一個 all > 30 的單幀」太脆弱：game 剛開頭 HUD splash 動畫
        #   會把 base 推高（如 BLG vs JDG 721s base=208~229，穩定態 cur=113~119
        #   → 穩定態被誤判 dim → dark_now=5 永遠 SKIP 整幀 → 永遠不記 hit）。
        #   改為收集前 INIT_SAMPLES 幀 ROI 中位數，避開單幀污染。
        INIT_SAMPLES = 30   # 30 * 0.5s = 15s 內取 30 個 sample
        init_samples_buf: list[list[float]] = []

        frame_idx = 0
        total = max(1, int((game_end_sec - game_start_sec) / stride_sec))

        for t, frame in self._iter_frames(game_start_sec, game_end_sec, stride_sec):
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            sx = frame.shape[1] / self._width
            sy = frame.shape[0] / self._height

            current: list[float] = []
            for cx, cy in flash_coords:
                x1 = max(0, int(cx * sx) - half_w)
                y1 = max(0, int(cy * sy) - half_h)
                x2 = min(gray.shape[1], int(cx * sx) + half_w)
                y2 = min(gray.shape[0], int(cy * sy) + half_h)
                roi = gray[y1:y2, x1:x2]
                current.append(float(np.mean(roi)) if roi.size > 0 else 0.0)

            # 基準亮度初始化：收集 INIT_SAMPLES 個 sample，取每 coord 中位數
            # （修 AF：取代「第一個 all > 30 的單幀」，避開 game 開頭 HUD splash 動畫污染）
            if not initialized:
                if all(b > 30 for b in current):
                    init_samples_buf.append(current[:])
                if len(init_samples_buf) >= INIT_SAMPLES:
                    arr = np.array(init_samples_buf)            # shape: (N, n_coords)
                    base_brightness = np.median(arr, axis=0).tolist()
                    initialized = True
                    base_str = " ".join(f"{int(b):3d}" for b in base_brightness)
                    logger.info(
                        f"[flash] base_brightness init @t={t:.1f}s "
                        f"({len(init_samples_buf)} 幀中位數) base=[{base_str}]"
                    )
                frame_idx += 1
                continue

            # 先判斷此幀是否為畫面切換（大多數格同時變暗）→ 跳過狀態更新
            dark_now = sum(
                1 for b, c in zip(base_brightness, current)
                if b > 30 and c < b * dim_ratio
            )
            if dark_now > max_simultaneous_dark:
                logger.debug(f"  [flash] {dark_now} 格同時暗 @{t:.1f}s → 畫面切換，跳過")
                frame_idx += 1
                continue

            # 邊緣觸發：對每個座標檢查 bright → dim 轉變
            new_hits_this_frame = 0
            for i, (base, cur) in enumerate(zip(base_brightness, current)):
                if base <= 30:
                    continue
                is_dim     = cur < base * dim_ratio
                is_bright  = cur >= base * recover_ratio

                if prev_states[i] == STATE_BRIGHT and is_dim:
                    prev_states[i] = STATE_DIM
                    new_hits_this_frame += 1
                elif prev_states[i] == STATE_DIM and is_bright:
                    prev_states[i] = STATE_BRIGHT   # 冷卻結束，可再次偵測

            if new_hits_this_frame > 0:
                hits.append(t)
                logger.debug(
                    f"  [flash] {new_hits_this_frame} 格 bright→dim @{t:.1f}s → 閃現使用"
                )

            # 緩慢更新基準亮度（僅對 bright 狀態的座標）
            for i in range(len(base_brightness)):
                if prev_states[i] == STATE_BRIGHT and current[i] > base_brightness[i]:
                    base_brightness[i] = base_brightness[i] * 0.95 + current[i] * 0.05

            frame_idx += 1
            if frame_idx % 120 == 0:
                logger.info(f"  [flash] Phase 2: {frame_idx * 100 // total}%  ({t:.0f}s)")

        # 同一瞬間多人閃現會落在同一 t → cluster 合併近 1s 的點以去重
        result = cluster_timestamps(hits, gap_sec=1.0)
        logger.info(f"[get_flash_times] 偵測到 {len(result)} 次閃現使用")
        return result

    def get_game_start_anchor(
        self,
        search_start_sec: float = 0.0,
        max_search_sec: float = 180.0,
        min_icons: int = 8,
        stride_sec: float = 1.0,
        template_path: str | None = None,
    ) -> float | None:
        """flash icon anchor 偵測（Phase 49-3e v3，5/17 user 5/17 晚的洞察）：

        從 search_start_sec 開始掃，找『第一個有 ≥ min_icons 個 flash icon 出現』的幀。
        該幀 ≈ in-game t=0+α（HUD 載入瞬間），可當 timeline anchor 對齊用。

        對 game.mp4 跑：anchor ≈ in-game t=0 (±5-10s)，比 BP_UI 偵測 (±60s) 準 6-10 倍。
        對 broadcast.mp4 跑：anchor 是廣播內第一場 game start 那一瞬間。

        Args:
            search_start_sec: 從幾秒開始掃
            max_search_sec:   掃多久（預設 180s 應該足以涵蓋 BP + 載入畫面）
            min_icons:        至少要找到幾個 flash icons 才算 anchor（預設 8/10，容忍 2 個漏抓）
            stride_sec:       取樣間隔（預設 1s 比 Phase 1 的 2s 細）

        Returns:
            anchor 時間戳（秒，相對於影片開頭）；找不到 None。
        """
        try:
            import cv2
        except ImportError:
            logger.warning("[anchor] opencv-python 未安裝，跳過")
            return None

        tpl_path = template_path or str(
            Path(__file__).parent.parent / "assets" / "templates" / "flash_icon.png"
        )
        tpl = cv2.imread(tpl_path, cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            logger.warning(f"[anchor] 找不到模板：{tpl_path}")
            return None
        if tpl.ndim == 3:
            tpl = tpl[:, :, 0]
        tpl_h, tpl_w = tpl.shape

        # 跟 get_flash_times Phase 1 同尺度範圍
        SCALES = [0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.00, 1.05, 1.10]
        threshold = 0.65

        search_end = search_start_sec + max_search_sec
        logger.info(
            f"[anchor] 掃 {search_start_sec:.0f}~{search_end:.0f}s "
            f"找 ≥{min_icons} flash icons"
        )

        for t, frame in self._iter_frames(search_start_sec, search_end, stride_sec):
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            max_icons_this_frame = 0
            for scale in SCALES:
                stpl = cv2.resize(
                    tpl,
                    (max(1, int(tpl_w * scale)), max(1, int(tpl_h * scale))),
                )
                res = cv2.matchTemplate(gray, stpl, cv2.TM_CCOEFF_NORMED)
                locs = np.where(res >= threshold)
                if len(locs[0]) == 0:
                    continue

                stpl_h, stpl_w = stpl.shape
                # 去重：距離 > 20 像素才視為不同 icon
                candidates: list[tuple[int, int]] = []
                for y, x in zip(locs[0], locs[1]):
                    cx, cy = x + stpl_w // 2, y + stpl_h // 2
                    if all(abs(cx - ex) > 20 or abs(cy - ey) > 20 for ex, ey in candidates):
                        candidates.append((cx, cy))
                if len(candidates) > max_icons_this_frame:
                    max_icons_this_frame = len(candidates)
                if max_icons_this_frame >= min_icons:
                    break  # 該幀已達 threshold，不用試剩下 scale

            if max_icons_this_frame >= min_icons:
                logger.info(
                    f"[anchor] ✓ @{t:.1f}s 偵到 {max_icons_this_frame} flash icons "
                    f"→ game-time anchor"
                )
                return float(t)

        logger.warning(
            f"[anchor] 整個搜尋區間 {search_start_sec:.0f}~{search_end:.0f}s 內"
            f"沒找到 ≥{min_icons} flash icons"
        )
        return None

    def _detect_multi_classes(
        self,
        class_names: list,
        start_sec: float,
        end_sec: float,
        stride_sec: float,
        batch_size: int = 8,
        scale_width: int = 640,
        seek_workers: int = 8,
        chunk_size: int = 0,
        conf_overrides: dict | None = None,
    ) -> dict:
        """
        平行 seek + 批次 YOLO 推論，同時偵測多個類別。
        回傳 {class_name: [timestamp, ...]}

        策略：
          1. ThreadPoolExecutor（seek_workers 個執行緒）平行執行 FFmpeg seek，
             每個 seek 只解碼 1 幀（H.264 keyframe jump ~50ms）。
          2. 全部幀提取完後，用 YOLO batch inference 一次推論。

        chunk_size : 每次載入記憶體的最大幀數（0 = 不分塊，一次全載）。
                     stride=1s 長影片建議設 200，避免 RAM OOM。
                     200 幀 @ 640x360 ≈ 138MB；stride=5s 短影片可保持 0。

        conf_overrides : 5/16 加 — per-class 自訂 confidence threshold（高於 self.conf 才接受）
                         例：{'bp_ui': 0.55} 表示只接受 bp_ui conf≥0.55 的偵測
                         理由：YOLO 對 LPL 隊伍進場 / 舞台空鏡頭誤判 bp_ui (conf 0.41-0.47)，
                              真實 BP conf 通常 ≥0.7，提高 threshold 砍掉 false positive。
                         其他 class（nexus / end_graph_screen）保持 self.conf=0.5 不動。

        與連續 pipe 的差異：
          連續 pipe = 解碼全部 1,047,660 幀，只輸出 582 幀（約 5 分鐘）
          平行 seek = 582 個 seek，每次 ~50ms，8 執行緒 = 約 36 秒
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if self._model is None:
            return {cls: [] for cls in class_names}

        # 計算目標解析度
        if scale_width and scale_width < self._width:
            w = scale_width
            h = int(self._height * scale_width / self._width / 2) * 2
            vf_args = ["-vf", f"scale={w}:{h}"]
        else:
            w, h = self._width, self._height
            vf_args = []

        frame_size = w * h * 3

        timestamps = [
            start_sec + i * stride_sec
            for i in range(int((end_sec - start_sec) / stride_sec) + 1)
            if start_sec + i * stride_sec <= end_sec
        ]
        total = len(timestamps)

        logger.info(
            f"[YOLO multi] 開始掃描  類別={class_names}  "
            f"幀解析度={w}x{h}  batch={batch_size}  總幀數={total}  workers={seek_workers}"
        )

        hwaccel = self._hwaccel
        video_path = str(self.video_path)

        def seek_one(t: float):
            cmd = ["ffmpeg", "-y"]
            if hwaccel:
                cmd += ["-hwaccel", hwaccel]
            cmd += ["-ss", f"{t:.3f}", "-i", video_path, "-frames:v", "1"]
            cmd += vf_args
            cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
            try:
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                raw = proc.stdout
                if len(raw) >= frame_size:
                    return t, np.frombuffer(raw[:frame_size], dtype=np.uint8).reshape(h, w, 3)
            except Exception as e:
                logger.debug(f"[YOLO multi] seek 失敗 t={t:.0f}s: {e}")
            return t, None

        hits: dict = {cls: [] for cls in class_names}
        target_set = set(class_names)

        def flush_batch(batch_frames, batch_times):
            if not batch_frames:
                return
            _kw = {"conf": self.conf, "verbose": False}
            if self.device is not None:
                _kw["device"] = self.device
            results = self._model(batch_frames, **_kw)
            for res, t in zip(results, batch_times):
                # 5/16 改：per-class conf threshold filter（取代純 cls 集合）
                # 對每個 detection 比對 class-specific min_conf，>= 才認可
                hit_classes: set = set()
                cls_arr = res.boxes.cls.tolist()
                conf_arr = res.boxes.conf.tolist()
                for cid, cv in zip(cls_arr, conf_arr):
                    name = res.names[int(cid)]
                    if name not in target_set:
                        continue
                    min_conf = (conf_overrides or {}).get(name, self.conf)
                    if cv >= min_conf:
                        hit_classes.add(name)
                for cls in hit_classes:
                    hits[cls].append(t)

        # chunk_size=0 → 一次處理全部（原有行為，適合幀數少的場景）
        effective_chunk = chunk_size if chunk_size > 0 else total
        done = 0

        for chunk_start in range(0, total, effective_chunk):
            chunk_ts = timestamps[chunk_start: chunk_start + effective_chunk]

            # ── Phase 1：平行 seek 提取此 chunk 的幀 ────────────────────────────
            extracted: list = [None] * len(chunk_ts)
            with ThreadPoolExecutor(max_workers=seek_workers) as pool:
                future_map = {pool.submit(seek_one, t): i for i, t in enumerate(chunk_ts)}
                for future in as_completed(future_map):
                    idx = future_map[future]
                    extracted[idx] = future.result()
                    done += 1
                    if done % 40 == 0 or done == total:
                        pct = done * 100 // total
                        logger.info(f"  [YOLO multi] seek {pct}%  [{done}/{total}]")

            # ── Phase 2：批次 YOLO 推論此 chunk ─────────────────────────────────
            batch_frames: list = []
            batch_times: list = []
            inferred = 0
            valid_count = sum(1 for _, f in extracted if f is not None)
            logger.info(f"  [YOLO multi] 開始推論  {valid_count} 幀...")
            for t, frame in extracted:
                if frame is None:
                    continue
                batch_frames.append(frame)
                batch_times.append(t)
                if len(batch_frames) >= batch_size:
                    flush_batch(batch_frames, batch_times)
                    inferred += len(batch_frames)
                    if valid_count >= 100:  # 幀數少時不打 log（避免雜訊）
                        pct = inferred * 100 // valid_count
                        logger.info(f"  [YOLO multi] infer {pct}%  [{inferred}/{valid_count}]")
                    batch_frames, batch_times = [], []
            flush_batch(batch_frames, batch_times)

        logger.info(
            f"  [YOLO multi] 完成  共 {total} 幀  "
            + "  ".join(f"{cls}={len(hits[cls])}" for cls in class_names)
        )
        return hits

    def _detect_multi_classes_in_ranges(
        self,
        class_names: list,
        time_ranges: list,
        stride_sec: float,
        batch_size: int = 8,
        scale_width: int = 640,
        seek_workers: int = 8,
    ) -> list:
        """
        在指定時間區段內細掃多個類別，回傳每幀的類別計數（不是 hit-times）。

        time_ranges : [(start_sec, end_sec), ...]
        回傳        : [(timestamp, {class_name: count}), ...]，按 timestamp 升序

        典型用途：偵測 hp_bar 從「有」到「無」的瞬間（英雄死亡），
        需要每幀完整 count 才能找轉變點，普通 _detect_multi_classes 不夠用。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if self._model is None or not time_ranges:
            return []

        if scale_width and scale_width < self._width:
            w = scale_width
            h = int(self._height * scale_width / self._width / 2) * 2
            vf_args = ["-vf", f"scale={w}:{h}"]
        else:
            w, h = self._width, self._height
            vf_args = []

        frame_size = w * h * 3

        # 構建所有要掃描的 timestamps（按範圍展開、去重排序）
        timestamps = []
        for rng_start, rng_end in time_ranges:
            n = int((rng_end - rng_start) / stride_sec) + 1
            for i in range(n):
                t = rng_start + i * stride_sec
                if t <= rng_end:
                    timestamps.append(t)
        timestamps = sorted(set(round(t, 3) for t in timestamps))
        total = len(timestamps)

        if total == 0:
            return []

        logger.info(
            f"[YOLO ranges] 開始掃描  類別={class_names}  幀解析度={w}x{h}  "
            f"範圍數={len(time_ranges)}  總幀數={total}  workers={seek_workers}"
        )

        hwaccel = self._hwaccel
        video_path = str(self.video_path)

        def seek_one(t: float):
            cmd = ["ffmpeg", "-y"]
            if hwaccel:
                cmd += ["-hwaccel", hwaccel]
            cmd += ["-ss", f"{t:.3f}", "-i", video_path, "-frames:v", "1"]
            cmd += vf_args
            cmd += ["-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
            try:
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                raw = proc.stdout
                if len(raw) >= frame_size:
                    return t, np.frombuffer(raw[:frame_size], dtype=np.uint8).reshape(h, w, 3)
            except Exception as e:
                logger.debug(f"[YOLO ranges] seek 失敗 t={t:.0f}s: {e}")
            return t, None

        # 平行 seek 提取所有幀
        extracted = [None] * total
        with ThreadPoolExecutor(max_workers=seek_workers) as pool:
            future_map = {pool.submit(seek_one, t): i for i, t in enumerate(timestamps)}
            done = 0
            for future in as_completed(future_map):
                idx = future_map[future]
                extracted[idx] = future.result()
                done += 1
                if done % 50 == 0 or done == total:
                    logger.info(f"  [YOLO ranges] seek {done * 100 // total}%  [{done}/{total}]")

        # 批次推論並累計 per-class count
        target_set = set(class_names)
        per_frame_counts = {}   # t -> {cls: count}

        def flush_batch(batch_frames, batch_times):
            if not batch_frames:
                return
            _kw = {"conf": self.conf, "verbose": False}
            if self.device is not None:
                _kw["device"] = self.device
            results = self._model(batch_frames, **_kw)
            for res, t in zip(results, batch_times):
                counts = {cls: 0 for cls in target_set}
                for c in res.boxes.cls.tolist():
                    name = res.names[int(c)]
                    if name in target_set:
                        counts[name] += 1
                per_frame_counts[t] = counts

        batch_frames, batch_times = [], []
        for t, frame in extracted:
            if frame is None:
                per_frame_counts[t] = {cls: 0 for cls in target_set}
                continue
            batch_frames.append(frame)
            batch_times.append(t)
            if len(batch_frames) >= batch_size:
                flush_batch(batch_frames, batch_times)
                batch_frames, batch_times = [], []
        flush_batch(batch_frames, batch_times)

        # 輸出按時間排序
        result = [(t, per_frame_counts[t]) for t in timestamps]

        total_hits = sum(sum(c.values()) for _, c in result)
        logger.info(f"  [YOLO ranges] 完成  共 {total} 幀  total_detections={total_hits}")
        return result

    # ── FFmpeg 幀提取 ─────────────────────────────────────────────────────────

    def _iter_frames(
        self,
        start_sec: float,
        end_sec: float,
        stride_sec: float,
        scale_width: int | None = None,
    ):
        """
        FFmpeg pipe 批次輸出幀，yield (timestamp, frame_bgr)。
        -hwaccel d3d11va：Windows GPU 解碼，無支援時自動軟解。

        scale_width : 輸出幀寬度（像素）。傳入後 FFmpeg 直接縮圖，
                      None 或 >= self._width 時保持原始解析度。
                      否則 scale() ROI 座標會超出幀邊界。
        """
        if scale_width and scale_width < self._width:
            w = scale_width
            h = int(self._height * scale_width / self._width / 2) * 2  # 確保偶數
            vf = f"fps=1/{stride_sec},scale={w}:{h}"
        else:
            w, h = self._width, self._height
            vf = f"fps=1/{stride_sec}"

        logger.debug(f"[_iter_frames] 提取解析度: {w}x{h}  vf={vf}")
        frame_size = w * h * 3

        cmd = ["ffmpeg"]
        if self._hwaccel:
            cmd += ["-hwaccel", self._hwaccel]
        cmd += [
            "-ss", str(start_sec),
            "-to", str(end_sec),
            "-i", str(self.video_path),
            "-vf", vf,
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "pipe:1",
        ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            t = start_sec
            while True:
                raw = proc.stdout.read(frame_size)
                if len(raw) < frame_size:
                    break
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3)
                yield t, frame
                t += stride_sec
            proc.stdout.close()
            proc.wait()
        except Exception as e:
            logger.error(f"[YOLODetector] FFmpeg pipe 失敗: {e}")

    # ── 工具 ──────────────────────────────────────────────────────────────────

    def _probe_video(self) -> tuple[int, int, float]:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", str(self.video_path)],
            capture_output=True, text=True,
        )
        for s in json.loads(result.stdout).get("streams", []):
            if s.get("codec_type") == "video":
                w = s.get("width", 1920)
                h = s.get("height", 1080)
                rfr = s.get("r_frame_rate", "30/1")
                try:
                    n, d = rfr.split("/")
                    fps = float(n) / float(d)
                except Exception:
                    fps = 30.0
                logger.info(f"[YOLODetector] 影片規格: {w}x{h} @ {fps:.2f}fps")
                return w, h, fps
        return 1920, 1080, 30.0

    def _detect_hwaccel(self) -> str | None:
        try:
            result = subprocess.run(["ffmpeg", "-hwaccels"], capture_output=True, text=True)
            output = result.stdout + result.stderr
            for accel in ["d3d11va", "cuda", "dxva2"]:
                if accel in output:
                    logger.info(f"[YOLODetector] FFmpeg 硬體解碼: {accel}")
                    return accel
        except Exception:
            pass
        return None

"""
bp_analyzer.py — BP 段落音訊 RMS 分析，用於動態計算 BP 剪輯窗口長度。

Phase 49-3e（fail-fast 鐵則 R1）：
  bp_ui_interval=None 時 raise BPNotFoundError，**不再退回 RMS 亂猜**。
  R1「BP 必須從 BP_UI 範圍內選」是強制鐵則，沒抓到 BP_UI 就放棄整支剪輯，
  寧可漏一支 highlight，也不要剪出 BP 段對不上畫面的垃圾片。
"""

import logging
import subprocess

import numpy as np

logger = logging.getLogger(__name__)


class BPNotFoundError(Exception):
    """BP_UI 沒被偵測到 → 無法取 BP 片頭（鐵則 R1 強制）。

    clip_worker 看到 main.py exit code 2 = BPNotFoundError。
    """


def compute_bp_rms(video_path: str, start: float, end: float) -> list:
    """
    用 FFmpeg pipe 提取 [start, end] 段落的音訊，計算每秒 RMS。
    回傳長度 = floor(end - start) 的 float list。
    依賴：numpy、FFmpeg（在 PATH 中）。
    """
    duration = end - start
    if duration <= 0:
        return []
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start), "-to", str(end),
        "-i", video_path,
        "-vn", "-ac", "1", "-ar", "16000",
        "-f", "s16le", "pipe:1",
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        audio = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception as e:
        logger.warning(f"[BP RMS] 音訊提取失敗: {e}")
        return []
    sr = 16000
    n_sec = int(len(audio) / sr)
    rms = []
    for i in range(n_sec):
        chunk = audio[i * sr:(i + 1) * sr]
        rms.append(float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) > 0 else 0.0)
    return rms


def analyze_bp_clip_window(
    bp_end: float,
    rms: list,
    music_cfg: dict,
    bp_ui_interval: dict | None = None,
) -> tuple:
    """
    根據整段 BP 的 RMS 列表（從 bp_end - rms_offset 秒開始，每秒一個值），
    計算動態剪輯窗口 (clip_start, clip_end)。

    搜尋範圍：bp_end - 160s ~ bp_end - 40s（最後 4 選所在區間）。
    若有激動信號，以 RMS 峰值為錨點；
    若無，使用 heuristic T = bp_end - bp_post_pick_wait。

    Phase 40：若提供 bp_ui_interval = {"start": ..., "end": ...}，則
      1) RMS 搜尋範圍 clamp 在 bp_ui_interval 內
      2) 最終輸出窗口 clamp 在 bp_ui_interval 內
      確保 BP 段只會取到真的有 BP_UI 畫面的地方（使用者鐵則）。

    回傳 (clip_start, clip_end)，均已 clamp 在 [0, bp_end] 內。
    """
    dur_min    = float(music_cfg.get("bp_duration_min", 35))
    dur_max    = float(music_cfg.get("bp_duration_max", 65))
    threshold  = float(music_cfg.get("bp_excitement_threshold", 1.5))
    scale      = float(music_cfg.get("bp_excitement_scale", 3.0))
    post_wait  = float(music_cfg.get("bp_post_pick_wait", 42))
    rms_offset = float(music_cfg.get("bp_rms_offset", 750))

    LEAD_IN = 5.0
    POST    = 8.0

    def clamp(v, lo, hi):
        return max(lo, min(hi, v))

    rms_start_abs    = max(0.0, bp_end - rms_offset)  # clamp：短影片 bp_end < rms_offset 時不產生負數
    search_abs_start = bp_end - 160.0
    search_abs_end   = bp_end - 40.0

    # Phase 40：若有 bp_ui_interval，把搜尋範圍鎖在裡面（硬邊界）
    if bp_ui_interval:
        ui_start = float(bp_ui_interval.get("start", 0.0))
        ui_end   = float(bp_ui_interval.get("end", bp_end))
        search_abs_start = max(search_abs_start, ui_start)
        search_abs_end   = min(search_abs_end, ui_end)
        logger.info(
            f"[BP 分析] bp_ui 硬邊界 {ui_start:.0f}~{ui_end:.0f}s → "
            f"搜尋範圍鎖定 {search_abs_start:.0f}~{search_abs_end:.0f}s"
        )

    idx_lo = max(0, int(search_abs_start - rms_start_abs))
    idx_hi = min(len(rms), int(search_abs_end - rms_start_abs))

    median_rms = float(np.median(rms)) if rms and len(rms) > 5 else 0.01

    peak_idx_global = -1
    peak_val = 0.0

    if idx_hi > idx_lo and rms:
        window = rms[idx_lo:idx_hi]
        smoothed = []
        for i in range(len(window)):
            lo3 = max(0, i - 1)
            hi3 = min(len(window), i + 2)
            smoothed.append(float(np.mean(window[lo3:hi3])))

        local_max_i = int(np.argmax(smoothed))
        peak_val = smoothed[local_max_i]
        peak_idx_global = idx_lo + local_max_i

    excitement = peak_val / median_rms if median_rms > 0 else 1.0

    if scale > 1.0:
        ratio = clamp((excitement - 1.0) / (scale - 1.0), 0.0, 1.0)
    else:
        ratio = 0.0
    clip_duration = clamp(dur_min + ratio * (dur_max - dur_min), dur_min, dur_max)

    logger.info(
        f"[BP 分析] excitement={excitement:.2f}  median_rms={median_rms:.4f}"
        f"  peak_rms={peak_val:.4f}  clip_dur={clip_duration:.0f}s"
    )

    # Phase 49-3e（fail-fast）：強制要求 bp_ui_interval。沒有就 raise，不退回亂猜。
    if not bp_ui_interval:
        raise BPNotFoundError(
            "BP_UI 未被 YOLO 偵測到（bp_ui_interval=None），"
            "無法定位 BP 片頭範圍。鐵則 R1：BP 段必須在 BP_UI 範圍內，"
            "本片放棄剪輯。"
        )

    # 修 AE：BP 片頭結尾強制對齊 bp_ui_interval.end
    #   → BP 片頭結束 = BP_UI 真正消失瞬間 = 接 clip 1 + BGM 響起點
    #   → 動態長度仍由 RMS excitement 決定（dur_min~dur_max），但結尾錨點固定
    ui_start = float(bp_ui_interval.get("start", 0.0))
    ui_end   = float(bp_ui_interval.get("end", bp_end))
    clip_end   = ui_end
    clip_start = max(ui_start, ui_end - clip_duration)
    mode = "激動局" if (peak_idx_global >= 0 and excitement >= threshold) else "平淡局"
    logger.info(
        f"[BP 分析] {mode} → 結尾對齊 BP_UI.end={ui_end:.1f}s, "
        f"窗口 {clip_start:.1f}~{clip_end:.1f}s ({clip_duration:.0f}s)"
    )

    # Phase 41：取消個人 mark 延伸 — 使用者明確要求「bp_ui 最後 hit 之前」的內容，
    # 不要包含 bp_ui 消失後的 loading 畫面
    return float(clip_start), float(clip_end)

"""
scene_change_detector.py — 用 FFmpeg 偵測畫面切換點

原理：
  FFmpeg 內建的 scene filter 會對每幀計算與前一幀的差異度（0~1），
  超過 threshold 就標記為 scene change。
  對 LoL 轉播而言，導播切換鏡頭（從中路切到下路、從團戰切到地圖）
  就會產生明顯差異，因此可以作為「場景邊界」訊號。

用法：
    det = SceneChangeDetector(video_path, threshold=0.3)
    times = det.detect(start_sec=0, end_sec=duration)
    # times = [123.5, 456.8, 789.1, ...]

參考：
  FFmpeg filter:   select='gt(scene\\,0.3)'
  showinfo output: pts_time=123.456789
"""

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class SceneChangeDetector:
    """
    用 FFmpeg scene filter 偵測畫面切換點，回傳時間點列表（秒）。

    介面類比 yolo_detector / end_graph_detector / kill_feed_detector，
    只吃影片路徑、吐 list[float]，方便 scan_video.py 統一調度。
    """

    def __init__(
        self,
        video_path: Path,
        threshold: float = 0.3,
    ):
        """
        參數：
            video_path — 影片檔案路徑
            threshold — scene 差異度閾值（0~1），越高越嚴格
                        0.2 → 抓到很多（含鏡頭內物件移動）
                        0.3 → 平衡（推薦，抓到導播切鏡）
                        0.4 → 只抓大幅切換
        """
        self.video_path = Path(video_path)
        self.threshold  = float(threshold)

    # ── 對外介面 ─────────────────────────────────────────────────────────────

    def detect(
        self,
        start_sec: float = 0.0,
        end_sec: float | None = None,
    ) -> list[float]:
        """
        掃描指定範圍，回傳所有 scene change 時間點（相對影片起點，秒）。

        start_sec / end_sec：限制掃描範圍；end_sec=None 代表掃到結尾
        """
        if not self.video_path.exists():
            logger.warning(f"[SceneChangeDetector] 找不到影片：{self.video_path}")
            return []

        cmd = self._build_ffmpeg_cmd(start_sec, end_sec)
        logger.info(
            f"[SceneChangeDetector] 掃描 {start_sec:.0f}s"
            + (f"~{end_sec:.0f}s" if end_sec is not None else " 到結尾")
            + f"  threshold={self.threshold}"
        )

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as e:
            logger.error(f"[SceneChangeDetector] FFmpeg 執行失敗：{e}")
            return []

        times = self._parse_scene_times(proc.stderr, start_sec)
        logger.info(f"[SceneChangeDetector] 偵測到 {len(times)} 個 scene change")
        return times

    # ── FFmpeg 命令組裝 ──────────────────────────────────────────────────────

    def _build_ffmpeg_cmd(
        self,
        start_sec: float,
        end_sec: float | None,
    ) -> list[str]:
        """
        組 FFmpeg 命令：只掃亮度差異（縮圖到 320x180 加速）
        用 showinfo 輸出每個命中幀的 pts_time 到 stderr
        """
        cmd = ["ffmpeg", "-nostats", "-hide_banner"]

        # 使用 -ss / -to 限制範圍（放在 -i 前可以 seek，較快）
        if start_sec > 0:
            cmd += ["-ss", f"{start_sec:.3f}"]
        if end_sec is not None and end_sec > start_sec:
            cmd += ["-to", f"{end_sec:.3f}"]

        cmd += ["-i", str(self.video_path)]

        # scene filter + showinfo + 縮圖加速
        vf = f"scale=320:180,select='gt(scene\\,{self.threshold})',showinfo"
        cmd += [
            "-vf", vf,
            "-an",                       # 不處理音訊
            "-f", "null",
            "-",                         # 不輸出檔案
        ]
        return cmd

    # ── stderr 解析 ─────────────────────────────────────────────────────────

    _PTS_RE = re.compile(r"pts_time:([\d.]+)")

    def _parse_scene_times(self, stderr: str, start_sec: float) -> list[float]:
        """
        從 FFmpeg showinfo 的 stderr 中抽出 pts_time 列表。

        注意：pts_time 是「相對 -ss 起點」的時間，所以要加回 start_sec。
        """
        times: list[float] = []
        for m in self._PTS_RE.finditer(stderr):
            try:
                t_rel = float(m.group(1))
                times.append(start_sec + t_rel)
            except ValueError:
                continue
        # 去重 + 排序（同一幀可能在 showinfo 被記兩次）
        return sorted(set(round(t, 3) for t in times))

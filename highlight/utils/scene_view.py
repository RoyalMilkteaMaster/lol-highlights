"""統一事件資料模型（pandas DataFrame）。

把 scene.json 所有事件（kill_feed、hp_disappear、flash、objective、scene_change、replay）
melt 成 long-form DataFrame，每行 = 一個事件，columns = (time, type, source, weight, metadata)。

定位：「view 層」，提供方便的查詢與聚合，不取代 detect_battles 的演算法（那個還是 list-based）。
權重可從 config.yaml 覆寫，預設值見 DEFAULT_WEIGHTS。

使用範例：
    from highlight.utils.scene_view import EventStore

    store = EventStore.from_scene_json(Path("scene.json"))
    store.kill_times                    # list[float]
    store.per_second_score(window=5.0)  # np.ndarray，每秒加權分數
    store.events_in(1000.0, 1100.0)     # 該區間所有事件 DataFrame
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd


# ── 預設權重（每個事件類型對「精彩度」的貢獻）────────────────────────────────
# 對照 reports/CUTTING_RULES.md 的事件權重；這裡的值供視覺化分數曲線用，
# 不影響 detect_battles 的聚類邏輯。
DEFAULT_WEIGHTS: dict[str, float] = {
    "kill_feed":          100.0,
    "hp_disappear":        80.0,
    "flash":               30.0,
    "objective":          200.0,   # baron / dragon / herald / voidgrub
    "scene_change":         5.0,
    "nexus_explosion":    500.0,
    "game_end_screen":    500.0,
    "end_graph":          300.0,   # Phase 45+：scan_video Step 2a 的結算表（賽後）
    "suspected_game_end": 200.0,   # Phase 45+：kill_feed last+30 的 game_end fallback
    "last_kill_feed":     150.0,   # Phase 45+：最後一個 kill_feed（人類常用來確認 game_end）
    "replay":             -50.0,   # 負權重表示「應排除」
}


# ── EventStore ──────────────────────────────────────────────────────────────

@dataclass
class EventStore:
    """所有 scene.json 事件的統一 DataFrame view。

    df columns:
      - time:    float    事件時間（秒）
      - type:    str      事件類型（kill_feed / hp_disappear / flash / objective ...）
      - source:  str      事件來源（YOLO class / detector 名稱）
      - weight:  float    權重（DEFAULT_WEIGHTS 或 config 覆寫）
      - meta:    str      額外資訊（如 objective 的 type，replay 的 end）
    """
    df: pd.DataFrame = field(default_factory=pd.DataFrame)
    game_start: float = 0.0
    game_end:   float = 0.0
    weights:    dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    # ── Constructor ─────────────────────────────────────────────────────────
    @classmethod
    def from_scene_json(
        cls,
        scene_json: Path,
        weights: dict[str, float] | None = None,
    ) -> "EventStore":
        """從 scene.json 載入並 melt 成 long-form DataFrame。"""
        scene = json.loads(scene_json.read_text(encoding="utf-8"))
        w = dict(DEFAULT_WEIGHTS)
        if weights:
            w.update(weights)

        rows: list[dict] = []

        def _add(time: float, type_: str, source: str, meta: str = ""):
            rows.append({
                "time":   float(time),
                "type":   type_,
                "source": source,
                "weight": w.get(type_, 0.0),
                "meta":   meta,
            })

        for t in scene.get("kill_feed_times",    []) or []:
            _add(t, "kill_feed", "kill_feed_yolo")
        for t in scene.get("hp_disappear_times", []) or []:
            _add(t, "hp_disappear", "yolo_detector")
        for t in scene.get("flash_times",        []) or []:
            _add(t, "flash", "flash_template")
        for ev in scene.get("objective_events",  []) or []:
            _add(ev["time"], "objective", "yolo_detector", meta=ev.get("type", ""))
        for t in scene.get("scene_changes",      []) or []:
            _add(t, "scene_change", "ffmpeg_scene")
        for t in scene.get("nexus_explosion_times", []) or []:
            _add(t, "nexus_explosion", "yolo_detector")
        for t in scene.get("game_end_screen_times", []) or []:
            _add(t, "game_end_screen", "yolo_detector")
        for r in scene.get("replay_segments",    []) or []:
            # replay 用 start 點代表，end 存在 meta
            _add(r["start"], "replay", "yolo_detector", meta=f"end={r['end']:.1f}")

        # Phase 45+：scan_video 提供的單一時間點 (game_end 相關，不是 list)
        eg = scene.get("end_graph_time")
        if eg is not None:
            _add(eg, "end_graph", "end_graph_yolo", meta="scan_video Step 2a")
        sus = scene.get("suspected_game_end")
        if sus is not None:
            _add(sus, "suspected_game_end", "kill_feed_yolo", meta="last_kill+30")
        # 最後一個 kill_feed（人類常用 last kill 確認 game_end）
        kf_list = scene.get("kill_feed_times", []) or []
        if kf_list:
            _add(max(kf_list), "last_kill_feed", "kill_feed_yolo", meta="max(kill_feed_times)")

        df = pd.DataFrame(rows)
        if not df.empty:
            df = df.sort_values("time").reset_index(drop=True)

        return cls(
            df         = df,
            game_start = float(scene.get("bp_end") or 0.0),
            game_end   = float(scene.get("game_end_time") or 0.0),
            weights    = w,
        )

    # ── 屬性：相容舊 list 介面 ────────────────────────────────────────────
    def _times_of(self, type_: str) -> list[float]:
        if self.df.empty:
            return []
        return self.df.loc[self.df["type"] == type_, "time"].tolist()

    @property
    def kill_times(self) -> list[float]:
        return self._times_of("kill_feed")

    @property
    def hp_disappear_times(self) -> list[float]:
        return self._times_of("hp_disappear")

    @property
    def flash_times(self) -> list[float]:
        return self._times_of("flash")

    @property
    def scene_changes(self) -> list[float]:
        return self._times_of("scene_change")

    @property
    def nexus_times(self) -> list[float]:
        return self._times_of("nexus_explosion")

    @property
    def replay_segments(self) -> list[dict]:
        if self.df.empty:
            return []
        out: list[dict] = []
        for _, row in self.df[self.df["type"] == "replay"].iterrows():
            end = float(row["meta"].split("=")[-1])
            out.append({"start": float(row["time"]), "end": end})
        return out

    @property
    def objective_events(self) -> list[dict]:
        if self.df.empty:
            return []
        return [
            {"time": float(row["time"]), "type": row["meta"]}
            for _, row in self.df[self.df["type"] == "objective"].iterrows()
        ]

    # ── 查詢：時間範圍 ────────────────────────────────────────────────────
    def events_in(self, t0: float, t1: float) -> pd.DataFrame:
        """回傳 [t0, t1] 範圍內所有事件 DataFrame。"""
        if self.df.empty:
            return self.df
        mask = (self.df["time"] >= t0) & (self.df["time"] <= t1)
        return self.df.loc[mask]

    # ── 過濾：權重 mask ───────────────────────────────────────────────────
    def filter_by_weight(self, min_weight: float) -> "EventStore":
        """回傳權重 ≥ min_weight 的子集合（新 EventStore，原本不變）。"""
        if self.df.empty:
            return EventStore(df=self.df.copy(), game_start=self.game_start,
                              game_end=self.game_end, weights=self.weights)
        sub = self.df[self.df["weight"] >= min_weight].copy().reset_index(drop=True)
        return EventStore(df=sub, game_start=self.game_start,
                          game_end=self.game_end, weights=self.weights)

    # ── 聚合：每秒加權分數曲線（視覺化核心）──────────────────────────────
    def per_second_score(
        self,
        window: float = 5.0,
        t0: float | None = None,
        t1: float | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        計算「每秒 ± window 內所有事件加權總分」，回傳 (times, scores) 兩個等長 ndarray。

        參數：
          window — 影響半徑，每秒分數 = sum(weights of events in [t-window, t+window])
          t0, t1 — 時間範圍（None 預設用 game_start ~ game_end）
        """
        t0 = self.game_start if t0 is None else t0
        t1 = self.game_end   if t1 is None else t1
        if t1 <= t0:
            return np.array([]), np.array([])

        times = np.arange(int(t0), int(t1) + 1, dtype=float)
        scores = np.zeros_like(times)

        if self.df.empty:
            return times, scores

        ev_times   = self.df["time"].to_numpy()
        ev_weights = self.df["weight"].to_numpy()

        for i, t in enumerate(times):
            mask = (ev_times >= t - window) & (ev_times <= t + window)
            scores[i] = ev_weights[mask].sum()

        return times, scores

    # ── 統計摘要 ───────────────────────────────────────────────────────────
    def summary(self) -> dict[str, int]:
        """回傳每個事件類型的數量。"""
        if self.df.empty:
            return {}
        return self.df["type"].value_counts().to_dict()

    def __repr__(self) -> str:
        s = self.summary()
        s_str = ", ".join(f"{k}={v}" for k, v in sorted(s.items()))
        return (
            f"EventStore(game={self.game_start:.0f}~{self.game_end:.0f}s, "
            f"events={len(self.df)}, {s_str})"
        )

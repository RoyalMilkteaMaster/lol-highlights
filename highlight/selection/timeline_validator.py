"""Timeline-based highlight segment validator（Phase 49-3e v3 Phase D，5/17）。

職責：對 highlight segments 用權威 timeline events 反向驗證，標出可能是
replay / 走路誤判的 segment。

設計：
  輸入：
    - segments：list of Segment-like (start_sec, end_sec) 在 game.mp4 內
    - timeline JSON path：E:/videos/timelines/<date>/<source>_<id>.json
    - anchor_sec：broadcast_games.timeline_anchor_sec（game.mp4 內 = in-game t=0+α）
    - source：'leaguepedia' / 'bilibili'

  邏輯：
    對每 segment 算 in-game time range = (start - anchor, end - anchor)
    看 timeline events 內有沒有事件落在這 range：
      有 CHAMPION_KILL / OBJECTIVE / BUILDING_KILL / SPECIAL_KILL → quality='valid'
      無事件 + 不在 game 開頭 5 min 內 → quality='suspicious'
      無事件 + 在 game 開頭 5 min 內 → quality='early_game'（走路正常）

  輸出：
    list of dict {segment, quality, matched_events, in_game_range_sec}
    caller（pipeline / selection）決定要不要根據 quality 過濾 / 加權

  trigger：暫時為 CLI 獨立模組，未來可 hook 進 pipeline/clip.py。

架構鐵則：
  selection/ 不能 import automation/。讀 disk JSON OK，不違反邊界。

CLI：
  python -m highlight.selection.timeline_validator --game-id 84
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highlight.utils.vod_metadata import lookup_game_timeline_by_id

logger = logging.getLogger(__name__)


# Riot V5 timeline events 對 highlight 有意義的 type
_RELEVANT_V5_TYPES = {
    "CHAMPION_KILL",
    "CHAMPION_SPECIAL_KILL",       # 雙殺三殺等
    "BUILDING_KILL",
    "ELITE_MONSTER_KILL",          # Dragon / Baron / Herald
    "TURRET_PLATE_DESTROYED",
    "DRAGON_SOUL_GIVEN",
    "OBJECTIVE_BOUNTY_PRESTART",
    "GAME_END",
}

# game 開頭 5 min 內無事件不算 suspicious（早期 walking 正常）
EARLY_GAME_FORGIVE_SEC = 300.0


@dataclass
class TimelineEvent:
    """正規化兩來源後的 highlight-relevant 事件。"""
    in_game_time_sec: float       # 從 in-game 0 算
    event_type: str
    description: str
    team: str | None = None       # 'red'/'blue'/None
    raw: dict = field(default_factory=dict)


@dataclass
class SegmentValidation:
    """單 segment 驗證結果。"""
    segment_start: float          # game.mp4 內秒數
    segment_end: float
    in_game_start: float          # 對應 in-game time
    in_game_end: float
    quality: str                  # 'valid' / 'suspicious' / 'early_game' / 'before_game' / 'after_game'
    matched_events: list[TimelineEvent] = field(default_factory=list)
    note: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Timeline JSON parsing
# ─────────────────────────────────────────────────────────────────────────────
def load_timeline_events(timeline_path: Path, source: str) -> list[TimelineEvent]:
    """讀 disk timeline JSON 並正規化成 TimelineEvent list（只含 highlight-relevant）。

    source: 'leaguepedia' (Riot V5 format) / 'bilibili' (view_points format)
    """
    if not timeline_path.is_file():
        logger.warning("[timeline_validator] %s 不存在", timeline_path)
        return []
    with timeline_path.open(encoding="utf-8") as f:
        raw = json.load(f)

    if source == "leaguepedia":
        return _parse_leaguepedia_events(raw)
    if source == "bilibili":
        return _parse_bilibili_events(raw)
    logger.warning("[timeline_validator] 不支援 source: %s", source)
    return []


def _parse_leaguepedia_events(raw: dict) -> list[TimelineEvent]:
    """V5 timeline 內 frames[].events[] 抽 highlight-relevant 事件。"""
    out: list[TimelineEvent] = []
    for frame in raw.get("frames") or []:
        for ev in frame.get("events") or []:
            etype = ev.get("type", "")
            if etype not in _RELEVANT_V5_TYPES:
                continue
            ts_ms = ev.get("timestamp", 0)
            desc = _describe_v5_event(ev)
            out.append(TimelineEvent(
                in_game_time_sec=ts_ms / 1000.0,
                event_type=etype,
                description=desc,
                team=None,             # V5 沒直接 team，要 lookup participantId → team
                raw=ev,
            ))
    out.sort(key=lambda e: e.in_game_time_sec)
    return out


def _describe_v5_event(ev: dict) -> str:
    """V5 event 中文簡述。"""
    t = ev.get("type", "")
    if t == "CHAMPION_KILL":
        assist_n = len(ev.get("assistingParticipantIds") or [])
        return f"擊殺 (killer={ev.get('killerId')}, assist={assist_n})"
    if t == "CHAMPION_SPECIAL_KILL":
        kt = ev.get("killType", "?")
        return f"特殊擊殺 {kt}"
    if t == "BUILDING_KILL":
        bt = ev.get("buildingType", "")
        lt = ev.get("laneType", "")
        return f"建築擊殺 {bt} {lt}".strip()
    if t == "ELITE_MONSTER_KILL":
        mt = ev.get("monsterType", "")
        mst = ev.get("monsterSubType", "")
        return f"擊殺 {mt} {mst}".strip()
    if t == "TURRET_PLATE_DESTROYED":
        return "破塔皮"
    if t == "DRAGON_SOUL_GIVEN":
        return f"龍魂 {ev.get('name','?')}"
    if t == "GAME_END":
        return f"GAME_END (winner={ev.get('winningTeam','?')})"
    return t


def find_anchor_via_kill_correlation(
    yolo_kill_times_mp4: list[float],
    timeline_kill_times_ingame: list[float],
    *,
    tolerance_sec: float = 2.0,
    consecutive_required: int = 4,
    min_anchor: float = 30.0,
    max_anchor: float = 600.0,
) -> tuple[float | None, int]:
    """Phase E2 (5/17 user 確認用 4 連續)：用 YOLO kill_feed × Leaguepedia
    CHAMPION_KILL cross-correlation 找 anchor。

    對每個 candidate anchor (= yolo[i] - timeline[j])，掃所有 YOLO kills 看
    「連續 N 個 都能對到 timeline kill within ±tolerance」，找到的就接受。

    比 flash icon anchor 精準很多（後者 systematic 偏移 30-40s 因 HUD 動畫）。

    Args:
        yolo_kill_times_mp4:       SCAN scene.json kill_feed_times (mp4 內秒)
        timeline_kill_times_ingame: Riot V5 CHAMPION_KILL timestamps (in-game 秒)
        tolerance_sec:             單一 match 容差（user 確認 ±2s）
        consecutive_required:      要連續 N 個 match（user 確認 4）
        min_anchor / max_anchor:   anchor 合理範圍（BP+載入通常 100-300s）

    Returns:
        (anchor_sec, max_run):
          anchor_sec = mp4 內第幾秒 = in-game t=0
          max_run    = 找到的最長連續 match 數（debug 用）
          失敗回 (None, 0)
    """
    if len(yolo_kill_times_mp4) < consecutive_required:
        return None, 0
    if len(timeline_kill_times_ingame) < consecutive_required:
        return None, 0

    yolo_sorted = sorted(yolo_kill_times_mp4)
    tl_sorted = sorted(timeline_kill_times_ingame)

    # 列所有 candidate anchor（不重複，整數精度）
    candidates: set[float] = set()
    for i in range(len(yolo_sorted)):
        for j in range(len(tl_sorted)):
            a = yolo_sorted[i] - tl_sorted[j]
            if min_anchor < a < max_anchor:
                candidates.add(round(a, 1))

    # 對每個 candidate，找最長連續 match
    best_anchor: float | None = None
    best_run: int = 0
    for a in sorted(candidates):
        max_run = 0
        cur_run = 0
        for yk in yolo_sorted:
            ig = yk - a
            # 看 timeline 內有沒有 ±tolerance 內的 kill
            if any(abs(tk - ig) <= tolerance_sec for tk in tl_sorted):
                cur_run += 1
                if cur_run > max_run:
                    max_run = cur_run
            else:
                cur_run = 0
        if max_run >= consecutive_required and max_run > best_run:
            best_run = max_run
            best_anchor = a

    return best_anchor, best_run


def _parse_bilibili_events(raw: dict) -> list[TimelineEvent]:
    """Bilibili view_points (每 entry from/to/content/team_name)。"""
    out: list[TimelineEvent] = []
    for vp in raw.get("view_points") or []:
        t = float(vp.get("from", 0))
        out.append(TimelineEvent(
            in_game_time_sec=t,
            event_type="VIEW_POINT",
            description=str(vp.get("content", "")),
            team=str(vp.get("team_type") or "") or None,
            raw=vp,
        ))
    out.sort(key=lambda e: e.in_game_time_sec)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────────────────────
def validate_segments(
    segments: list[dict],         # [{start, end, ...}] (game.mp4 內秒)
    events: list[TimelineEvent],
    anchor_sec: float,
    game_duration_sec: float | None = None,
) -> list[SegmentValidation]:
    """對每 segment 算 in-game range 並比對 events。

    Args:
        segments: list of dict 含 'start' / 'end' fields (game.mp4 內秒)
        events: load_timeline_events 回傳
        anchor_sec: broadcast_games.timeline_anchor_sec
        game_duration_sec: timeline GAME_END_sec（可選）— 超過此值的 segment 標 after_game
    """
    # 5/18 Bug 6：game_duration_sec NULL 時 fallback 用 events 最大時間（V5 timeline
    # 都有 GAME_END frame，duration 一定有；Bilibili view_points 沒 GAME_END，
    # 用 max(view_point.to) 推估）
    if game_duration_sec is None and events:
        game_duration_sec = max(ev.in_game_time_sec for ev in events) + 60.0
        # +60s buffer（事件可能 sample 不到結尾）

    results: list[SegmentValidation] = []
    for seg in segments:
        start_mp4 = float(seg["start"])
        end_mp4 = float(seg["end"])
        in_start = start_mp4 - anchor_sec
        in_end = end_mp4 - anchor_sec

        # 在 game 之前（譬如 BP 段、Pause 等）
        if in_end < 0:
            results.append(SegmentValidation(
                segment_start=start_mp4, segment_end=end_mp4,
                in_game_start=in_start, in_game_end=in_end,
                quality="before_game",
                note="segment 完全在 anchor 之前（BP / 載入 / 開頭預錄）",
            ))
            continue

        # 在 game 之後（譬如 outro / 訪談）
        if game_duration_sec is not None and in_start > game_duration_sec:
            results.append(SegmentValidation(
                segment_start=start_mp4, segment_end=end_mp4,
                in_game_start=in_start, in_game_end=in_end,
                quality="after_game",
                note=f"segment 在 game_end_sec={game_duration_sec:.0f} 之後（outro / 訪談）",
            ))
            continue

        # 找 segment range 內的事件
        matched = [ev for ev in events
                   if in_start <= ev.in_game_time_sec <= in_end]

        if matched:
            results.append(SegmentValidation(
                segment_start=start_mp4, segment_end=end_mp4,
                in_game_start=in_start, in_game_end=in_end,
                quality="valid",
                matched_events=matched,
                note=f"含 {len(matched)} 個 timeline 事件",
            ))
        else:
            # 無事件
            # 5/18 Bug D：用 in_start 不是 in_end — segment 起點在 early game 內
            # 就 forgive（譬如 [250s, 310s] 主體仍在 5 min 內走位 phase）。
            if in_start < EARLY_GAME_FORGIVE_SEC:
                results.append(SegmentValidation(
                    segment_start=start_mp4, segment_end=end_mp4,
                    in_game_start=in_start, in_game_end=in_end,
                    quality="early_game",
                    note=f"游戏开头 {EARLY_GAME_FORGIVE_SEC:.0f}s 內無事件（走位正常）",
                ))
            else:
                results.append(SegmentValidation(
                    segment_start=start_mp4, segment_end=end_mp4,
                    in_game_start=in_start, in_game_end=in_end,
                    quality="suspicious",
                    note="game 中段無 timeline 事件 — 可能是 replay 或走位誤判",
                ))
    return results


def summarize_validation(results: list[SegmentValidation]) -> dict:
    """快速統計 + 過濾建議。"""
    counts = {}
    for r in results:
        counts[r.quality] = counts.get(r.quality, 0) + 1
    suspicious = [r for r in results if r.quality == "suspicious"]
    return {
        "total": len(results),
        "by_quality": counts,
        "suspicious_segments": [
            {"start": r.segment_start, "end": r.segment_end, "note": r.note}
            for r in suspicious
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="Timeline-based segment validator (Phase D)",
    )
    parser.add_argument("--game-id", type=int, required=True,
                        help="DB broadcast_games.game_id")
    parser.add_argument("--scene-json", type=str,
                        help="scene.json 路徑（含 highlight segments）— 沒指定走 DB 找")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    row = lookup_game_timeline_by_id(args.game_id)

    if not row:
        print(f"game_id {args.game_id} 不存在")
        return 1
    anchor = row.get("timeline_anchor_sec")
    source = row.get("timeline_source")
    duration = row.get("timeline_game_duration_sec")
    external_id = row.get("timeline_external_id")
    if not anchor or source in (None, "none"):
        print(f"g{args.game_id} timeline 未對齊（先跑 timeline_anchor）")
        return 2

    # 找 timeline JSON
    bd = row["broadcast_date"]
    from highlight.utils import paths as _paths
    timelines_dir = _paths.timelines_dir() / bd.strftime("%Y-%m-%d")
    if external_id:
        # 精確對映：用 DB 內 timeline_external_id 直接找對應 JSON
        # 替換不允許字元（跟 timeline.py _save_raw 一致）
        import re as _re
        safe_id = _re.sub(r"[^A-Za-z0-9._-]", "_", external_id)
        tl_path = timelines_dir / f"{source}_{safe_id}.json"
        if not tl_path.is_file():
            print(f"timeline JSON 不存在: {tl_path}")
            return 3
    else:
        # 沒對映：掃 dir 取 source 第一個（fallback）
        candidates = sorted(timelines_dir.glob(f"{source}_*.json"))
        if not candidates:
            print(f"timeline JSON 在 {timelines_dir} 內找不到 {source}_*.json")
            return 3
        if len(candidates) > 1:
            print(f"[WARN] 沒 timeline_external_id 對映，用第一個 {source} timeline（可能不對）")
        tl_path = candidates[0]
    print(f"=== Loading timeline: {tl_path.name} ===")
    events = load_timeline_events(tl_path, source)
    print(f"  {len(events)} highlight-relevant events")
    print(f"  anchor={anchor:.1f}s game_duration={duration:.0f}s source={source}")

    # 拿 segments：從 scene.json 或 game.mp4 對應的 finals/<stem>_highlights.mp4 scene
    if args.scene_json:
        scene_path = Path(args.scene_json)
    else:
        # 沒指定 — 從 game_path 推 scene.json
        gp = Path(row["game_path"]) if row["game_path"] else None
        if not gp:
            print("沒 game_path 也沒 --scene-json")
            return 4
        scene_path = gp.with_suffix(".scene.json")
        if not scene_path.is_file():
            # 試 scan/ 目錄
            from highlight.utils import paths as _paths
            scene_path = _paths.scan_dir() / f"{gp.stem}_scene.json"
    if not scene_path.is_file():
        print(f"找不到 scene.json: {scene_path}")
        return 5
    with scene_path.open(encoding="utf-8") as f:
        scene = json.load(f)

    # scene.json 內可能 game_clips（chosen segments）；不一定有
    segs = scene.get("game_clips") or scene.get("clips") or scene.get("segments") or []
    if not segs:
        print(f"scene.json 內無 clips/segments 欄位")
        print(f"top-level keys: {list(scene.keys())[:10]}")
        return 6

    print(f"  {len(segs)} segments to validate")
    print()

    results = validate_segments(
        segments=segs,
        events=events,
        anchor_sec=anchor,
        game_duration_sec=duration,
    )

    print("=== Validation results ===")
    for r in results:
        marker = {
            "valid": "[OK]",
            "suspicious": "[X]",
            "early_game": "[EARLY]",
            "before_game": "[BEFORE]",
            "after_game": "[AFTER]",
        }.get(r.quality, "[?]")
        print(f"  {marker} [{r.quality:11}] mp4 {r.segment_start:5.0f}~{r.segment_end:5.0f}s  "
              f"(in-game {r.in_game_start:5.0f}~{r.in_game_end:5.0f})  "
              f"events={len(r.matched_events)}  {r.note}")
        if r.matched_events and len(r.matched_events) <= 3:
            for ev in r.matched_events:
                print(f"      [{ev.in_game_time_sec:5.0f}s {ev.event_type:25}] {ev.description}")

    print()
    summary = summarize_validation(results)
    print(f"=== Summary ===")
    print(f"  total={summary['total']}")
    for q, c in summary["by_quality"].items():
        print(f"  {q}: {c}")
    if summary["suspicious_segments"]:
        print(f"\n[WARN] {len(summary['suspicious_segments'])} 個 suspicious segments:")
        for s in summary["suspicious_segments"][:10]:
            print(f"  mp4 {s['start']:.0f}~{s['end']:.0f}s  {s['note']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

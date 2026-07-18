"""把 scene.json + select_highlights 結果視覺化成 HTML 時間軸（純 SVG，瀏覽器直開）。

三層疊加視圖：每秒加權分數曲線（背景）+ 事件 markers（中層）+ clip 矩形（上層）。
可疑事件用紅色 outline + ⚠ 標出（kill_feed/hp 在 game_end 後、孤立 kill 等）。

使用方式：
  python -m dashboard.timeline_viewer                  # 自動找最新 scene.json
  python -m dashboard.timeline_viewer <scene.json>
  python -m dashboard.timeline_viewer [--serve]        # http://localhost:8766 + slider 調權重

最後輸出固定位置（VS Code Ctrl+Click 直接打開）：
  file:///C:/Users/lesli/Claude%20code/lol-highlights/dashboard/timeline_viewer.html

互動：滑鼠滾輪 zoom（X 軸為中心）；shift+滾輪 = 平移。
"""

from __future__ import annotations

import argparse
import html
import io
import json
import os
import sys
from pathlib import Path

# 讓 import core / pipeline 等找得到
sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

# Windows cp950 console 不支援部分 emoji/Unicode 的 stdout 改寫移進 main() 內，
# 避免 import 時改 sys.stdout 跟 caller (clip.py 的 _TeeToLog) 衝突
# (AttributeError: '_TeeToLog' object has no attribute 'buffer')

from highlight.utils.scene_view import EventStore, DEFAULT_WEIGHTS

# ── 視覺參數 ─────────────────────────────────────────────────────────────────

EVENT_COLORS = {
    "kill_feed":          "#e63946",   # 紅
    "hp_disappear":       "#f4a261",   # 橙
    "flash":              "#ffd166",   # 黃
    "objective":          "#9b5de5",   # 紫
    "scene_change":       "#52b788",   # 綠
    "nexus_explosion":    "#06d6a0",   # 青綠
    "game_end_screen":    "#118ab2",   # 藍綠
    "end_graph":          "#ff006e",   # 桃紅（結算表）
    "suspected_game_end": "#fb5607",   # 橘紅（kill_feed last+30 fallback）
    "last_kill_feed":     "#ffbe0b",   # 金黃（最後一個 kill_feed）
    "replay":             "#264653",   # 深藍（背景條）
}

EVENT_LABEL = {
    "kill_feed":          "Kill",
    "hp_disappear":       "HP↓",
    "flash":              "Flash",
    "objective":          "Obj",
    "scene_change":       "Scene",
    "nexus_explosion":    "Nexus",
    "game_end_screen":    "Victory",
    "end_graph":          "EndGraph",
    "suspected_game_end": "SusEnd",
    "last_kill_feed":     "LastKill",
    "replay":             "Replay",
}


def mmss(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    return f"{m:02d}:{s:02d}"


# ── 修 g5：可疑事件判斷 ─────────────────────────────────────────────────────
SUSPECT_COLOR = "#ff1744"   # 強紅 outline


def is_suspect_event(
    row,
    store: EventStore,
    hp_times_sorted: list[float],
    kill_times_sorted: list[float],
    flash_times_sorted: list[float] | None = None,
    cluster_gap: float = 15.0,
    hp_window: float = 18.0,
    flash_window: float = 18.0,
) -> tuple[bool, str]:
    """回傳 (是否可疑, 原因說明)。

    判斷規則（對齊 modules/clip_filter.py:filter_isolated_unverified_kills 修 A + AG 邏輯）：
      - kill_feed / hp_disappear 出現在 game_end 之後 → 修 H 已過濾（YOLO 看結算圖表誤判）
      - victory_screen 距 game_end > 60s → cluster filter 沒抓到的離群 hit
      - nexus_explosion 距 game_end > 60s → 早期 tower 爆炸誤判
      - kill_feed 「孤立 cluster」(±cluster_gap 內無其他 kill) AND ±hp_window 內無 hp_disappear
        AND ±flash_window 內無 flash → 真正可疑（單一無支撐的 false positive 候選）
        多 kill cluster 即使無 hp/flash 也信任（修 A 認定為真戰鬥被 hp 漏偵）
    """
    import bisect
    type_ = row["type"]
    t = row["time"]
    ge = store.game_end

    if type_ in ("kill_feed", "hp_disappear") and ge and t > ge + 1:
        return True, f"在 game_end({mmss(ge)}) 之後（YOLO 結算圖表誤判候選）"

    if type_ == "game_end_screen" and ge and abs(t - ge) > 60:
        return True, f"距 game_end({mmss(ge)}) 超過 60s"

    if type_ == "nexus_explosion" and ge and abs(t - ge) > 60:
        return True, f"距 game_end({mmss(ge)}) 超過 60s（疑早期誤判）"

    if type_ == "kill_feed":
        # 修 g5.2：對齊修 A — 先看 cluster，cluster ≥ 2 自動信任
        # 找 ±cluster_gap 內其他 kill_feed
        if kill_times_sorted:
            idx = bisect.bisect_left(kill_times_sorted, t)
            has_neighbor = False
            for j in (idx - 1, idx, idx + 1):
                if 0 <= j < len(kill_times_sorted):
                    other = kill_times_sorted[j]
                    if other != t and abs(other - t) <= cluster_gap:
                        has_neighbor = True
                        break
            if has_neighbor:
                return False, ""   # cluster ≥ 2 信任，不視為 sus

        # 孤立 kill → 檢查 hp_disappear 或 flash（修 AG）
        has_hp = False
        if hp_times_sorted:
            idx = bisect.bisect_left(hp_times_sorted, t)
            candidates = []
            if idx < len(hp_times_sorted): candidates.append(hp_times_sorted[idx])
            if idx > 0: candidates.append(hp_times_sorted[idx - 1])
            has_hp = any(abs(hp - t) <= hp_window for hp in candidates)

        has_flash = False
        if flash_times_sorted:
            idx = bisect.bisect_left(flash_times_sorted, t)
            candidates = []
            if idx < len(flash_times_sorted): candidates.append(flash_times_sorted[idx])
            if idx > 0: candidates.append(flash_times_sorted[idx - 1])
            has_flash = any(abs(f - t) <= flash_window for f in candidates)

        if not (has_hp or has_flash):
            return True, (
                f"孤立 kill（±{cluster_gap:.0f}s 無其他 kill）"
                f"且 ±{hp_window:.0f}s 無 hp_disappear / ±{flash_window:.0f}s 無 flash 支撐"
            )

    return False, ""


# ── select_highlights 包裝（避免 import clip.py 時跑 _TeeToLog stdout 重定向）
def get_clips_from_scene(
    scene_json: Path,
    minimal: bool = False,
) -> list[dict]:
    """呼叫 select_highlights 取得最終 clip 列表。"""
    from highlight.pipeline.clip import select_highlights

    scene = json.loads(scene_json.read_text(encoding="utf-8"))
    bp_end = scene.get("bp_end")
    game_end = scene.get("game_end_time")

    return select_highlights(
        scene_json,
        kill_feed_times    = scene.get("kill_feed_times")    or None,
        victory_times      = scene.get("nexus_explosion_times") or None,
        flash_times        = scene.get("flash_times")        or None,
        bp_end             = bp_end,
        game_end_override  = game_end,
        hp_disappear_times = scene.get("hp_disappear_times") or None,
        minimal            = minimal,
    )


# ── HTML 渲染 ────────────────────────────────────────────────────────────────

# Phase 49-3e v3 (5/17)：Leaguepedia/Bilibili 真實 timeline events parser
# Dragon subType 中文映射
_DRAGON_NAME_MAP = {
    "AIR_DRAGON": "雲龍", "AIR": "雲龍",
    "EARTH_DRAGON": "土龍", "EARTH": "土龍", "MOUNTAIN": "土龍",
    "FIRE_DRAGON": "火龍", "FIRE": "火龍", "INFERNAL": "火龍",
    "WATER_DRAGON": "水龍", "WATER": "水龍", "OCEAN": "水龍",
    "CHEMTECH_DRAGON": "化龍", "CHEMTECH": "化龍",
    "HEXTECH_DRAGON": "極龍", "HEXTECH": "極龍",
    "ELDER_DRAGON": "遠古龍", "ELDER": "遠古龍",
}
_SPECIAL_KILL_MAP = {
    "DOUBLE_KILL": "雙殺", "TRIPLE_KILL": "三殺",
    "QUADRA_KILL": "四殺", "PENTA_KILL": "五殺",
    "KILL_FIRST_BLOOD": "一血",
}


def _parse_v5_official_event(ev: dict) -> dict | None:
    """V5 Riot timeline event 解析成 {label, category, team_id}（None=不展示）。"""
    t = ev.get("type", "")
    ts_ms = ev.get("timestamp", 0)
    if t == "ELITE_MONSTER_KILL":
        mt = ev.get("monsterType", "")
        mst = ev.get("monsterSubType", "")
        team = ev.get("killerTeamId", 0)  # 100=blue, 200=red
        if mt == "DRAGON":
            label = _DRAGON_NAME_MAP.get(mst, f"龍({mst})")
            return {"time_ms": ts_ms, "category": "dragon",
                    "label": label, "team_id": team}
        if mt == "BARON_NASHOR":
            return {"time_ms": ts_ms, "category": "baron",
                    "label": "男爵", "team_id": team}
        if mt == "RIFTHERALD":
            return {"time_ms": ts_ms, "category": "herald",
                    "label": "先鋒", "team_id": team}
        if mt == "HORDE":
            return {"time_ms": ts_ms, "category": "voidgrub",
                    "label": "巢蟲", "team_id": team}
        return {"time_ms": ts_ms, "category": "monster",
                "label": mt, "team_id": team}
    if t == "BUILDING_KILL":
        bt = ev.get("buildingType", "")
        lt = ev.get("laneType", "")
        team = ev.get("teamId", 0)
        if bt == "TOWER_BUILDING":
            return {"time_ms": ts_ms, "category": "tower",
                    "label": f"塔 {lt[:3]}", "team_id": team}
        if bt == "INHIBITOR_BUILDING":
            return {"time_ms": ts_ms, "category": "inhibitor",
                    "label": f"兵營 {lt[:3]}", "team_id": team}
        return None
    if t == "CHAMPION_SPECIAL_KILL":
        kt = ev.get("killType", "")
        team = ev.get("killerTeamId", 0)
        label = _SPECIAL_KILL_MAP.get(kt, kt)
        return {"time_ms": ts_ms, "category": "special",
                "label": label, "team_id": team}
    if t == "DRAGON_SOUL_GIVEN":
        team = ev.get("teamId", 0)
        return {"time_ms": ts_ms, "category": "soul",
                "label": f"龍魂 {ev.get('name','?')}", "team_id": team}
    if t == "GAME_END":
        return {"time_ms": ts_ms, "category": "nexus",
                "label": "主堡爆", "team_id": ev.get("winningTeam", 0)}
    return None


def _parse_bilibili_view_point(vp: dict) -> dict | None:
    """Bilibili view_points entry → {time_ms, label, team_name}。"""
    from_sec = vp.get("from", 0)
    content = (vp.get("content") or "").strip()
    team_name = vp.get("team_name") or ""
    team_type = vp.get("team_type") or ""   # red / blue
    # category 從 content 判
    cat = "obj"
    if "亚龙" in content or "龙" in content and "纳什" not in content:
        cat = "dragon"
    elif "纳什男爵" in content or "男爵" in content:
        cat = "baron"
    elif "峡谷先锋" in content or "先锋" in content:
        cat = "herald"
    elif "虚空巢虫" in content or "巢虫" in content:
        cat = "voidgrub"
    elif "第一滴血" in content or "first blood" in content.lower():
        cat = "special"
    elif "团灭" in content:
        cat = "special"
    elif "开始" in content:
        cat = "start"
    # 用 team_type 對應 100/200（red=200, blue=100）
    team_id = 200 if team_type == "red" else (100 if team_type == "blue" else 0)
    return {"time_ms": int(from_sec * 1000), "category": cat,
            "label": content, "team_id": team_id, "team_name": team_name}


def load_official_events_for_scene(scene_path: Path) -> tuple[list[dict], float | None]:
    """從 scene.json path 反查 DB，載入對應 Leaguepedia / Bilibili timeline 事件。

    回傳：(events list with mp4_time, source_label)
      events 內元素：{time_sec (mp4), category, label, team_id, ...}
      source_label：'LCK Leaguepedia' / 'LPL Bilibili' / None
    沒對到 DB 或 timeline 未準備好 → ([], None)
    """
    try:
        # 從 scene.json stem 反推 game stem（去掉 '_scene' 尾巴）
        stem = scene_path.stem
        if stem.endswith("_scene"):
            game_stem = stem[:-6]
        else:
            game_stem = stem
        game_mp4_name = f"{game_stem}.mp4"

        from automation.db.connection import mysql_conn
        with mysql_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT bg.game_id, bg.timeline_anchor_sec, bg.timeline_source,
                       bg.timeline_external_id, bg.game_path,
                       b.broadcast_date, b.league_code
                FROM broadcast_games bg
                JOIN broadcasts b ON b.broadcast_id = bg.broadcast_id
                WHERE bg.game_path LIKE %s
                ORDER BY bg.game_id DESC LIMIT 1
            """, (f"%{game_mp4_name}",))
            row = cur.fetchone()
    except Exception as e:
        print(f"[timeline_viewer] DB 查 timeline 失敗: {e}")
        return [], None

    if not row:
        return [], None
    anchor = row.get("timeline_anchor_sec")
    source = row.get("timeline_source")
    external_id = row.get("timeline_external_id")
    if not anchor or source in (None, "none") or not external_id:
        return [], None

    bd = row["broadcast_date"]
    from highlight.utils import paths as _paths
    timelines_dir = _paths.timelines_dir() / bd.strftime("%Y-%m-%d")
    import re as _re
    safe_id = _re.sub(r"[^A-Za-z0-9._-]", "_", external_id)
    tl_path = timelines_dir / f"{source}_{safe_id}.json"
    if not tl_path.is_file():
        print(f"[timeline_viewer] timeline JSON 不存在: {tl_path}")
        return [], None

    raw = json.loads(tl_path.read_text(encoding="utf-8"))
    events: list[dict] = []
    if source == "leaguepedia":
        for frame in raw.get("frames") or []:
            for ev in frame.get("events") or []:
                parsed = _parse_v5_official_event(ev)
                if parsed:
                    events.append(parsed)
    elif source == "bilibili":
        for vp in raw.get("view_points") or []:
            parsed = _parse_bilibili_view_point(vp)
            if parsed:
                events.append(parsed)

    # 轉成 mp4 time
    for e in events:
        e["time_sec"] = anchor + (e["time_ms"] / 1000.0)
    events.sort(key=lambda e: e["time_sec"])
    src_label = f"{row['league_code']} {source}"
    return events, src_label


def render_html(
    store: EventStore,
    clips: list[dict],
    title: str = "Timeline Viewer",
    end_graph_time: float | None = None,
    official_events: list[dict] | None = None,
    official_source: str | None = None,
) -> str:
    """產出單頁 HTML（含 SVG 時間軸 + 事件 + clip 矩形 + 分數曲線）。

    修 g1：X 軸範圍嚴格用 game_start ~ max(game_end, end_graph + 30s)，
           不被 clips（含早期誤判）拉長，方便看清比賽中段細節。
    """
    t0 = store.game_start
    t1 = store.game_end
    if end_graph_time and end_graph_time > t1:
        t1 = end_graph_time + 30.0   # 留 30s 給 end_graph_visual 段尾
    if t1 <= t0:
        # fallback：如果 game_end 沒設好，用 clips 範圍
        t1 = max((c["end"] for c in clips), default=t0 + 60)
    duration = max(1.0, t1 - t0)

    # 計算每秒分數
    times, scores = store.per_second_score(window=5.0, t0=t0, t1=t1)
    score_max = max(float(scores.max()), 1.0)

    # SVG 尺寸
    SVG_W = 1800
    SVG_H = 600
    PAD_L = 60
    PAD_R = 30
    PAD_T = 60
    PAD_B = 80
    PLOT_W = SVG_W - PAD_L - PAD_R
    PLOT_H = SVG_H - PAD_T - PAD_B

    def x_of(t: float) -> float:
        return PAD_L + (t - t0) / duration * PLOT_W

    def y_of_score(s: float) -> float:
        return PAD_T + PLOT_H - (s / score_max) * PLOT_H

    # 1. 背景：每秒分數曲線（區塊填色，深淺表示分數）
    score_polyline_pts = " ".join(
        f"{x_of(t):.1f},{y_of_score(s):.1f}" for t, s in zip(times, scores)
    )
    score_area_pts = (
        f"{x_of(t0):.1f},{PAD_T + PLOT_H:.1f} " +
        score_polyline_pts +
        f" {x_of(t1):.1f},{PAD_T + PLOT_H:.1f}"
    )

    # 2. Replay 區段（明顯藍色斜線條紋 + 上下實線邊框 + 文字標籤）
    replay_rects = []
    for r in store.replay_segments:
        x0 = x_of(r["start"])
        x1 = x_of(r["end"])
        w  = x1 - x0
        rep_color = EVENT_COLORS["replay"]
        tooltip = f'Replay {mmss(r["start"])}~{mmss(r["end"])} ({r["end"]-r["start"]:.0f}s)'
        # 半透明藍底
        replay_rects.append(
            f'<rect x="{x0:.1f}" y="{PAD_T}" width="{w:.1f}" height="{PLOT_H}" '
            f'fill="{rep_color}" opacity="0.30" stroke="{rep_color}" stroke-width="1.5" '
            f'stroke-dasharray="3,2">'
            f'<title>{tooltip}</title></rect>'
        )
        # 上方標籤
        if w > 25:
            replay_rects.append(
                f'<text x="{(x0+x1)/2:.1f}" y="{PAD_T + 12:.0f}" text-anchor="middle" '
                f'font-size="10" font-weight="bold" fill="{rep_color}">'
                f'REPLAY {r["end"]-r["start"]:.0f}s</text>'
            )

    # 3. 事件 markers（小圓點，按類型排不同高度）
    type_y_offset = {
        "kill_feed":        PAD_T + PLOT_H * 0.10,
        "hp_disappear":     PAD_T + PLOT_H * 0.20,
        "flash":            PAD_T + PLOT_H * 0.30,
        "objective":         PAD_T + PLOT_H * 0.40,
        "scene_change":      PAD_T + PLOT_H * 0.95,
        "nexus_explosion":   PAD_T + PLOT_H * 0.50,
        "game_end_screen":   PAD_T + PLOT_H * 0.55,
        "end_graph":         PAD_T + PLOT_H * 0.60,   # Phase 45+
        "suspected_game_end":PAD_T + PLOT_H * 0.65,   # Phase 45+
        "last_kill_feed":    PAD_T + PLOT_H * 0.70,   # Phase 45+
    }
    # Phase 45+：game_end 相關的單一 marker 用大星號 + 垂直虛線標出
    SPECIAL_MARKERS = {"end_graph", "suspected_game_end", "last_kill_feed",
                       "nexus_explosion", "game_end_screen"}
    # 修 g5：先準備 hp_disappear / kill_times 排序好的 list 給 suspect 判斷用
    hp_times_sorted = sorted(store.hp_disappear_times)
    kill_times_sorted = sorted(store.kill_times)
    flash_times_sorted = sorted(store.flash_times) if hasattr(store, "flash_times") else []
    markers = []
    suspect_count = 0
    suspect_list: list[dict] = []   # 修 g5.1：收集所有 suspect 給頁面清單渲染
    for _, row in store.df.iterrows():
        type_ = row["type"]
        if type_ == "replay":
            continue
        x = x_of(row["time"])
        y = type_y_offset.get(type_, PAD_T + PLOT_H * 0.5)
        color = EVENT_COLORS.get(type_, "#777")
        meta = html.escape(str(row["meta"]) if row["meta"] else "")

        # 修 g5：可疑事件判斷
        suspect, reason = is_suspect_event(
            row, store, hp_times_sorted, kill_times_sorted, flash_times_sorted
        )
        if suspect:
            suspect_count += 1
            suspect_list.append({
                "time":   float(row["time"]),
                "type":   type_,
                "reason": reason,
                "meta":   str(row["meta"]) if row["meta"] else "",
            })

        tooltip = f"{mmss(row['time'])} {EVENT_LABEL.get(type_, type_)}"
        if meta:
            tooltip += f" [{meta}]"
        tooltip += f"  weight={row['weight']:.0f}"
        if suspect:
            tooltip += f"  ⚠ 可疑：{html.escape(reason)}"

        # 比較 game_end 用的特殊 markers：用大圓 + 垂直虛線從上到下標出
        if type_ in {"end_graph", "suspected_game_end", "last_kill_feed"}:
            markers.append(
                f'<line x1="{x:.1f}" y1="{PAD_T}" x2="{x:.1f}" y2="{PAD_T + PLOT_H}" '
                f'stroke="{color}" stroke-width="1.5" stroke-dasharray="4,3" opacity="0.6"/>'
            )
            markers.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{color}" '
                f'stroke="#000" stroke-width="1" opacity="0.95">'
                f'<title>{tooltip}</title></circle>'
            )
            markers.append(
                f'<text x="{x+8:.1f}" y="{y+4:.1f}" font-size="10" font-weight="bold" '
                f'fill="{color}">{EVENT_LABEL[type_]}</text>'
            )
        elif suspect:
            # 修 g5：可疑事件 — 加大圓 + 紅色 outline + ⚠ 標記
            markers.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}" '
                f'stroke="{SUSPECT_COLOR}" stroke-width="2" opacity="0.9">'
                f'<title>{tooltip}</title></circle>'
            )
            markers.append(
                f'<text x="{x:.1f}" y="{y-7:.1f}" text-anchor="middle" '
                f'font-size="11" font-weight="bold" fill="{SUSPECT_COLOR}">⚠</text>'
            )
        else:
            markers.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="{color}" opacity="0.85">'
                f'<title>{tooltip}</title></circle>'
            )

    # 3.5 Phase 49-3e v3 (5/17): 真實 timeline 事件 row（Leaguepedia/Bilibili）
    # 對應 in-game time 經 anchor 轉成 mp4 time，跟 YOLO 偵測同一時間軸
    official_markers: list[str] = []
    OFFICIAL_Y = PAD_T + PLOT_H * 0.42  # 介於 objective 0.40 跟 nexus 0.50 之間
    OFFICIAL_CATEGORY_COLOR = {
        "dragon":    "#ff6b9d",  # 粉紅
        "baron":     "#9b5de5",  # 紫
        "herald":    "#ff8500",  # 橘
        "voidgrub":  "#06d6a0",  # 綠
        "tower":     "#ffd166",  # 黃
        "inhibitor": "#e63946",  # 紅
        "special":   "#f15bb5",  # 桃紅（雙殺/三殺）
        "soul":      "#9b5de5",  # 紫（龍魂）
        "nexus":     "#000000",  # 黑（主堡爆）
        "start":     "#888888",  # 灰
        "obj":       "#9b5de5",  # 紫
    }
    if official_events:
        for oe in official_events:
            t = oe.get("time_sec", 0)
            if t < t0 or t > t1:
                continue
            x = x_of(t)
            cat = oe.get("category", "obj")
            label = oe.get("label", "?")
            team_id = oe.get("team_id", 0)
            color = OFFICIAL_CATEGORY_COLOR.get(cat, "#666")
            # team 標識：team_id=100 blue, 200 red, 其他 灰
            team_marker = "🔵" if team_id == 100 else ("🔴" if team_id == 200 else "")
            tooltip = f"{mmss(t)} [{cat}] {label}"
            if team_marker:
                tooltip = f"{team_marker} {tooltip}"
            # 用 diamond marker 跟 YOLO 圓點區分
            official_markers.append(
                f'<polygon points="{x:.1f},{OFFICIAL_Y-5:.1f} '
                f'{x+5:.1f},{OFFICIAL_Y:.1f} '
                f'{x:.1f},{OFFICIAL_Y+5:.1f} '
                f'{x-5:.1f},{OFFICIAL_Y:.1f}" '
                f'fill="{color}" stroke="#000" stroke-width="0.5" opacity="0.9">'
                f'<title>{html.escape(tooltip)}</title></polygon>'
            )
            # label text 緊鄰 marker 上方
            official_markers.append(
                f'<text x="{x:.1f}" y="{OFFICIAL_Y-8:.1f}" text-anchor="middle" '
                f'font-size="9" fill="{color}" font-weight="bold">{html.escape(label[:6])}</text>'
            )
        # row 標籤
        if official_source:
            official_markers.append(
                f'<text x="{PAD_L - 5}" y="{OFFICIAL_Y + 4:.1f}" text-anchor="end" '
                f'font-size="10" fill="#444" font-weight="bold">官方 timeline</text>'
            )
            official_markers.append(
                f'<text x="{PAD_L - 5}" y="{OFFICIAL_Y + 16:.1f}" text-anchor="end" '
                f'font-size="9" fill="#888">{html.escape(official_source)}</text>'
            )

    # 4. Clip 矩形
    clip_y      = PAD_T + PLOT_H + 10
    clip_height = 30
    clip_rects = []
    for i, c in enumerate(clips):
        x0 = x_of(c["start"])
        x1 = x_of(c["end"])
        ctype = c.get("type", "clip")
        # 配色：teamfight 紅、small 橘、solo 黃、objective 紫、其他灰
        if "objective" in ctype:
            color = "#9b5de5"
        elif "teamfight" in ctype:
            color = "#e63946"
        elif "small" in ctype:
            color = "#f4a261"
        elif "solo" in ctype:
            color = "#ffd166"
        elif "victory" in ctype or "game_end" in ctype:
            color = "#06d6a0"
        elif "kill_enforce" in ctype:
            color = "#aaaaaa"
        else:
            color = "#777"
        dur = c["end"] - c["start"]
        tooltip = (
            f"clip[{i+1}] {mmss(c['start'])}~{mmss(c['end'])} ({dur:.0f}s) "
            f"type={ctype} score={c.get('score',0)}"
        )
        clip_rects.append(
            f'<rect x="{x0:.1f}" y="{clip_y}" width="{max(1, x1-x0):.1f}" height="{clip_height}" '
            f'fill="{color}" opacity="0.7" stroke="#000" stroke-width="0.5">'
            f'<title>{tooltip}</title></rect>'
        )
        if x1 - x0 > 50:
            clip_rects.append(
                f'<text x="{(x0+x1)/2:.1f}" y="{clip_y + 18:.0f}" '
                f'text-anchor="middle" font-size="10" fill="#fff">{i+1}</text>'
            )

    # 5. 時間軸刻度（每分鐘）
    ticks = []
    for t_min in range(int(t0 // 60), int(t1 // 60) + 1):
        t = t_min * 60
        if t < t0 or t > t1:
            continue
        x = x_of(t)
        ticks.append(
            f'<line x1="{x:.1f}" y1="{PAD_T + PLOT_H}" x2="{x:.1f}" '
            f'y2="{PAD_T + PLOT_H + 5}" stroke="#888"/>'
            f'<text x="{x:.1f}" y="{PAD_T + PLOT_H + 18}" '
            f'text-anchor="middle" font-size="10" fill="#888">{mmss(t)}</text>'
        )

    # 6. Y 軸：分數刻度
    y_ticks = []
    for frac in [0.0, 0.25, 0.5, 0.75, 1.0]:
        y = PAD_T + PLOT_H - frac * PLOT_H
        s = score_max * frac
        y_ticks.append(
            f'<line x1="{PAD_L - 5}" y1="{y:.1f}" x2="{PAD_L}" y2="{y:.1f}" stroke="#888"/>'
            f'<text x="{PAD_L - 8}" y="{y + 3:.1f}" text-anchor="end" '
            f'font-size="10" fill="#888">{s:.0f}</text>'
        )

    # 7. 圖例（含 replay）
    legend_items = []
    for t_, color in EVENT_COLORS.items():
        if t_ == "replay":
            # replay 用色塊（背景區段樣式）而非圓點
            legend_items.append(
                f'<span style="display:inline-block; width:18px; height:10px; '
                f'background:{color}; opacity:0.4; border:1.5px dashed {color}; '
                f'margin:0 4px; vertical-align:middle;"></span>'
                f'<span style="margin-right:14px;">{EVENT_LABEL.get(t_, t_)} (區段)</span>'
            )
        else:
            legend_items.append(
                f'<span style="display:inline-block; width:10px; height:10px; '
                f'background:{color}; border-radius:50%; margin:0 4px;"></span>'
                f'<span style="margin-right:14px;">{EVENT_LABEL.get(t_, t_)}</span>'
            )
    legend_html = "".join(legend_items)

    # 8. Summary
    summary = store.summary()
    summary_str = " · ".join(f"{k}={v}" for k, v in sorted(summary.items()))

    # 統合 SVG
    # 修 g2：用 viewBox 讓 JS 可改 viewBox 達成 zoom；保留 width/height 為顯示尺寸
    svg = f"""
<svg id="timeline-svg" width="{SVG_W}" height="{SVG_H}" viewBox="0 0 {SVG_W} {SVG_H}" xmlns="http://www.w3.org/2000/svg" style="cursor: grab;">
  <!-- 修 g2: data-base-* 給 JS reset zoom 用 -->
  <defs><style>.no-zoom {{ vector-effect: non-scaling-stroke; }}</style></defs>
  <!-- 背景分數曲線（藍色填色） -->
  <polygon points="{score_area_pts}" fill="#4ea8de" opacity="0.25"/>
  <polyline points="{score_polyline_pts}" fill="none" stroke="#1d3557" stroke-width="1.0"/>

  <!-- Replay 背景條 -->
  {''.join(replay_rects)}

  <!-- 事件 markers -->
  {''.join(markers)}

  <!-- 官方 timeline events (Leaguepedia/Bilibili，5/17 加) -->
  {''.join(official_markers)}

  <!-- Clip 矩形 -->
  {''.join(clip_rects)}

  <!-- 時間軸刻度 -->
  <line x1="{PAD_L}" y1="{PAD_T + PLOT_H}" x2="{PAD_L + PLOT_W}" y2="{PAD_T + PLOT_H}" stroke="#888"/>
  {''.join(ticks)}

  <!-- Y 軸 -->
  <line x1="{PAD_L}" y1="{PAD_T}" x2="{PAD_L}" y2="{PAD_T + PLOT_H}" stroke="#888"/>
  {''.join(y_ticks)}

  <!-- 標題 -->
  <text x="{SVG_W/2}" y="30" text-anchor="middle" font-size="18" fill="#222">{html.escape(title)}</text>
  <text x="{PAD_L}" y="50" font-size="11" fill="#666">每秒加權分數曲線（背景） + 事件 markers + Clip 矩形（hover 看詳細）</text>

  <!-- Clip 軌道標籤 -->
  <text x="{PAD_L - 5}" y="{clip_y + 18}" text-anchor="end" font-size="10" fill="#666">Clips</text>
</svg>
"""

    # HTML 包裝
    page = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: 'Segoe UI', 'Microsoft JhengHei', sans-serif;
         background: #f8f9fa; color: #222; padding: 20px; }}
  .container {{ max-width: {SVG_W + 40}px; margin: 0 auto;
                background: white; padding: 20px; border-radius: 8px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  .summary {{ font-size: 13px; color: #555; margin: 8px 0; }}
  .legend {{ font-size: 12px; color: #555; padding: 8px 0; }}
  .clip-list {{ font-family: 'Consolas', monospace; font-size: 12px;
                background: #f1f3f5; padding: 12px; border-radius: 6px;
                margin-top: 12px; max-height: 280px; overflow-y: auto; }}
  .clip-list .row {{ padding: 2px 0; border-bottom: 1px solid #e9ecef; }}
  .suspect-list {{ font-family: 'Consolas', monospace; font-size: 12px;
                   background: #fff5f5; border: 1px solid #ffd6d6; padding: 12px;
                   border-radius: 6px; margin-top: 12px; max-height: 240px; overflow-y: auto; }}
  .suspect-list .row {{ padding: 3px 0; border-bottom: 1px solid #ffe5e5; }}
  .suspect-list .suspect-time {{ display: inline-block; min-width: 50px;
                                  color: #888; font-weight: bold; }}
  .suspect-list .suspect-type {{ display: inline-block; min-width: 80px;
                                  font-weight: bold; }}
  .suspect-list .suspect-reason {{ color: #ff1744; margin-left: 8px; }}
  svg {{ display: block; }}
</style>
</head>
<body>
<div class="container">
  <h2>{html.escape(title)}</h2>
  <div class="summary">
    遊戲時間：{mmss(t0)} ~ {mmss(t1)}（{duration:.0f}s）<br>
    事件統計：{html.escape(summary_str)}<br>
    最終 Clip：{len(clips)} 段，總長 {sum(c['end']-c['start'] for c in clips):.0f}s
  </div>
  <div class="legend">{legend_html}</div>
  <div style="font-size:11px; color:#888; margin: 4px 0;">
    🔍 <b>Ctrl+滾輪</b> = X 軸 zoom（以滑鼠位置為中心）　|　 拖曳 = 平移　|　 雙擊 = 重置
    {f'　|　 ⚠ {suspect_count} 個可疑事件已標紅（清單見下方）' if suspect_count else ''}
  </div>
  {svg}
"""

    # 修 g5.1：可疑事件清單（在 SVG 後 / Clip 列表前）
    if suspect_list:
        page += f"""
  <h3>⚠ 可疑事件清單（{len(suspect_list)} 個）</h3>
  <div class="suspect-list">
    <div style="font-size:11px; color:#666; margin-bottom:6px;">
      規則：(1) kill_feed/hp_disappear 在 game_end 之後 　(2) victory/nexus 距 game_end 超過 60s
      　(3) kill_feed 在 ±18s 無 hp_disappear 配對（孤立 kill，可能 minion/tower false positive）
    </div>
"""
        # 按時間排序
        for s in sorted(suspect_list, key=lambda s: s["time"]):
            tlabel = EVENT_LABEL.get(s["type"], s["type"])
            meta_str = f' [{html.escape(s["meta"])}]' if s["meta"] else ""
            page += (
                f'<div class="row">'
                f'<span class="suspect-time">{mmss(s["time"])}</span> '
                f'<span class="suspect-type" style="color:{EVENT_COLORS.get(s["type"],"#777")};">'
                f'{html.escape(tlabel)}</span>'
                f'{html.escape(meta_str)}'
                f' <span class="suspect-reason">{html.escape(s["reason"])}</span>'
                f'</div>'
            )
        page += "</div>\n"

    page += """
  <h3>Clip 列表</h3>
  <div class="clip-list">
"""
    for i, c in enumerate(clips):
        ctype = html.escape(c.get("type", "clip"))
        page += (
            f'<div class="row">[{i+1:2d}] {mmss(c["start"])}~{mmss(c["end"])} '
            f'({c["end"]-c["start"]:.0f}s)  type={ctype}  score={c.get("score",0)}</div>'
        )
    page += """
  </div>
</div>
<script>
// 修 g2：SVG 滾輪 zoom（X 軸方向，以滑鼠位置為中心）
(function() {
  const svg = document.getElementById('timeline-svg');
  if (!svg) return;
  const baseVB = svg.viewBox.baseVal;   // {x, y, width, height}
  const ORIG = { x: baseVB.x, y: baseVB.y, w: baseVB.width, h: baseVB.height };
  let isDragging = false;
  let dragStart = null;

  function applyVB(x, w) {
    // 限制：寬度不小於原寬 1/50，不大於原寬；x 不超出邊界
    w = Math.max(ORIG.w / 50, Math.min(ORIG.w, w));
    x = Math.max(ORIG.x, Math.min(ORIG.x + ORIG.w - w, x));
    svg.setAttribute('viewBox', x + ' ' + ORIG.y + ' ' + w + ' ' + ORIG.h);
  }

  svg.addEventListener('wheel', function(e) {
    // 修 g2.1：只在 Ctrl+滾輪時 zoom，普通滾輪保留瀏覽器頁面捲動
    if (!e.ctrlKey) return;
    e.preventDefault();
    const vb = svg.viewBox.baseVal;
    // 滑鼠在 SVG 內的座標（viewBox 座標系）
    const rect = svg.getBoundingClientRect();
    const mx = vb.x + (e.clientX - rect.left) / rect.width * vb.width;
    // Ctrl+滾輪向上 = zoom in（縮小 viewBox），向下 = zoom out
    const factor = e.deltaY < 0 ? 0.85 : 1.18;
    const newW = vb.width * factor;
    const newX = mx - (mx - vb.x) * (newW / vb.width);
    applyVB(newX, newW);
  }, { passive: false });

  // 拖曳平移
  svg.addEventListener('mousedown', function(e) {
    isDragging = true;
    svg.style.cursor = 'grabbing';
    const rect = svg.getBoundingClientRect();
    const vb = svg.viewBox.baseVal;
    dragStart = { x: e.clientX, vbX: vb.x, scale: vb.width / rect.width };
  });
  window.addEventListener('mousemove', function(e) {
    if (!isDragging) return;
    const dx = (e.clientX - dragStart.x) * dragStart.scale;
    applyVB(dragStart.vbX - dx, svg.viewBox.baseVal.width);
  });
  window.addEventListener('mouseup', function() {
    isDragging = false;
    svg.style.cursor = 'grab';
  });

  // 雙擊重置
  svg.addEventListener('dblclick', function() {
    svg.setAttribute('viewBox', ORIG.x + ' ' + ORIG.y + ' ' + ORIG.w + ' ' + ORIG.h);
  });
})();
</script>
</body>
</html>
"""
    return page


# ── 互動 Server (Phase 45f) ─────────────────────────────────────────────────

def _serve(scene_path: Path, port: int = 8766):
    """啟動 HTTP server，提供 slider 互動式調權重。

    GET  /            → 主頁（HTML + slider + JS）
    POST /api/score   → JSON {weights: {...}, window: 5.0}
                        回傳 {times: [...], scores: [...]}
    """
    import json as _json
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from socketserver import ThreadingMixIn
    import webbrowser

    print(f"讀取 scene.json：{scene_path.name}")
    store_base = EventStore.from_scene_json(scene_path)
    print(store_base)
    print(f"預跑 select_highlights 取 clips（normal mode）...")
    try:
        clips = get_clips_from_scene(scene_path, minimal=False)
        print(f"  → {len(clips)} 段 clips")
    except Exception as e:
        print(f"  ⚠ select_highlights 失敗：{e}")
        clips = []

    page = render_serve_html(store_base, clips, scene_path.stem)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404); self.end_headers()

        def do_POST(self):
            if self.path != "/api/score":
                self.send_response(404); self.end_headers(); return
            length = int(self.headers.get("Content-Length", 0))
            data = _json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            new_weights = data.get("weights", {})
            window      = float(data.get("window", 5.0))
            # 重建 EventStore 用新 weights
            new_store = EventStore.from_scene_json(scene_path, weights=new_weights)
            times, scores = new_store.per_second_score(window=window)
            body = _json.dumps({
                "times":  times.tolist(),
                "scores": scores.tolist(),
                "max":    float(scores.max()) if len(scores) else 0.0,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass   # 安靜

    class ThreadedServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedServer(("localhost", port), Handler)
    url = f"http://localhost:{port}"
    print(f"\n伺服器啟動：{url}")
    print(f"瀏覽器開啟，拖動 slider 即時調權重看分數曲線")
    print(f"Ctrl+C 結束")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n伺服器關閉")


def render_serve_html(store: EventStore, clips: list[dict], stem: str) -> str:
    """互動版 HTML：含 slider + JS 重畫曲線。"""
    static = render_html(store, clips, title=f"Timeline (interactive) — {stem}")

    # 加 slider + JS 到 body 開頭
    sliders_html = '<div class="sliders" style="margin: 16px 0; padding: 12px; '
    sliders_html += 'background: #fff3e0; border-radius: 6px; font-size: 13px;">'
    sliders_html += '<strong>權重 slider（拖動即時更新分數曲線）</strong><br><br>'
    for k, default in DEFAULT_WEIGHTS.items():
        sliders_html += (
            f'<label style="display:inline-block; min-width:130px;">{k}</label>'
            f'<input type="range" id="w_{k}" min="-200" max="500" value="{default}" '
            f'step="10" style="width:240px;" oninput="onWeightChange()">'
            f'<span id="v_{k}" style="display:inline-block; min-width:50px;">{default:.0f}</span>'
            f'<br>'
        )
    sliders_html += (
        '<label style="display:inline-block; min-width:130px;">window (sec)</label>'
        '<input type="range" id="w_window" min="1" max="20" value="5" step="1" '
        'style="width:240px;" oninput="onWeightChange()">'
        '<span id="v_window" style="display:inline-block; min-width:50px;">5</span>'
    )
    sliders_html += '</div>'

    js = f"""
<script>
const WEIGHT_KEYS = {list(DEFAULT_WEIGHTS.keys())};
let debounceTimer = null;

function onWeightChange() {{
  for (const k of WEIGHT_KEYS) {{
    document.getElementById('v_' + k).textContent =
      document.getElementById('w_' + k).value;
  }}
  document.getElementById('v_window').textContent =
    document.getElementById('w_window').value;
  // debounce
  if (debounceTimer) clearTimeout(debounceTimer);
  debounceTimer = setTimeout(recompute, 200);
}}

async function recompute() {{
  const weights = {{}};
  for (const k of WEIGHT_KEYS) {{
    weights[k] = parseFloat(document.getElementById('w_' + k).value);
  }}
  const window_ = parseFloat(document.getElementById('w_window').value);
  try {{
    const r = await fetch('/api/score', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{weights, window: window_}}),
    }});
    const data = await r.json();
    redrawScore(data.times, data.scores, data.max);
  }} catch (e) {{ console.error(e); }}
}}

function redrawScore(times, scores, scoreMax) {{
  const svg = document.querySelector('svg');
  if (!svg || !times.length) return;

  // SVG 座標常數（跟 render_html 對齊）
  const PAD_L = 60, PAD_R = 30, PAD_T = 60, PAD_B = 80;
  const W = parseFloat(svg.getAttribute('width'));
  const H = parseFloat(svg.getAttribute('height'));
  const PLOT_W = W - PAD_L - PAD_R;
  const PLOT_H = H - PAD_T - PAD_B;

  const t0 = times[0], t1 = times[times.length-1];
  const duration = Math.max(1, t1 - t0);
  const sMax = Math.max(scoreMax, 1);

  const xOf = t => PAD_L + (t - t0) / duration * PLOT_W;
  const yOf = s => PAD_T + PLOT_H - (s / sMax) * PLOT_H;

  let polyPts = [];
  for (let i = 0; i < times.length; i++) {{
    polyPts.push(xOf(times[i]).toFixed(1) + ',' + yOf(scores[i]).toFixed(1));
  }}
  const polyStr = polyPts.join(' ');
  const areaStr = xOf(t0).toFixed(1) + ',' + (PAD_T + PLOT_H).toFixed(1) + ' ' +
                  polyStr + ' ' +
                  xOf(t1).toFixed(1) + ',' + (PAD_T + PLOT_H).toFixed(1);

  // 找到第一個 polygon 跟 polyline (背景跟線)，更新 points
  const polygons = svg.querySelectorAll('polygon');
  if (polygons.length > 0) polygons[0].setAttribute('points', areaStr);
  const polylines = svg.querySelectorAll('polyline');
  if (polylines.length > 0) polylines[0].setAttribute('points', polyStr);
}}
</script>
"""

    # 把 sliders + JS 插到 <h2> 後面
    static = static.replace('<h2>', sliders_html + js + '<h2>', 1)
    return static


# ── CLI ─────────────────────────────────────────────────────────────────────

# 讀 highlight.utils.paths，支援跨機器 env override
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from highlight.utils import paths as _paths

SCENE_SEARCH_DIRS = [
    _paths.output_dir(),
    _paths.scan_dir(),
]


def find_latest_scene_json() -> Path | None:
    """修 g3：掃 SCENE_SEARCH_DIRS 找最新修改的 *_scene.json。"""
    candidates: list[Path] = []
    for d in SCENE_SEARCH_DIRS:
        if d.exists():
            candidates.extend(d.glob("*_scene.json"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def main():
    # Windows cp950 console 不支援部分 emoji/Unicode，強制 UTF-8 stdout
    # 放在 main() 內避免 import 時改 sys.stdout 跟 caller 衝突
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

    parser = argparse.ArgumentParser(description="時間軸視覺化（HTML+SVG）")
    parser.add_argument(
        "scene_json", nargs="?",
        help=f"scene.json 路徑（省略=自動找 {_paths.output_dir()} 跟 {_paths.scan_dir()} 中最新）",
    )
    parser.add_argument(
        "--output", "-o",
        default=str(Path(__file__).parent / "timeline_viewer.html"),
        help="輸出 HTML 路徑（預設：dashboard/timeline_viewer.html）",
    )
    parser.add_argument(
        "--minimal", action="store_true",
        help="select_highlights 用 minimal mode（debug，跳過所有後端篩選器）",
    )
    parser.add_argument(
        "--no-clips", action="store_true",
        help="不跑 select_highlights，只畫事件 markers 與分數曲線",
    )
    parser.add_argument(
        "--serve", action="store_true",
        help="啟動 HTTP server (port 8766) 提供 slider 互動式調權重",
    )
    parser.add_argument("--port", type=int, default=8766, help="server port (預設 8766)")
    args = parser.parse_args()

    if args.scene_json:
        scene_path = Path(args.scene_json)
    else:
        scene_path = find_latest_scene_json()
        if scene_path is None:
            print(f"未指定 scene.json 且 {_paths.output_dir()} 跟 {_paths.scan_dir()} 都找不到 *_scene.json", file=sys.stderr)
            sys.exit(1)
        print(f"[auto] 自動選用最新 scene.json：{scene_path}")
    if not scene_path.exists():
        print(f"找不到 scene.json：{scene_path}", file=sys.stderr)
        sys.exit(1)

    if args.serve:
        _serve(scene_path, port=args.port)
        return

    print(f"讀取 scene.json：{scene_path.name}")
    store = EventStore.from_scene_json(scene_path)
    print(store)

    clips: list[dict] = []
    if not args.no_clips:
        print(f"\n呼叫 select_highlights（minimal={args.minimal}）...")
        try:
            clips = get_clips_from_scene(scene_path, minimal=args.minimal)
            print(f"  → {len(clips)} 段 clips")
        except Exception as e:
            print(f"  ⚠ select_highlights 失敗：{e}（只畫事件，不畫 clip）")

    title = f"Timeline — {scene_path.stem}"
    if args.minimal:
        title += "  (MINIMAL MODE)"

    print(f"\n渲染 HTML...")
    scene_data = json.loads(scene_path.read_text(encoding="utf-8"))
    end_graph_time = scene_data.get("end_graph_time")

    # 5/17 v3：載入對應的真實 timeline（Leaguepedia / Bilibili）
    official_events, official_source = load_official_events_for_scene(scene_path)
    if official_events:
        print(f"[official] 載入 {len(official_events)} 個官方事件 (source={official_source})")
    else:
        print(f"[official] 沒對應 timeline（game 沒對齊 DB / Cargo 未 fetch）")

    page = render_html(
        store, clips, title=title, end_graph_time=end_graph_time,
        official_events=official_events,
        official_source=official_source,
    )

    out_path = Path(args.output)
    out_path.write_text(page, encoding="utf-8")
    print(f"輸出：{out_path.absolute()}")
    print(f"瀏覽器打開即可查看")


if __name__ == "__main__":
    main()

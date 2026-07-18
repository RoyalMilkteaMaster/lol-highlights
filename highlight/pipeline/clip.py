"""精華剪輯主流程：scene.json + 視覺事件 → 最終 clip 列表。

主要邏輯：
  - 戰鬥聚類選段（detect_battles → 取代滑動窗口評分）
  - Two-Pass Replay 篩選（Objective_Replay 優先，總長不足時補 Standard_Replay）
  - 8 條鐵律強制：R1 BP / R2 擊殺完整 / R3 fadeblack 轉場 / R4 走路限制
                   R5 物件保護 / R6 先 Live 再 Replay / R7 主堡爆炸結尾 / R8 觀眾反應(待)
  - Trim 收尾：擊殺後 1.5~4s 內找 pause 切（避免賽評被腰斬）
  - 結尾保護：victory / nexus / end_graph 段（PROTECTED_LABELS 豁免截斷）

CLI：
  python pipeline/clip.py --video <vod.mp4> --scene <scene.json> --output <out.mp4>
"""

import os
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import json
import logging
import sys
from pathlib import Path

# 讓 `from highlight.xxx import` 找得到（spawn 子程序時 sys.path 不會繼承）。
# Path(__file__).parents[2] = lol-highlights/（highlight/ 的 parent）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml

from highlight.selection.clip_filter import (
    GAME_END_TRAIL_SEC, PROTECTED_LABELS,
    dedup_clips,
    apply_objective_filter,
    trim_kill_tails,
    apply_victory_protection,
    enforce_kill_coverage, enforce_game_end_coverage,
    enforce_end_graph_coverage,   # Phase 45+ 修 G：結算圖表前 5s 切過去
    enforce_continuous_last_kill_to_nexus,  # 5/9 R7-strict 鐵則
    NexusCoverageError,                      # 5/9 R7-strict fail-fast
    apply_scene_change_boundaries,
    enforce_max_lead_in,
    cross_validate_kills,
    detect_battles,          # Phase 44：戰鬥聚類（取代滑動窗口評分）
    remove_replay_regions,   # Phase 44：砍掉 clip 中跟 replay 重疊的部分
)
from highlight.selection.bp_analyzer import (
    compute_bp_rms,
    analyze_bp_clip_window,
    BPNotFoundError,
)

# Phase 49-3e（fail-fast 鐵則 R7）：游戲結尾偵測失敗 raise
class EndNotFoundError(Exception):
    """end_graph / nexus_explosion / game_end_screen 三類訊號都沒偵測到。

    clip_worker 看到 main.py exit code 3 = EndNotFoundError。
    寧可漏一支 highlight，也不要剪到結尾全是 replay / 沒主堡爆炸的垃圾片。
    """


# config.yaml 絕對路徑
_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

# 所有 F:/lol-highlights/output 路徑讀 highlight.utils.paths，支援 OUTPUT_DIR env override
from highlight.utils import paths as _paths
from highlight.utils.vod_metadata import (
    lookup_game_timeline_by_path,
    update_game_timeline_anchor,
)

LOG_FILE = _paths.cut_progress_log()
# main.py 也會寫到同一個檔（先寫 split/scan 階段），所以這裡用 append 不覆寫

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(str(LOG_FILE), mode="a", encoding="utf-8")
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)-7s  %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        logging.getLogger().addHandler(handler)
    except Exception:
        logger.exception("[WARN] 無法建立 progress log: %s", LOG_FILE)

SCENE_JSON    = _paths.output_dir() / "scene_events.json"
SOURCE_VOD    = _paths.raw_dir() / "bilibili_test" / "lpl_vod.mp4"
OUTPUT_DIR    = _paths.final_dir()
TEMPLATES_DIR = Path(__file__).parent.parent / "assets/templates"

# ── BP（R1）───────────────────────────────────────────────────────────────────
# Emergency fallback only — 正常情況應由 scan_video.py 的 bp_ui YOLO 填入 scene.json
# 若 scene.json 沒有 bp_end（偵測失敗），才會用這個值；屆時會印 WARNING。
# 此常數值（819s）是 2026-04 某個測試影片的 BP_END，不代表任何通用值。
_BP_END_EMERGENCY_FALLBACK = 13 * 60 + 39

# ── 解說密度參數 ──────────────────────────────────────────────────────────────
WINDOW_SEC  = 90
STEP_SEC    = 15
MIN_GAP_SEC = 90    # 最小間距 = WINDOW_SEC，不會真重疊但容許連續會戰都入選
NUM_CLIPS   = 20    # Phase 29：15 → 20

# ── 事件權重（Phase 29：鐵則升級）─────────────────────────────────────────────
# 設計：擊殺、結束 = 絕對鐵則（分數遠高於其他），其他為輔助分
KILL_SCORE_VISUAL     = 1000   # 每個擊殺的視覺加分（原 80）
GAME_END_SCORE        = 2000   # 遊戲結束段的分數（victory / game_end）
KILL_GUARANTEE_SCORE  = 1000   # kill guarantee 合成窗口分數（原 90）
FLASH_SCORE_VISUAL    = 30     # 每個閃現使用加分（原 50，降低避免純 flash 段被撐起）
OBJECTIVE_SCORE_VISUAL = 60    # 每個 objective 事件加分

# ── 視覺事件 Combo 加分 ──────────────────────────────────────────────────────
COMBO_WINDOW_SEC = 5.0    # 閃現後多少秒內有擊殺才算連擊
COMBO_BONUS      = 100    # 每個連擊加分

# ── 剪輯精度參數 ──────────────────────────────────────────────────────────────
CLIP_LEAD_IN = 45   # R6：Objective Replay 前至少 45s 直播

# ─────────────────────────────────────────────────────────────────────────────
# （過濾邏輯已移至 modules/clip_filter.py，BP 分析已移至 modules/bp_analyzer.py）
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# Phase D (5/17)：timeline 過濾 segments 走路/replay 誤判 + game 結束後 segments
# ─────────────────────────────────────────────────────────────────────────────

def _filter_segments_with_timeline(segments_out, source_vod: Path):
    """用 broadcast_games.timeline_* 對 segments 反向驗證並過濾掉無效片段。

    過濾規則（quality from highlight.selection.timeline_validator）：
      - 'valid'        : 含 timeline 事件 → 保留
      - 'early_game'   : game 開頭 5 min 內無事件（走位正常）→ 保留
      - 'before_game'  : segment 在 anchor 之前（BP/載入）→ 保留（BP 預錄段可能要）
      - 'suspicious'   : game 中段無 timeline 事件 → **拿掉**（走路 / replay 誤判）
      - 'after_game'   : segment 在 GAME_END 之後 → **拿掉**（避免冗長 outro / 訪談）

    沒對到 DB game（譬如手動 --vod 路徑）or timeline 未對齊 → 跳過，原樣返回。
    """
    try:
        from highlight.selection.timeline_validator import load_timeline_events, validate_segments
    except Exception as e:
        logger.warning("[timeline_filter] import 失敗 %s — 跳過 timeline 過濾", e)
        return segments_out

    # 1. 從 source_vod path 反查 broadcast_games
    try:
        row = lookup_game_timeline_by_path(source_vod)
    except Exception as e:
        logger.warning("[timeline_filter] DB 查詢失敗 %s — 跳過", e)
        return segments_out
    if not row:
        logger.info("[timeline_filter] source_vod 不在 broadcast_games（手動路徑？）-> 跳過")
        return segments_out

    anchor = row.get("timeline_anchor_sec")
    source = row.get("timeline_source")
    external_id = row.get("timeline_external_id")
    duration = row.get("timeline_game_duration_sec")
    if not anchor or source in (None, "none") or not external_id:
        logger.info(
            "[timeline_filter] g%s timeline 未對齊（source=%s）-> 跳過",
            row.get("game_id"), source,
        )
        return segments_out

    # 2. 找 timeline JSON
    bd = row["broadcast_date"]
    timelines_dir = _paths.timelines_dir() / bd.strftime("%Y-%m-%d")
    import re as _re
    safe_id = _re.sub(r"[^A-Za-z0-9._-]", "_", external_id)
    tl_path = timelines_dir / f"{source}_{safe_id}.json"
    if not tl_path.is_file():
        logger.info("[timeline_filter] timeline JSON 不存在: %s -> 跳過", tl_path)
        return segments_out

    # 3. 跑 validator
    events = load_timeline_events(tl_path, source)
    segments_dicts = [{"start": s.start, "end": s.end} for s in segments_out]
    results = validate_segments(
        segments=segments_dicts,
        events=events,
        anchor_sec=anchor,
        game_duration_sec=duration,
    )

    # 4. 過濾並記 log
    # 5/17 user 提醒：LPL Bilibili view_points ~15 events / game 太稀疏，
    # 對「無事件就 suspicious」會誤殺正常走位 segments。
    # 過濾規則：events 稀疏（< 50）→ 只過濾 after_game；events 密集 → 加上 suspicious。
    # 5/18 Bug：不要假設 bilibili == LPL（未來 LCK 也可能轉 bilibili）。
    # 改用 events 密度判斷 — 比 source 判斷穩。
    sparse_threshold = 50
    is_sparse = len(events) < sparse_threshold
    filter_qualities = ("after_game",) if is_sparse else ("suspicious", "after_game")

    kept: list = []
    removed: list = []
    for seg, vr in zip(segments_out, results):
        # 標 quality 進 labels（讓後續 debug 可看）
        seg.labels = (seg.labels or []) + [f"timeline:{vr.quality}"]
        if vr.quality in filter_qualities:
            removed.append((seg, vr))
        else:
            kept.append(seg)

    print(f"\n[timeline filter] g{row['game_id']} {source}/{external_id}")
    print(f"  anchor={anchor:.0f}s game_duration={(duration or 0):.0f}s")
    print(f"  原 {len(segments_out)} 段 -> 保留 {len(kept)} 段，過濾 {len(removed)} 段")
    if removed:
        print(f"  過濾掉的 segments:")
        for seg, vr in removed:
            mm1, ss1 = divmod(int(seg.start), 60)
            mm2, ss2 = divmod(int(seg.end), 60)
            # 5/17 fix：Windows cp950 console 不支援 emoji（❌/✓）→ 用 ASCII
            print(f"    [X][{vr.quality:11}] {mm1:02d}:{ss1:02d}~{mm2:02d}:{ss2:02d} "
                  f"(in-game {vr.in_game_start:.0f}~{vr.in_game_end:.0f}s) - {vr.note}")
    return kept


def _compute_kill_correlation_anchor_for_game(
    yolo_kill_feed_times: list[float],
    source_vod: Path,
) -> float | None:
    """Phase E2 (5/17 user 確認 4 連續)：用 YOLO kill_feed × Leaguepedia CHAMPION_KILL
    cross-correlation 算 anchor，寫回 broadcast_games.timeline_anchor_sec。

    取代之前 flash icon anchor（偏移 30-40s 不準）。
    對位失敗（< 4 連續 match）→ 清 DB anchor + verified=0，augment/filter 跳過。
    LPL（source != leaguepedia）→ 完全不動。
    """
    try:
        from highlight.selection.timeline_validator import (
            find_anchor_via_kill_correlation, load_timeline_events,
        )
    except Exception as e:
        logger.warning("[kill_correlation] import 失敗 %s — skip", e)
        return None

    if not yolo_kill_feed_times or len(yolo_kill_feed_times) < 4:
        return None

    # 撈 game DB info
    try:
        row = lookup_game_timeline_by_path(source_vod)
    except Exception as e:
        logger.warning("[kill_correlation] DB 查失敗 %s — skip", e)
        return None
    if not row:
        return None
    source = row.get("timeline_source")
    external_id = row.get("timeline_external_id")
    if source != "leaguepedia" or not external_id:
        # LPL / 未對齊 → 不動（沒 CHAMPION_KILL 級別資料可對位）
        return None

    # 載入 timeline + 抽 CHAMPION_KILL
    bd = row["broadcast_date"]
    timelines_dir = _paths.timelines_dir() / bd.strftime("%Y-%m-%d")
    import re as _re
    safe_id = _re.sub(r"[^A-Za-z0-9._-]", "_", external_id)
    tl_path = timelines_dir / f"{source}_{safe_id}.json"
    if not tl_path.is_file():
        logger.warning("[kill_correlation] timeline JSON 不存在: %s", tl_path)
        return None
    events = load_timeline_events(tl_path, source)
    tl_kills_ingame = [e.in_game_time_sec for e in events if e.event_type == "CHAMPION_KILL"]
    if len(tl_kills_ingame) < 4:
        logger.info("[kill_correlation] timeline CHAMPION_KILL < 4 個 -> skip")
        return None

    # Cross-correlation
    anchor, max_run = find_anchor_via_kill_correlation(
        yolo_kill_feed_times, tl_kills_ingame,
        tolerance_sec=2.0,
        consecutive_required=4,
    )

    if anchor is None:
        logger.warning(
            "[kill_correlation] g%s 4 連續 match 找不到 (YOLO=%d, timeline=%d) "
            "-> 清 DB anchor + 跳過 augment/filter",
            row["game_id"], len(yolo_kill_feed_times), len(tl_kills_ingame),
        )
        # 清 anchor + 標 unverified（避免 augment/filter 用舊壞 anchor）
        try:
            update_game_timeline_anchor(row["game_id"], None, verified=False)
        except Exception:
            pass
        return None

    # 寫回 DB
    try:
        update_game_timeline_anchor(row["game_id"], anchor, verified=True)
    except Exception as e:
        logger.warning("[kill_correlation] DB UPDATE 失敗 %s", e)

    old_anchor = row.get("timeline_anchor_sec")
    print(f"\n[kill_correlation] g{row['game_id']} [OK] anchor={anchor:.1f}s "
          f"(max_run={max_run} 連續 match, tolerance=+-2s)")
    if old_anchor:
        print(f"  舊 flash anchor={old_anchor:.1f}s -> diff {anchor - old_anchor:+.1f}s")
    return anchor


def _augment_kill_feed_with_timeline(
    kill_feed_times: list[float],
    source_vod: Path,
) -> list[float]:
    """Phase E (5/17)：LCK/LCP 用 Leaguepedia V5 timeline CHAMPION_KILL 補強 YOLO kill_feed。

    補強模式（user 強調 5/17 晚）：
      - 只對 LCK/LCP 加（timeline_source='leaguepedia'）
      - LPL（bilibili view_points）或沒對到 DB → 原樣返回不動，避免破壞既有 highlight 流程
      - timeline kills 跟 YOLO kills 用 ±2s 去重 union

    為什麼補強而不取代：
      - YOLO 偵 kill_feed UI 顯示時間，timeline 偵 in-game kill 事件，兩者 timestamp 略有差異
      - YOLO 可能多殺 spree 漏抓（同 kill_feed 卡顯示），timeline 每殺都有
      - 但 anchor 估有 ±5-10s 誤差，timeline-derived mp4 time 可能比 YOLO 偏 5s 左右
      - union 後 + select_highlights 內部 cluster 去重，最終 cluster 內保留最早時刻
    """
    try:
        from highlight.selection.timeline_validator import load_timeline_events
    except Exception:
        return kill_feed_times

    try:
        row = lookup_game_timeline_by_path(source_vod)
    except Exception as e:
        logger.warning("[timeline_augment] DB 查失敗 %s — 跳過", e)
        return kill_feed_times

    # 沒對到 DB（手動 --vod 路徑）→ 跳過
    if not row:
        return kill_feed_times
    anchor = row.get("timeline_anchor_sec")
    source = row.get("timeline_source")
    external_id = row.get("timeline_external_id")
    if not anchor or source != "leaguepedia" or not external_id:
        # LPL (bilibili) / 未對齊 → 不動
        return kill_feed_times

    # 載入 timeline JSON
    bd = row["broadcast_date"]
    timelines_dir = _paths.timelines_dir() / bd.strftime("%Y-%m-%d")
    import re as _re
    safe_id = _re.sub(r"[^A-Za-z0-9._-]", "_", external_id)
    tl_path = timelines_dir / f"{source}_{safe_id}.json"
    if not tl_path.is_file():
        return kill_feed_times

    events = load_timeline_events(tl_path, source)
    # 只取 CHAMPION_KILL events
    tl_kills_mp4 = [
        anchor + ev.in_game_time_sec
        for ev in events
        if ev.event_type == "CHAMPION_KILL"
    ]
    if not tl_kills_mp4:
        return kill_feed_times

    # Union + dedupe ±2s
    DEDUPE_GAP = 2.0
    yolo_set = list(kill_feed_times or [])
    added_tl: list[float] = []
    for tlk in tl_kills_mp4:
        # 看 YOLO 內有沒有近的（±2s）
        nearest = min((abs(tlk - y) for y in yolo_set), default=float("inf"))
        if nearest > DEDUPE_GAP:
            added_tl.append(tlk)
    merged = sorted(yolo_set + added_tl)

    logger.info(
        "[timeline_augment] g%s LCK/LCP timeline 補強 kill_feed: "
        "YOLO %d + timeline 新增 %d (timeline 共 %d kills, 已在 YOLO 內 %d) -> 合計 %d",
        row["game_id"], len(yolo_set), len(added_tl),
        len(tl_kills_mp4), len(tl_kills_mp4) - len(added_tl), len(merged),
    )
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# 主要選段函式
# ─────────────────────────────────────────────────────────────────────────────

def select_highlights(
    scene_json: Path,
    kill_feed_times: list[float] | None = None,
    victory_times: list[float] | None = None,
    flash_times: list[float] | None = None,
    bp_end: float | None = None,
    game_end_override: float | None = None,
    hp_disappear_times: list[float] | None = None,
    minimal: bool = False,
) -> list[dict]:
    with open(scene_json, encoding="utf-8") as f:
        scene = json.load(f)

    replay_segs    = scene["replay_segments"]
    nexus_times    = scene.get("nexus_explosion_times", []) or []   # 修 O：給 victory_protection 校正 trail（單獨 nexus 用）

    # 5/15：R7-strict 改用 end_game_signals = nexus_explosion ∪ game_end_screen（user 指示，兩 class 等價）
    _game_end_screen_times = scene.get("game_end_screen_times", []) or []
    end_game_signals = sorted({float(t) for t in (nexus_times + _game_end_screen_times)})

    game_end       = scene["game_end_time"] if game_end_override is None else game_end_override

    # Fallback：nexus/game_end_screen 兩個 class 都沒偵測到，但 game_end 已由
    # suspected_game_end（tower 密集視窗）估算出來 → 用 game_end 當錨點讓 R7-strict 通過。
    # 以 WARNING 標記，clip 結尾可能略早於真實 nexus，需人工複查。
    if not end_game_signals and game_end is not None:
        end_game_signals = [float(game_end)]
        logger.warning(
            f"[R7-strict] end_game_signals 空（nexus+game_end_screen 皆未偵測）-> "
            f"fallback: 用 game_end_time={game_end:.0f}s 當結尾錨點（suspected_game_end 估算，請複查結尾）"
        )
    obj_events     = scene.get("objective_events", [])
    scene_changes  = scene.get("scene_changes", []) or []
    end_graph_first_seen = scene.get("end_graph_time")   # Phase 45+ 修 G：直接從 scene.json 讀

    # 修 U：duration 不再用「game_end + 60」這種 LCK Carry 短片的便宜假設。
    # end_graph 比 game_end 晚 5~10 分鐘是 LoL 完整轉播的標準流程，原本的 cap 把它排除是 bug。
    # 改成只服務 enforce_end_graph_coverage 的 target_end 上限（用 end_graph + 30s 自然涵蓋）。
    # 整段 clip end 安全防護用 MAX_SINGLE_CLIP_SEC（已存在）守住。
    duration = None   # 不再人為設上限，下游自己處理
    if bp_end is None:
        logger.warning(
            f"[WARN] select_highlights 收到 bp_end=None，使用 emergency fallback "
            f"{_BP_END_EMERGENCY_FALLBACK}s — 這個值跟當前影片無關，可能導致選段不正確！"
        )
    game_start_sec = float(bp_end if bp_end is not None else _BP_END_EMERGENCY_FALLBACK)

    # ── Phase 45+ 修 H：game_end 後的 kill_feed 視為誤判直接無視 ─────────────
    # 遊戲已結束（nexus 爆炸 / victory 畫面後），不可能還有真擊殺。
    # 通常是 victory 動畫期間 YOLO 把畫面元素誤判為 kill_feed。
    # 砍掉這些誤判，避免 enforce_kill_coverage 補出無效段擾亂結尾。
    if kill_feed_times and game_end:
        _orig = len(kill_feed_times)
        kill_feed_times = [k for k in kill_feed_times if k <= game_end]
        _filtered = _orig - len(kill_feed_times)
        if _filtered > 0:
            logger.info(
                f"  [game_end 後過濾] 砍掉 {_filtered} 個 game_end @{game_end:.0f}s 之後的 kill_feed (誤判)"
            )
    if hp_disappear_times and game_end:
        hp_disappear_times = [h for h in hp_disappear_times if h <= game_end]

    # ── Phase 41 鐵則：kill_feed × hp_disappear 交叉驗證 ─────────────────────
    # 一個 kill_feed 在 ±18s 內若有 hp_disappear → verified（真擊殺）
    # 否則 unverified（可能 minion/tower kill_feed false positive）
    # 後續所有「kill 相關判斷」（density 過濾、加分、kill_guarantee、enforce_kill_coverage）
    # 全部用 verified_kill_times 為準。
    # Phase 42：12s → 18s，hp_disappear 偵測延遲（倒地動畫 1~3s + stride=0.8s + 漏偵容忍）
    # 原本 12s 容易把真擊殺誤判成 unverified 導致漏段。
    # Phase 45+ 修 A：smart cross-validate（取代舊的「全 pool 過濾」邏輯）
    # 舊邏輯把所有沒 hp_disappear 配對的 kill 都丟掉，會誤殺像 herald 多殺
    # 但 hp_disappear 模型沒抓到的真戰鬥。
    # 新邏輯：只對「孤立 1 kill」做 hp 驗證，多殺 cluster 全部信任保留。
    if kill_feed_times:
        from highlight.selection.clip_filter import filter_isolated_unverified_kills
        kill_feed_times = filter_isolated_unverified_kills(
            kill_feed_times,
            hp_disappear_times or [],
            flash_times=flash_times or [],
            cluster_gap=15.0,
            hp_window=18.0,
            flash_window=18.0,
        )

    # ── Phase 44：事件聚類選段（取代舊的滑動窗口評分）─────────────────────
    # 舊作法（L161~L246 滑動窗口 + Top-N + split_into_active_clips）的問題：
    #   - 兩場相距 60s 的戰鬥被評為同一高分區 → dedup 合併 → 硬截丟尾
    #   - 產生 middle game 黑洞（中期 13 分鐘內容消失）
    # 新作法：把 kill + hp_disappear + 戰鬥用閃現聚類，每場戰鬥 = 一個 clip。
    all_sub_clips: list[dict] = detect_battles(
        kill_times=kill_feed_times or [],
        hp_disappear_times=hp_disappear_times or [],
        flash_times=flash_times or [],
        objective_events=obj_events,
        game_start=game_start_sec,
        game_end=game_end,
    )
    _n_solo  = sum(1 for b in all_sub_clips if "solo_kill"      in b["type"])
    _n_small = sum(1 for b in all_sub_clips if "small_skirmish" in b["type"])
    _n_team  = sum(1 for b in all_sub_clips if "teamfight"      in b["type"])
    _n_obj   = sum(1 for b in all_sub_clips if "objective"      in b["type"])
    logger.info(
        f"[battle_cluster] {len(all_sub_clips)} 場戰鬥 "
        f"(solo={_n_solo}, small={_n_small}, team={_n_team}, obj={_n_obj})"
    )

    if minimal:
        logger.info("[minimal mode] 跳過 apply_objective_filter / R5_E4 延伸")
    else:
        # ── V3：物件戰鬥過濾器 ────────────────────────────────────────────────────
        all_sub_clips = apply_objective_filter(
            all_sub_clips,
            obj_events,
            kill_feed_times or [],
            flash_times or [],
            game_start_sec,
        )

        # ── R5/E4：高優先物件補充延伸 ────────────────────────────────────────────
        HIGH_PRIORITY = {"baron", "elder_dragon", "elder"}
        for ev in obj_events:
            obj_t    = ev["time"]
            obj_type = ev.get("type", "").lower()
            if not any(k in obj_type for k in HIGH_PRIORITY):
                continue
            for clip in all_sub_clips:
                if clip["end"] - 150 <= obj_t <= clip["end"] + 30:
                    new_end = obj_t + 90.0
                    if new_end > clip["end"]:
                        clip["end"] = new_end
                    break

    # ── Phase 44：直接砍掉每個 clip 中跟 replay 重疊的部分（minimal mode 也保留）
    # 副作用：mega-battle 中間若有 replay 會被自然切成兩段，解決後期連續 6 分鐘
    # 大團戰被塞進同一個 battle cluster 的問題。
    all_sub_clips = remove_replay_regions(all_sub_clips, replay_segs)
    logger.info(f"[remove_replay_regions] 挖掉 replay 後 -> {len(all_sub_clips)} 段")

    _battle_times = sorted((kill_feed_times or []) + (flash_times or []))
    all_sub_clips = dedup_clips(all_sub_clips, battle_times=_battle_times)

    if minimal:
        logger.info(
            "[minimal mode] 跳過 trim_kill_tails / apply_victory_protection / "
            "enforce_kill_coverage / apply_scene_change_boundaries"
        )
    else:
        # ── V3：擊殺後收尾 ────────────────────────────────────────────────────────
        all_sub_clips = trim_kill_tails(
            all_sub_clips,
            kill_feed_times or [],
            flash_times or [],
            obj_events,
            hp_disappear_times=hp_disappear_times,   # Phase 40：hp_disappear 當主節點
        )

        all_sub_clips = dedup_clips(all_sub_clips, battle_times=_battle_times)

        # ── R7 視覺層：victory 結尾保護（修 O：用 nexus 校正 trail）─────────────
        if victory_times:
            all_sub_clips = apply_victory_protection(
                all_sub_clips, victory_times, game_end,
                nexus_times=nexus_times,
                score=GAME_END_SCORE,
            )

        # ── 擊殺鐵則：每個 kill 都必須被涵蓋 ─────────────────────────────────────
        # Phase 35：傳入 hp_disappear_times，clip 結尾優先採用「英雄倒地」節點
        if kill_feed_times:
            all_sub_clips = enforce_kill_coverage(
                all_sub_clips, kill_feed_times, game_start_sec,
                score=KILL_GUARANTEE_SCORE,
                hp_disappear_times=hp_disappear_times,
            )
            # Phase 41：對新補入的 kill_enforce 段再跑一次滾動延伸，讓團戰連續 kill 合併成大段
            # （避免 29 段裡大部分都是 8~30s kill_enforce 小段）
            all_sub_clips = trim_kill_tails(
                all_sub_clips,
                kill_feed_times or [],
                flash_times or [],
                obj_events,
                hp_disappear_times=hp_disappear_times,
            )
            all_sub_clips = dedup_clips(all_sub_clips, battle_times=_battle_times)

            # ── Phase 45a：修 replay leak ─────────────────────────────────────
            # enforce_kill_coverage / trim_kill_tails 可能新增或延伸 clip 進入 replay 區段。
            # 再跑一次 remove_replay_regions 確保 replay 不會偷渡回精華影片。
            _before = len(all_sub_clips)
            all_sub_clips = remove_replay_regions(all_sub_clips, replay_segs)
            logger.info(
                f"[remove_replay_regions 第二輪] enforce_kill 後再砍 -> {_before} -> {len(all_sub_clips)} 段"
            )

        # ── Phase 42：Scene Change 邊界鎖定（移到 enforce_kill_coverage 之後）──
        # 原本在 enforce_kill_coverage 之前跑，讓 kill_enforce 段的 25s 前置保持完整。
        # 但使用者回饋「25s 前置有很多無關空鏡頭」→ 改為之後跑，讓 scene_change
        # 自動把 kill_enforce 段的起點收縮到「擊殺前的最後一個場景切換點」。
        # 這樣 25s 是上限，實際前置長度 = 最近 scene change 之後 → 自動刪去空鏡頭。
        # PROTECTED_LABELS（victory_visual）仍然豁免，不會被動到。
        if scene_changes:
            all_sub_clips = apply_scene_change_boundaries(
                all_sub_clips,
                scene_changes,
                kill_feed_times or [],
                flash_times or [],
                obj_events,
            )
            all_sub_clips = dedup_clips(all_sub_clips, battle_times=_battle_times)

    # ── 結束鐵則：最後一定要有 game_end 畫面（minimal mode 也保留，否則就沒結尾）
    all_sub_clips = enforce_game_end_coverage(
        all_sub_clips, game_end, score=GAME_END_SCORE
    )

    # ── 5/9 R7-strict 鐵則 + 5/15 等價類更新：last_kill 到遊戲結束畫面不可跳過 ──────
    # 強制最後段尾延伸到 end_game_max + 5s，保證連續包含遊戲結束畫面（nexus 或 game_end_screen）。
    # 若 end_game_signals 空（兩個 class 都沒抓到）→ raise NexusCoverageError →
    # __main__ exit 4 → clip_worker mark FATAL_NO_NEXUS 不出爛 highlight。
    logger.info(
        f"  [R7-strict] end_game_signals = {[round(t,1) for t in end_game_signals]} "
        f"(nexus={len(nexus_times)} ∪ game_end_screen={len(_game_end_screen_times)})"
    )
    all_sub_clips = enforce_continuous_last_kill_to_nexus(
        all_sub_clips, end_game_signals,
        end_buffer_sec=GAME_END_TRAIL_SEC,
    )

    # Fix #4：R7-strict 延伸最後段尾後，可能跨過 replay 區（user 觀察到 g2 9:02-9:30 replay）。
    # 第三輪 remove_replay_regions：對 R7 延伸出的範圍重新挖洞，replay 不會偷渡進結尾段。
    if replay_segs:
        _before = len(all_sub_clips)
        all_sub_clips = remove_replay_regions(all_sub_clips, replay_segs)
        logger.info(
            f"[remove_replay_regions 第三輪] R7-strict 延伸後再砍 -> {_before} -> {len(all_sub_clips)} 段"
        )

    # ── Phase 45+ 修 G + S：結算圖表出現前 5 秒切過去（純 BGM 段，總長 14s）──
    # 主堡爆炸完整覆蓋由 apply_victory_protection (R7 + 修 O nexus 校正) 負責
    all_sub_clips = enforce_end_graph_coverage(
        all_sub_clips,
        end_graph_first_seen,
        duration=duration,
        lookback=5.0,
        trail=9.0,           # 修 S：15 → 9（lookback 5 + trail 9 = 總 14s）
        score=GAME_END_SCORE,
    )

    if not minimal:
        # ── Phase 40 鐵則：clip 起點最多從第一個事件往前 10s（objective_replay 例外 45s）──
        all_sub_clips = enforce_max_lead_in(
            all_sub_clips,
            kill_feed_times or [],
            flash_times or [],
            obj_events,
        )

    all_sub_clips = dedup_clips(all_sub_clips, battle_times=_battle_times)

    # ── Phase 45+ 通用原則：無真戰鬥訊號的段時長一律截短（B 版安全做法）──────
    # 「真戰鬥」= kill_feed OR hp_disappear（任一在範圍內就算真戰鬥）。
    # 用 hp_disappear 兜底是因為 v5 模型 mAP ~0.55，kill_feed 偶爾漏偵時
    # hp_disappear 還能接住，避免真擊殺被誤截。
    # PROTECTED_LABELS（victory_visual / game_end）豁免（結尾段必須完整）。
    NO_KILL_MAX_SEC = 10.0
    _kf_sorted = sorted(kill_feed_times or [])
    _hp_sorted = sorted(hp_disappear_times or [])
    if _kf_sorted or _hp_sorted:
        _drop = []
        for c in all_sub_clips:
            if c.get("type") in PROTECTED_LABELS:
                continue
            has_kill = any(c["start"] <= k <= c["end"] for k in _kf_sorted)
            has_hp   = any(c["start"] <= h <= c["end"] for h in _hp_sorted)
            cur_len = c["end"] - c["start"]
            if not (has_kill or has_hp) and cur_len > NO_KILL_MAX_SEC:
                # Fix #1：純物件偵測無戰鬥 → 整段丟（不再保留 10s 走路畫面）
                # 對應 user 抱怨 02:55-03:05 龍打完走路。dragon_combat 不在 PROTECTED_LABELS，
                # 之前 truncate 到 10s 留下純走路 → 改為直接 drop
                logger.info(
                    f"  [無戰鬥丟棄] {c['start']:.0f}~{c['end']:.0f}s "
                    f"({cur_len:.0f}s, type={c.get('type','?')}) -> 整段丟（無 kill 無 hp）"
                )
                _drop.append(c)
        for c in _drop:
            all_sub_clips.remove(c)

    # ── 安全防護：單段 clip 長度上限（PROTECTED_LABELS 豁免）─────────────────
    # 修 U：拿掉「c["end"] = min(c["end"], duration + 5.0)」這條無謂的 cap。
    # 早期用 game_end + 60 推算「影片大概多長」是 LCK Carry 短片的便宜假設，
    # LPL/LCP 完整轉播 end_graph 在 game_end 後 5~10 分鐘，這條 cap 反而把它排除。
    # 真正的影片邊界由 ffmpeg stream copy 自動截到 EOF 處理，不需這條人為 cap。
    MAX_SINGLE_CLIP_SEC = 300.0
    for c in all_sub_clips:
        if c.get("type") in PROTECTED_LABELS:
            continue
        if c["end"] - c["start"] > MAX_SINGLE_CLIP_SEC:
            logger.warning(
                f"  [截斷超長段] {c['start']:.0f}~{c['end']:.0f}s "
                f"({c['end']-c['start']:.0f}s > {MAX_SINGLE_CLIP_SEC:.0f}s 上限)，截至 "
                f"{c['start'] + MAX_SINGLE_CLIP_SEC:.0f}s"
            )
            c["end"] = c["start"] + MAX_SINGLE_CLIP_SEC

    # Phase 41 safety：過濾 invalid clips（end <= start + 1，避免 FFmpeg -to < -ss crash）
    valid_clips = []
    for c in all_sub_clips:
        if c["end"] <= c["start"] + 1.0:
            logger.warning(
                f"  [過濾無效] {c['start']:.0f}~{c['end']:.0f}s "
                f"({c['end']-c['start']:.1f}s) [{c.get('type','?')}] — end <= start"
            )
            continue
        valid_clips.append(c)
    all_sub_clips = valid_clips

    # Phase 40 鐵則：最終輸出必須按真實時間線排序（使用者要求首殺在第一段）
    all_sub_clips.sort(key=lambda c: (c["start"], c["end"]))

    return all_sub_clips


# ─────────────────────────────────────────────────────────────────────────────
# Phase 43：視覺偵測已搬進 scan_video.py，這裡不再有 run_visual_detection
# clip.py（前身：cut_highlights.py）現在純讀 scene.json → 選段 → FFmpeg
# ─────────────────────────────────────────────────────────────────────────────




# ─────────────────────────────────────────────────────────────────────────────
# 主程式
# ─────────────────────────────────────────────────────────────────────────────

class _TeeToLog:
    """把 print() 同時寫到 logger（進 log 檔）"""
    def __init__(self, orig): self._orig = orig
    def write(self, msg):
        self._orig.write(msg)
        stripped = msg.rstrip()
        if stripped:
            logging.getLogger().info(stripped)
    def flush(self): self._orig.flush()
    def fileno(self): return self._orig.fileno()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Scene-Aware 精華剪輯 v3")
    parser.add_argument("--video",  help="來源影片路徑")
    parser.add_argument("--scene",  help="scene events JSON 路徑")
    parser.add_argument("--output", help="輸出影片路徑")
    parser.add_argument("--bp-end", type=float, help="BP 結束時間秒數")
    # Phase 43：--force-rescan / --kill-model / --end-graph-model 已轉移到
    # scan_video.py（因為視覺偵測不再由 clip.py 負責）。
    # 保留空 args 參數避免 main.py 傳入時報錯。
    parser.add_argument("--force-rescan", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--kill-model",     help=argparse.SUPPRESS)
    parser.add_argument("--end-graph-model", help=argparse.SUPPRESS)
    # Phase 45c：debug 用 --minimal 跳過所有後端篩選器，看 detect_battles + remove_replay 原始輸出
    parser.add_argument(
        "--minimal", action="store_true",
        help="debug mode：跳過 objective_filter / kill_enforce / scene_change_boundaries / "
             "max_lead_in / trim_kill_tails / victory_protection 等所有篩選器，"
             "只保留 detect_battles + remove_replay_regions + game_end_coverage",
    )
    args = parser.parse_args()
    _setup_logging()
    sys.stdout = _TeeToLog(sys.stdout)

    source_vod  = Path(args.video)  if args.video  else SOURCE_VOD
    if args.output:
        output_path = Path(args.output)
    else:
        # 未指定 --output 時，從 --video 檔名自動推導，避免覆蓋舊檔
        output_path = OUTPUT_DIR / f"{source_vod.stem}.mp4"

    _out_dir = _paths.output_dir()
    _stem    = source_vod.stem

    if args.scene:
        scene_json = Path(args.scene)
    else:
        _derived_scene = _out_dir / f"{_stem}_scene.json"
        scene_json = _derived_scene if _derived_scene.exists() else SCENE_JSON

    logger.info(f"scene_json: {scene_json}")

    # bp_end 優先順序：--bp-end > scene JSON > emergency fallback
    bp_end_val = args.bp_end
    bp_ui_interval = None   # Phase 40：bp_ui YOLO 實際連續區間
    _game_end_for_bp_cap: float | None = None
    if scene_json.exists():
        with open(scene_json, encoding="utf-8") as f:
            _s = json.load(f)
        if bp_end_val is None:
            bp_end_val = _s.get("bp_end")
        bp_ui_interval = _s.get("bp_ui_interval")
        _game_end_for_bp_cap = _s.get("game_end_time")

    # ── BP_UI YOLO 誤判 fix：用 timeline.game_duration 反推真實 game_start，cap ui_end ──
    # YOLO bp_ui 對 loading screen / champion select 結束動畫 / 隊伍進場儀式有 false positive，
    # 會把 ui_end 往後拖到非 BP 區，BP clip 窗口跟著被拖進遊戲畫面。
    # 若 timeline 已對齊（拓到 game_duration），可以準確算出 game_start_in_video，
    # 強制 ui_end ≤ game_start_in_video，保證 BP 段只落在真實 pre-game 區間。
    if bp_ui_interval and _game_end_for_bp_cap is not None:
        try:
            _row = lookup_game_timeline_by_path(source_vod)
            _tl_dur = _row.get("timeline_game_duration_sec") if _row else None
            if _tl_dur and _tl_dur > 0:
                _game_start_in_video = float(_game_end_for_bp_cap) - float(_tl_dur)
                _orig_end = float(bp_ui_interval.get("end", 0.0))
                _ui_start = float(bp_ui_interval.get("start", 0.0))
                if _game_start_in_video > _ui_start and _game_start_in_video < _orig_end:
                    bp_ui_interval = dict(bp_ui_interval)
                    bp_ui_interval["end"] = _game_start_in_video
                    bp_ui_interval["timeline_capped"] = True
                    logger.warning(
                        "[BP cap] YOLO ui_end=%.0fs 超過 timeline 推算的 game_start=%.0fs "
                        "(game_end=%.0fs - duration=%.0fs)，cap ui_end -> %.0fs "
                        "（避開 loading/intro FP）",
                        _orig_end, _game_start_in_video, _game_end_for_bp_cap,
                        _tl_dur, _game_start_in_video,
                    )
                    # bp_end_val 也要跟著拉回，否則 BP RMS 提取範圍會包到非 BP
                    if bp_end_val is not None and bp_end_val > _game_start_in_video:
                        bp_end_val = _game_start_in_video
        except Exception:
            logger.exception("[BP cap] timeline cap 失敗（不影響主流程，退回 YOLO 原 ui_end）")
    if bp_end_val is None:
        logger.warning(
            f"[WARN] scene.json 與 --bp-end 都沒有 bp_end，使用 emergency fallback "
            f"{_BP_END_EMERGENCY_FALLBACK}s — 這個值是特定舊影片遺留，"
            f"跟當前影片無關。請確認 scan_video.py 的 bp_ui YOLO 偵測是否正常運作！"
        )
        bp_end_val = float(_BP_END_EMERGENCY_FALLBACK)
    bp_start_val = max(0.0, bp_end_val - 75.0)

    print("=== Scene-Aware 精華剪輯 v3（物件戰鬥過濾 + Two-Pass Replay）===\n")
    print(f"  影片：{source_vod}")
    print(f"  BP_END={bp_end_val:.0f}s  BP_START={bp_start_val:.0f}s")
    print(f"  輸出：{output_path}\n")

    with open(_CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    cfg["highlight"]["original_volume"] = 1.0

    # ffmpeg PATH 必須在所有 ffmpeg 呼叫之前設定（含 bp_rms）
    _ffmpeg_bin = cfg.get("ffmpeg_path", "")
    if _ffmpeg_bin:
        os.environ["PATH"] = _ffmpeg_bin + os.pathsep + os.environ.get("PATH", "")

    # ── BP 動態窗口分析（音訊 RMS 驅動）──────────────────────────────────────
    _music_cfg  = cfg.get("music", {})
    _rms_offset = float(_music_cfg.get("bp_rms_offset", 750))
    _rms_start  = max(0.0, bp_end_val - _rms_offset)
    logger.info(f"[BP 分析] 提取音訊 RMS: {_rms_start:.0f}s ~ {bp_end_val:.0f}s ...")
    _rms = compute_bp_rms(str(source_vod), _rms_start, bp_end_val)
    if _rms:
        _bp_start_actual, _bp_end_actual = analyze_bp_clip_window(
            bp_end_val, _rms, _music_cfg,
            bp_ui_interval=bp_ui_interval,
        )
    else:
        _bp_keep = float(_music_cfg.get("bp_duration", 45))
        _bp_start_actual = bp_end_val - _bp_keep
        _bp_end_actual   = bp_end_val
        logger.warning("[BP 分析] RMS 提取失敗，使用固定偏移 fallback")
    cfg["music"]["bp_start_time"] = _bp_start_actual
    cfg["music"]["bp_end_time"]   = _bp_end_actual
    logger.info(
        f"[BP 分析] 最終窗口: {_bp_start_actual:.1f}s ~ {_bp_end_actual:.1f}s"
        f"  ({_bp_end_actual - _bp_start_actual:.0f}s)"
    )

    # BP 音樂路徑：轉成絕對路徑
    _project_root = Path(__file__).parent.parent
    _bp_music_rel = _music_cfg.get("bp_music", "")
    if _bp_music_rel:
        cfg["music"]["bp_music"] = str((_project_root / _bp_music_rel).resolve())
    else:
        cfg["music"]["bp_music"] = ""

    from highlight.rendering.video_editor import VideoEditor
    from highlight.selection.segments import Segment


    # ── Phase 43：所有視覺偵測已由 scan_video.py 完成，這裡純讀 scene.json ──
    if not scene_json.exists():
        raise RuntimeError(
            f"[X] scene.json 不存在：{scene_json}\n"
            f"   請先跑 scan_video.py 產生偵測資料（或跑 main.py 完整 pipeline）"
        )

    with open(scene_json, encoding="utf-8") as f:
        _sd = json.load(f)

    # 遊戲結束時間
    game_end_from_scene = _sd.get("game_end_time")
    if game_end_from_scene is None:
        try:
            import subprocess as _sp
            _probe = _sp.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(source_vod)],
                capture_output=True,
            )
            _dur = json.loads(_probe.stdout.decode("utf-8", errors="replace"))
            game_end_from_scene = float(_dur.get("format", {}).get("duration", 7200.0))
            logger.info(f"game_end_time 未偵測到，使用影片實際長度 {game_end_from_scene:.0f}s")
        except Exception:
            game_end_from_scene = 7200.0
            logger.warning("ffprobe 失敗，game_end fallback 7200s")
    else:
        game_end_from_scene = float(game_end_from_scene)

    # Phase 43：視覺偵測結果全部從 scene.json 取得
    nexus_from_scene     = _sd.get("nexus_explosion_times", []) or []
    kill_feed_times      = _sd.get("kill_feed_times", []) or []
    flash_times          = _sd.get("flash_times", []) or []

    # Phase E2 (5/17)：先用 YOLO kill_feed × Leaguepedia CHAMPION_KILL cross-correlation
    # 算出 anchor + 寫 DB（取代之前 flash icon anchor 偏移 35s 的 bug）
    # 要求：4 連續 YOLO kills 跟 timeline 對到 (±2s)，否則對位失敗 → augment/filter 都跳過
    _compute_kill_correlation_anchor_for_game(kill_feed_times, source_vod)

    # Phase E (5/17)：LCK/LCP 用 Leaguepedia V5 timeline CHAMPION_KILL 補強 YOLO kill_feed
    # 補強模式（user 強調不要動 LPL 主邏輯）：LCK/LCP 有 timeline 才加，LPL/沒 timeline 完全不動
    kill_feed_times = _augment_kill_feed_with_timeline(kill_feed_times, source_vod)

    hp_disappear_times   = _sd.get("hp_disappear_times", []) or []
    suspected_game_end   = _sd.get("suspected_game_end")
    end_graph_first_seen = _sd.get("end_graph_time")

    # Phase 45b：victory_times 只取「跟 game_end_time 同 cluster」的 nexus 時間點
    # 原本把所有 nexus 都當 victory，導致誤判幀（單獨出現的 tower 爆炸/ult 特效被當 nexus）
    # 把 victory 拉到 5 分鐘前，整段精華提早結束。
    # game_end_from_scene 已經是 scan_video 修正後的「最後一個 ≥2 幀 cluster」結果，可信。
    if game_end_from_scene is not None:
        VICTORY_CLUSTER_GAP = 30.0
        nexus_near_end = [
            float(t) for t in nexus_from_scene
            if abs(float(t) - game_end_from_scene) <= VICTORY_CLUSTER_GAP
        ]
        victory_times = sorted([float(game_end_from_scene)] + nexus_near_end)
    else:
        victory_times = sorted(float(t) for t in nexus_from_scene)

    print(
        f"\n[scene.json 讀取] kill_feed={len(kill_feed_times)} / "
        f"flash={len(flash_times)} / hp_disappear={len(hp_disappear_times)} / "
        f"victory={len(victory_times)} / suspected_end={suspected_game_end} / "
        f"end_graph={end_graph_first_seen}"
    )

    # 決定最終 game_end（Phase 41 最終版）：
    #   scene.json.game_end_time 已經是 scan_video 的精準決定值：
    #     - 優先 nexus/victory 最早時間（真實結束瞬間）
    #     - 若 end_graph 有但 nexus/victory 沒 → end_graph - 30s 估計
    #     - 全失敗 → kill_feed last + 30s
    #   所以 clip.py 直接用 scene.json.game_end_time，不用再判斷優先序
    # 修 R：用上方已讀好的 _sd（line ~538），不重複讀檔
    _scene_game_end = _sd.get("game_end_time")
    _nexus_explosion_times = _sd.get("nexus_explosion_times", []) or []
    _scene_end_graph_time = _sd.get("end_graph_time")   # 僅 debug 用

    # 直接採用 scan_video 的決定值（已包含所有 fallback 邏輯）
    if _scene_game_end is not None:
        _final_game_end = float(_scene_game_end)
        logger.info(
            f"[game_end] 採用 scan_video 精準值 = {_final_game_end:.0f}s "
            f"(end_graph 粗定位 = {_scene_end_graph_time}s)"
        )
        _skip_explicit = True
    else:
        _skip_explicit = False

    # 以下是舊 fallback 路徑（若 scene.json.game_end_time 是 None 才會走，極罕見）
    _explicit_candidates = []
    _explicit_end = None
    _explicit_label = None
    if not _skip_explicit:
        if victory_times:
            _explicit_candidates.append(("victory_screen", float(victory_times[0])))
        if _nexus_explosion_times:
            _explicit_candidates.append(("nexus_explosion", float(_nexus_explosion_times[0])))

        if _explicit_candidates:
            _explicit_label, _explicit_end = min(_explicit_candidates, key=lambda x: x[1])
            logger.info(
                f"[game_end] 明確視覺訊號候選：{_explicit_candidates} -> "
                f"採用最早 {_explicit_label} = {_explicit_end:.0f}s"
            )

    if _skip_explicit:
        pass
    elif _explicit_end is not None:
        _final_game_end = _explicit_end
        logger.info(f"[game_end] 採用明確視覺訊號 {_explicit_label} = {_final_game_end:.0f}s")
    elif end_graph_first_seen is not None:
        if kill_feed_times:
            _last_kill = max(kill_feed_times)
            _final_game_end = min(_last_kill + 45.0, float(end_graph_first_seen))
            logger.info(
                f"[game_end] fallback end_graph 推算：last_kill({_last_kill:.0f}s)+45 "
                f"vs end_graph({end_graph_first_seen:.0f}s) -> {_final_game_end:.0f}s"
            )
        else:
            _final_game_end = float(end_graph_first_seen)
            logger.info(f"[game_end] fallback end_graph_first_seen = {_final_game_end:.0f}s")
    elif suspected_game_end is not None:
        _final_game_end = float(suspected_game_end)
        logger.info(f"[game_end] 採用 tower 密集 fallback = {_final_game_end:.0f}s")
    elif _scene_game_end is not None:
        _final_game_end = float(_scene_game_end)
        logger.info(f"[game_end] 採用 scene_json HUD = {_final_game_end:.0f}s")
    else:
        # Phase 49-3e（fail-fast 鐵則 R7）：三層訊號（end_graph / nexus / game_end_screen）
        # 全部 None → 拒絕硬切。寧可漏一支 highlight，也不要剪到「結尾全是 replay /
        # 沒主堡爆炸」的垃圾片。
        raise EndNotFoundError(
            f"game_end 三層訊號皆失："
            f"end_graph={end_graph_first_seen}, "
            f"victory_times={victory_times}, "
            f"nexus_explosion={_nexus_explosion_times}, "
            f"suspected_game_end={suspected_game_end}, "
            f"scene_json.game_end_time={_scene_game_end}. "
            f"本片放棄剪輯（鐵則 R7：必須含主堡爆炸 + 勝利畫面）。"
        )

    game_clips = select_highlights(
        scene_json,
        kill_feed_times=kill_feed_times or None,
        victory_times=victory_times or None,
        flash_times=flash_times or None,
        bp_end=bp_end_val,
        game_end_override=_final_game_end,
        hp_disappear_times=hp_disappear_times or None,
        minimal=args.minimal,
    )

    total = sum(c["end"] - c["start"] for c in game_clips)
    mm, ss = divmod(int(total), 60)
    print(f"\n遊戲精華：{len(game_clips)} 段，總長 {mm:02d}:{ss:02d}")
    for i, c in enumerate(game_clips, 1):
        mm1, ss1 = divmod(int(c["start"]), 60)
        mm2, ss2 = divmod(int(c["end"]), 60)
        dur = c["end"] - c["start"]
        sym = "sub" if not c.get("transition_before", True) else "new"
        print(f"  [{i:2d}] {sym} {mm1:02d}:{ss1:02d}~{mm2:02d}:{ss2:02d}  "
              f"({dur:.0f}s)  [{c['type']}]")

    segments_out = [
        Segment(
            start=c["start"],
            end=c["end"],
            score=c["score"],
            labels=[c.get("type", "density")],
            transition_before=c.get("transition_before", True),
            mute_original_audio=c.get("mute_original_audio", False),
        )
        for c in game_clips
    ]

    # ────────────────────────────────────────────────────────────────
    # Phase D (5/17)：用 timeline 過濾走路/replay 誤判 + 切掉 game 結束後 segments
    # 從 broadcast_games.game_path 反查 game_id，拓 timeline_anchor_sec / external_id
    # quality 'suspicious'(無 timeline 事件) / 'after_game'(超過 GAME_END) → 拿掉
    # quality 'early_game'(開頭 5 min 內走路) / 'before_game'(BP 段) → 保留（一般正常 highlight）
    # ────────────────────────────────────────────────────────────────
    segments_out = _filter_segments_with_timeline(segments_out, source_vod)

    print(f"\n開始剪輯 -> {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    editor = VideoEditor(cfg)
    out = editor.create_highlight(
        source_video=source_vod,
        segments=segments_out,
        output_path=output_path,
        music_file=None,   # VideoEditor 自動從 BPM catalog 選曲
    )
    print(f"\n完成：{out}")

if __name__ == "__main__":
    try:
        main()
    except BPNotFoundError as e:
        # Phase 49-3e（fail-fast）：BP_UI 沒偵測到 → exit 2
        # clip_worker 看到 exit 2 會 mark job 'failed' + reason='FATAL_NO_BP'，撈下一個 pending job。
        # 5/8：用 os._exit 不用 sys.exit。pythonw 跑時 atexit 清理寫 stderr 會把 exit code
        # 改成 120 → clip_worker 誤判一般失敗會 retry。手動 flush 後 _exit 確保 rc=2。
        logger.error(f"FATAL_NO_BP: {e}")
        print(f"[FATAL_NO_BP] {e}", file=sys.stderr)
        try:
            sys.stdout.flush(); sys.stderr.flush()
        except Exception:
            pass
        import os; os._exit(2)
    except EndNotFoundError as e:
        # Phase 49-3e（fail-fast）：游戲結尾三層訊號都沒抓到 → exit 3
        # clip_worker 看到 exit 3 會 mark job 'failed' + reason='FATAL_NO_END'，撈下一個 pending job。
        logger.error(f"FATAL_NO_END: {e}")
        print(f"[FATAL_NO_END] {e}", file=sys.stderr)
        try:
            sys.stdout.flush(); sys.stderr.flush()
        except Exception:
            pass
        import os; os._exit(3)
    except NexusCoverageError as e:
        # 5/9 R7-strict 鐵則：highlight 結尾必須含主堡爆炸 + 5s。
        # nexus_explosion 沒偵測到 / 延伸後仍無 clip 涵蓋 → exit 4
        # clip_worker 看到 exit 4 mark job 'failed' + reason='FATAL_NO_NEXUS' 不 retry。
        # 寧可漏一支 highlight，也不出沒主堡爆炸的爛 highlight（user 鐵則）。
        logger.error(f"FATAL_NO_NEXUS: {e}")
        print(f"[FATAL_NO_NEXUS] {e}", file=sys.stderr)
        try:
            sys.stdout.flush(); sys.stderr.flush()
        except Exception:
            pass
        import os; os._exit(4)

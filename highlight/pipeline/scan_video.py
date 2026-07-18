"""對單場 VOD 執行全面視覺掃描，產生 scene.json 給後續剪輯使用。

掃描內容（8 個 Step）：
  Step 1   BP 偵測（bp_ui）
  Step 2a  End Graph 偵測（→ 反推 game_end）
  Step 2b  Nexus / Victory fallback
  Step 2c  Kill Feed fallback
  Step 3   Replay 偵測
  Step 4   Objectives（baron/dragon/herald/voidgrub）
  Step 5   Kill Feed（YOLO + tower 副偵測）
  Step 6   Flash 偵測（模板 + 亮度狀態機）
  Step 7   HP Disappear（kill ±15s 範圍掃 hp_bar 消失）
  Step 8   Scene Change（FFmpeg scene filter）

使用方式：
  python pipeline/scan_video.py <影片路徑> [--out-dir ...] [--force-rescan]
"""

import argparse
import json
import logging
import sys
from pathlib import Path

# 讓 import detectors / core / selection 等找得到（pipeline/ 的上一層就是專案根目錄）
sys.path.insert(0, str(Path(__file__).parent.parent))

from highlight.utils.utils import ensure_ffmpeg_path, get_video_duration
from highlight.utils import paths

logger = logging.getLogger(__name__)

def run_yolo_scan(video_path: Path, out_path: Path, duration: float) -> dict:
    """用 YOLO 掃描所有視覺偵測資料，一次寫入 scene.json。

    Phase 43 最終順序（所有 YOLO 統一在 scan_video 做完）：
      Step 1  BP detect                       → game_start
      Step 2  End detect（三層 fallback）     → game_end
                2a. end_graph (parallel, ~40s)
                2b. nexus/victory (末端 30min, parallel)
                2c. kill_feed last (全場, parallel) → last_kill + 30s
                2d. 合理性檢查: game_end - game_start >= 15 分鐘，失敗 raise
      Step 3  Replay          [game_start, game_end+60]
      Step 4  Objectives      [game_start, game_end+60]
      Step 5  Kill Feed       [game_start, game_end+60]  ← Phase 43 搬進來
      Step 6  Flash           [game_start, game_end+60]  ← Phase 43 搬進來
      Step 7  HP Disappear    (kill ±15s, 依賴 Step 5)   ← Phase 43 搬進來
      Step 8  Scene Change    [game_start, game_end+60]

    為什麼 End detect 提前到 Step 2：
      - split_vod 切出的 mp4 duration 可能比真實 game_end 大很多（下一場 BP 混進來）
      - 先定位 game_end 才能限縮 Step 3/4/5/6/7/8 範圍，省時間 + 避免誤判

    Phase 43：原本 cut_highlights 的 [視覺偵測] 1/6~6/6 全部搬進來，階段分工乾淨：
      scan_video  = 所有偵測（寫 scene.json）
      clip        = 純讀 JSON + 選段 + FFmpeg（前身：cut_highlights.py）
    """
    ensure_ffmpeg_path()
    try:
        from highlight.detectors.yolo_detector import YOLODetector, cluster_timestamps
    except ImportError as e:
        logger.error(f"無法載入 yolo_detector: {e}")
        return {}

    logger.info("[YOLO 掃描] 載入模型...")
    det = YOLODetector(video_path)

    if det._model is None:
        logger.warning("[YOLO 掃描] 模型未載入，僅輸出空 scene JSON")
        data = {
            "video": str(video_path),
            "replay_segments": [],
            "objective_events": [],
            "game_end_time": None,
            "bp_start": None,
            "bp_end": None,
        }
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    # ── Step 1：BP 偵測（Phase 41 兩階段：稀疏定位 + 精掃 10 分鐘）────────────
    # 使用者要求：前期可能有大量廣告，無法假設 BP 在前 N 分鐘。
    # 策略：
    #   1a. 稀疏掃全場找 bp_ui 第一個 hit（stride=30s，~30 秒內掃完）
    #   1b. 從 first_hit 前 60s 到 first_hit + 600s (10 分鐘) 精掃（stride=5s）
    logger.info("[YOLO 掃描] Step 1 — BP 偵測（兩階段：稀疏定位 + 精掃 10 分鐘）...")
    logger.info("  Step 1a — 稀疏掃全場找 bp_ui 錨點（stride=30s, conf≥0.55）...")
    # 5/16：bp_ui 用 conf≥0.55 過濾掉 LPL 隊伍進場 / 舞台 false positive (conf 0.41-0.47)
    # 真實 BP conf 通常 ≥0.7，0.55 是安全 sweet spot
    coarse_hits = det._detect_multi_classes(
        ["bp_ui"],
        0, duration,
        stride_sec=30.0,
        chunk_size=200,
        conf_overrides={"bp_ui": 0.55},
    )
    raw_coarse = sorted(coarse_hits.get("bp_ui", []))

    if not raw_coarse:
        logger.warning("  [WARN] Step 1a 找不到任何 bp_ui hit -> BP 偵測失敗")
        bp_start, bp_end, bp_ui_interval = None, None, None
    else:
        first_hit = raw_coarse[0]
        last_hit_coarse = raw_coarse[-1]
        logger.info(
            f"  Step 1a 完成：first_hit={first_hit:.0f}s, "
            f"共 {len(raw_coarse)} 個稀疏 hit, 最晚 @{last_hit_coarse:.0f}s"
        )
        # Step 1b：精掃範圍 = [first_hit - 60s, last_hit_coarse + 60s]
        # 5/15 修：原本用 `first_hit + 600s` 對 LCK 直播 broken：
        #         LCK 有 14 分鐘賽前節目（힐링캠프 + 選手介紹 + 廣告），YOLO 會在賽前
        #         畫面誤判 bp_ui (conf 0.45-0.73)，導致 first_hit=0s，範圍卡在 [0, 600s]
        #         錯失真 BP（在 t=660~1260s）。改用 last_hit_coarse 涵蓋全部 hit 分布。
        bp_scan_start = max(0.0, first_hit - 60.0)
        bp_scan_end   = min(duration, last_hit_coarse + 60.0)
        logger.info(
            f"  Step 1b — 精掃 {bp_scan_start:.0f}~{bp_scan_end:.0f}s "
            f"(stride=5s, 範圍 {bp_scan_end - bp_scan_start:.0f}s, "
            f"first_hit={first_hit:.0f}s, last_hit={last_hit_coarse:.0f}s)..."
        )
        bp_start, bp_end, bp_ui_interval = det.get_bp_times_by_index(
            bp_scan_start, bp_scan_end,
            target_game_num=1, stride_sec=5.0,
        )
    logger.info(f"  BP: {bp_start}s ~ {bp_end}s  interval={bp_ui_interval}")

    game_start = bp_end if bp_end else 0.0

    # ── Step 2：End detect（三層 fallback + 合理性檢查）──────────────────
    game_end_time, end_graph_time, end_times, nexus_explosion_times, \
        kill_feed_times_fallback = _detect_game_end(video_path, duration, game_start, det)

    # 合理性檢查：LoL 投降最早 15 分鐘 = 900s
    MIN_GAME_LENGTH = 900.0
    if game_end_time is None:
        raise RuntimeError(
            f"❌ 無法偵測 game_end："
            f"end_graph / nexus / victory / kill_feed 全部失敗\n"
            f"   影片可能損毀或不是完整的比賽（bp={bp_end}, duration={duration}）\n"
            f"   終止 pipeline，不跑後續 Pass 3/4/5"
        )
    if game_end_time - game_start < MIN_GAME_LENGTH:
        raise RuntimeError(
            f"❌ 偵測到的 game_end 時長不合理："
            f"{game_end_time - game_start:.0f}s < {MIN_GAME_LENGTH:.0f}s "
            f"(game_start={game_start:.0f}, game_end={game_end_time:.0f})\n"
            f"   LoL 投降最早 15 分鐘，小於此值一定是誤判\n"
            f"   終止 pipeline，不跑後續 Pass 3/4/5"
        )
    logger.info(
        f"[YOLO 掃描] ✓ game_end 已確認 = {game_end_time:.0f}s "
        f"(遊戲時長 {game_end_time - game_start:.0f}s / "
        f"{(game_end_time - game_start) / 60:.1f} 分鐘)"
    )

    # 後續 Pass 的 scan 上限（game_end + 60s 緩衝）
    scan_end = min(game_end_time + 60.0, duration)

    # ── Step 3：Replay 偵測（限縮 [game_start, scan_end]）──────────────────
    logger.info(
        f"[YOLO 掃描] Step 3 — Replay 偵測 "
        f"{game_start:.0f}~{scan_end:.0f}s (限縮 {scan_end - game_start:.0f}s)..."
    )
    replay_hits = det._detect_multi_classes(
        ["replay"],
        game_start, scan_end,
        stride_sec=2.0,
        chunk_size=200,
    )
    replay_raw = sorted(replay_hits.get("replay", []))
    replay_segments = []
    if replay_raw:
        seg_start = replay_raw[0]
        prev = replay_raw[0]
        for t in replay_raw[1:]:
            if t - prev > 5.0:
                replay_segments.append({"start": seg_start, "end": prev + 1.0})
                seg_start = t
            prev = t
        replay_segments.append({"start": seg_start, "end": prev + 1.0})
    logger.info(f"  replay: {len(replay_segments)} 個區間")

    # ── Step 4：Objectives 偵測（限縮 [game_start, scan_end]）─────────────
    logger.info(
        f"[YOLO 掃描] Step 4 — Objectives 偵測 "
        f"{game_start:.0f}~{scan_end:.0f}s..."
    )
    obj_hits = det._detect_multi_classes(
        ["baron_hp_bar", "dragon_hp_bar", "herald_hp_bar", "voidgrub_hp_bar"],
        game_start, scan_end,
        stride_sec=2.0,
        chunk_size=200,
    )
    objective_events = []
    for cls, label in [
        ("baron_hp_bar",   "baron"),
        ("dragon_hp_bar",  "dragon"),
        ("herald_hp_bar",  "herald"),
        ("voidgrub_hp_bar","voidgrub"),
    ]:
        clustered = cluster_timestamps(obj_hits.get(cls, []), gap_sec=60.0)
        for t in clustered:
            objective_events.append({"time": t, "type": label})
    objective_events.sort(key=lambda x: x["time"])
    logger.info(f"  objectives: {len(objective_events)} 個事件")

    # ── Step 5：Kill Feed 全場偵測（限縮 [game_start, scan_end]）────────────
    # 若 Step 2c 的 fallback 已跑過全場 kill_feed，這裡直接 filter 用，省一次掃描
    kill_feed_times, tower_count, suspected_game_end = _detect_kill_feed_full(
        video_path, game_start, scan_end,
        reuse_kill_feed=kill_feed_times_fallback,
    )

    # ── Step 6：Flash 偵測（限縮範圍）────────────────────────────────────
    flash_times = _detect_flash(det, game_start, scan_end)

    # ── Step 7：HP Disappear 偵測（需要 Step 5 kill_feed_times）─────────────
    hp_disappear_times = _detect_hp_disappear(det, game_start, kill_feed_times)

    # ── Step 8：Scene Change 偵測（FFmpeg，限縮範圍）───────────────────────
    scene_changes = _detect_scene_changes(video_path, game_start, scan_end)

    data = {
        "video": str(video_path),
        "replay_segments": replay_segments,
        "objective_events": objective_events,
        # Phase 41：game_end_time 是「最終決定值」（end_graph 或 nexus/victory 或 last_kill+30s）
        "game_end_time": game_end_time,
        "game_end_screen_times": end_times,
        "nexus_explosion_times": nexus_explosion_times,
        "end_graph_time": end_graph_time,
        "kill_feed_fallback_times": kill_feed_times_fallback,   # 僅 Step 2c 跑時有值（debug）
        "scene_changes": scene_changes,
        "bp_start": bp_start,
        "bp_end": bp_end,
        "bp_ui_interval": bp_ui_interval,
        # Phase 43 新增：clip.py 不再自己跑視覺偵測
        "kill_feed_times":    kill_feed_times,
        "tower_count":        tower_count,
        "suspected_game_end": suspected_game_end,
        "flash_times":        flash_times,
        "hp_disappear_times": hp_disappear_times,
    }
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"[YOLO 掃描] 完成 -> {out_path.name}")
    return data


def _detect_game_end(
    video_path: Path,
    duration: float,
    game_start: float,
    det,
) -> tuple:
    """
    Phase 41：game_end 偵測（end_graph 當錨點 + 精準掃 nexus/victory）

    邏輯：
      Step 2a: end_graph 反向掃末端 15 分鐘 → 粗定位（結算表時間）
               注意：end_graph 只當錨點用來縮範圍，不當 game_end 值
               （因為轉播會先播 5~10 分鐘慶祝才切結算表，差距可能很大）

      Step 2b: 精準掃 nexus/victory → 真實 game_end（勝利瞬間）
        2b-1: end_graph 有 → 掃 [end_graph-600s, end_graph]（前 10 分鐘，無 after window）
        2b-2: end_graph 無 → fallback 掃末端 15 分鐘
        有命中 → game_end = 最早命中（nexus 或 game_end_screen）

      Step 2c: nexus/victory 也失敗 → kill_feed last + 30s（兜底）
      全失敗 → 回傳 None，上層 raise RuntimeError

    回傳 (game_end_time, end_graph_time, end_times, nexus_explosion_times, kill_feed_times_fallback)
    """
    END_SCAN_SEC = 1500.0          # 修 AD：末端 25 分鐘（Phase 41 設 15 min 對 LCP 太短，
                                   # LCP 比賽結束後 PoM/精彩回放占 5~10 分鐘，結算圖表常落在
                                   # mp4 -25min ~ -15min 範圍，舊範圍 15 min 漏抓）
    MIN_GAME_END_SEC = 900.0       # 投降最早 15 分鐘
    PRECISE_WINDOW_BEFORE = 600.0  # end_graph 前推 10 分鐘找 nexus/victory
                                   # 實況：轉播會先播 5~10 分鐘選手慶祝才切結算表
                                   # nexus/victory 絕對比 end_graph 早 → 不需要 AFTER window

    end_scan_start = max(game_start + MIN_GAME_END_SEC, duration - END_SCAN_SEC)
    end_scan_end   = duration

    end_graph_time        = None
    end_times             = []
    nexus_explosion_times = []
    kill_feed_times_fb    = []
    game_end_time         = None

    # ── Step 2a：end_graph 反向掃末端 30 分鐘 → 粗定位 ──────────────────────
    if end_scan_start < end_scan_end:
        try:
            from highlight.detectors.end_graph_detector import EndGraphDetector
            _project_root = Path(__file__).parent.parent
            _eg_model = _project_root / "assets" / "yolo_models" / "end_graph_yolov8n.pt"
            if _eg_model.exists():
                logger.info(
                    f"[YOLO 掃描] Step 2a — end_graph 反向掃 "
                    f"{end_scan_start:.0f}~{end_scan_end:.0f}s（粗定位）..."
                )
                eg_detector = EndGraphDetector(
                    model_path=_eg_model, conf=0.5, sample_interval=5.0,
                    persist_required=3, device=0,
                )
                eg_result = eg_detector.detect(video_path, end_scan_start, end_scan_end)
                end_graph_time = eg_result.first_seen
                if end_graph_time is not None:
                    logger.info(f"  [OK] end_graph 命中 = {end_graph_time:.0f}s（粗定位）")
        except Exception as e:
            logger.warning(f"[YOLO 掃描] end_graph 失敗：{e}")

    # ── Step 2b：精準掃 nexus/victory（範圍視 end_graph 有無而定）───────────
    if end_graph_time is not None:
        # 2b-1: 從 end_graph 前 10 分鐘一路掃到 end_graph（nexus/victory 絕對比 end_graph 早）
        precise_start = max(game_start + MIN_GAME_END_SEC, end_graph_time - PRECISE_WINDOW_BEFORE)
        precise_end   = end_graph_time   # 不需要 after window
        logger.info(
            f"[YOLO 掃描] Step 2b (精準) — nexus/victory 在 end_graph 前 "
            f"{precise_start:.0f}~{precise_end:.0f}s（窗口 {precise_end - precise_start:.0f}s）..."
        )
    elif end_scan_start < end_scan_end:
        # 2b-2: end_graph 無 → 全末端 30 分鐘掃
        precise_start = end_scan_start
        precise_end   = end_scan_end
        logger.info(
            f"[YOLO 掃描] Step 2b (fallback) — end_graph 無，nexus/victory "
            f"反向掃 {precise_start:.0f}~{precise_end:.0f}s..."
        )
    else:
        precise_start = precise_end = 0.0

    if precise_end > precise_start:
        end_hits = det._detect_multi_classes(
            ["game_end_screen", "nexus_explosion"],
            precise_start, precise_end,
            stride_sec=2.0,
            chunk_size=200,
        )
        end_times             = sorted(end_hits.get("game_end_screen", []))
        nexus_explosion_times = sorted(end_hits.get("nexus_explosion", []))
        all_hits = sorted([t for t in (end_times + nexus_explosion_times)
                           if t >= game_start + MIN_GAME_END_SEC])

        if all_hits:
            # Phase 45b 修正：原本 min(candidates) 取最早，但 nexus_explosion 容易誤判
            # （tower 爆炸、ult 特效等），單幀獨立出現會把 game_end 拉到 5 分鐘前。
            # 解法：cluster 後優先取「最後一個 ≥ 2 幀的 cluster」（多幀共識才算真信號）。
            # 因為遊戲只結束一次，最後的密集多幀必然是真實結束畫面。
            CLUSTER_GAP = 30.0   # 30 秒內的 hit 算同一 cluster
            clusters: list[list[float]] = [[all_hits[0]]]
            for t in all_hits[1:]:
                if t - clusters[-1][-1] <= CLUSTER_GAP:
                    clusters[-1].append(t)
                else:
                    clusters.append([t])

            solid = [c for c in clusters if len(c) >= 2]

            # 修 AD2：用 end_graph 當「真/假分界線」
            # LCP 重播會在賽後重播 Victory 字樣 → 兩個 cluster (真實 + 重播)。
            # 真實 victory/nexus 在 end_graph 之前，重播在 end_graph 之後。
            # 主路徑：取「end_graph 之前的最後一個 ≥ 2 幀 cluster」= 真實。
            # Fallback (end_graph=None): 退回舊邏輯「最後一個 ≥ 2 幀」(對 LCK 不影響)
            if solid:
                if end_graph_time is not None:
                    valid = [c for c in solid if c[0] < end_graph_time]
                    if valid:
                        game_end_time = valid[-1][0]
                        logger.info(
                            f"  ✓ 真實 game_end = {game_end_time:.0f}s "
                            f"(end_graph 分界 @{end_graph_time:.0f}s 之前的最後 ≥2 幀 cluster；"
                            f"共 {len(clusters)} 個 cluster, {len(solid)} 個多幀, "
                            f"{len(valid)} 個在分界前；nexus={len(nexus_explosion_times)} 幀, "
                            f"victory={len(end_times)} 幀)"
                        )
                    else:
                        # 罕見：end_graph 之前沒任何 cluster → 退回舊邏輯
                        game_end_time = solid[-1][0]
                        logger.warning(
                            f"  ⚠ end_graph 分界 @{end_graph_time:.0f}s 之前無多幀 cluster，"
                            f"退回舊邏輯取最後 ≥2 幀 = {game_end_time:.0f}s"
                        )
                else:
                    # 修 AD3：end_graph=None 時印明顯 WARNING + fallback 舊邏輯
                    game_end_time = solid[-1][0]
                    logger.warning("=" * 70)
                    logger.warning("[WARN] end_graph 偵測失敗 -> 結尾保護降級為 fallback 舊邏輯")
                    logger.warning("  可能原因:")
                    logger.warning("    1. 該影片真的沒結算圖表（如轉播提前切走）")
                    logger.warning("    2. 該聯賽 end_graph UI model 不熟（需 v6 重訓）")
                    logger.warning("    3. END_SCAN_SEC=1500s 範圍仍不夠（PoM 期 > 25 min）")
                    logger.warning(f"  fallback game_end = {game_end_time:.0f}s "
                                   f"(取最後 ≥2 幀 cluster，可能含重播誤判)")
                    logger.warning("=" * 70)
            else:
                # 沒有任何多幀 cluster → fallback 取最後一個單幀（仍比取最早安全）
                game_end_time = clusters[-1][0]
                logger.warning(
                    f"  ⚠ game_end = {game_end_time:.0f}s "
                    f"(無多幀 cluster，採用最後單幀；可能不準，"
                    f"nexus={nexus_explosion_times}, victory={end_times})"
                )

    # ── Step 2b-dense + Step 2c：5/15 新加密掃 fallback ───────────────────
    # 5/15 改寫（user 指示）：Step 2b 失敗 (game_end_time=None) 時，先跑 kill_feed 拿
    # last_kill，然後在 [last_kill, end_graph] 範圍**密掃** nexus + game_end_screen
    # (stride=0.8s ~每 24 幀 1 次)。任一抓到就重跑 cluster 訂 game_end_time。
    # 仍空才走真 fail-fast (Step 2c)。
    #
    # 設計理由：
    #   - user 訴求「一定要有遊戲結束畫面」— Step 2b stride=2s 可能漏（爆炸動畫只持續 2-3s）
    #   - nexus_explosion 跟 game_end_screen 是「等價類」(end_game_signals)，任一抓到 R7 過關
    #   - 不用 kill_feed 假裝 game_end (那個 +30s 估算是錯的，下游 clip.py 拿假數據會剪垃圾)
    if game_end_time is None:
        logger.warning(
            "[YOLO 掃描] Step 2b 失敗（end_times + nexus 都空 / cluster 無效）→ "
            "嘗試 Step 2b-dense 密掃"
        )

        # 先跑 kill_feed 拿密掃範圍起點 + suspected_game_end fallback
        _suspected_game_end_fb: float | None = None
        try:
            from highlight.detectors.kill_feed_detector import KillFeedDetector
            _project_root = Path(__file__).parent.parent
            _kf_model = _project_root / "assets" / "yolo_models" / "kill_feed_yolo11s.pt"
            if _kf_model.exists():
                kf_detector = KillFeedDetector(
                    model_path=_kf_model, kill_conf=0.55, tower_conf=0.3,
                    sample_interval=1.5, merge_window=5.0, device=0,
                )
                kf_result = kf_detector.detect(video_path, game_start, duration)
                kill_feed_times_fb = kf_result.kill_feed_times
                _suspected_game_end_fb = kf_result.suspected_game_end
                if kill_feed_times_fb:
                    logger.info(
                        f"  (kill_feed scan) {len(kill_feed_times_fb)} 個 kill，"
                        f"last={max(kill_feed_times_fb):.0f}s"
                    )
        except Exception as e:
            logger.error(f"  [Step 2b-dense] kill_feed 偵測失敗：{e}")

        # 算密掃範圍：last_kill ~ end_graph + 30s（最精準）→ end_graph - 300s ~ end_graph + 30s → 末 10 min
        if kill_feed_times_fb:
            # last_kill 前 300s：nexus 爆炸常比 last tower/structure kill 早 2-5 分鐘，
            # 只留 5s 緩衝會整個跳過 nexus explosion 的時間點
            dense_start = max(kill_feed_times_fb) - 300.0
        elif end_graph_time is not None:
            dense_start = end_graph_time - 300.0
        else:
            dense_start = max(end_scan_start, duration - 600.0)
        dense_start = max(dense_start, game_start + MIN_GAME_END_SEC)

        if end_graph_time is not None:
            dense_end = min(duration, end_graph_time + 30.0)
        else:
            dense_end = duration

        if dense_end > dense_start:
            logger.warning(
                f"[YOLO 掃描] Step 2b-dense — nexus + game_end_screen 都空 → 密掃 "
                f"{dense_start:.0f}~{dense_end:.0f}s (stride=0.8s)"
            )
            try:
                dense_hits = det._detect_multi_classes(
                    ["nexus_explosion", "game_end_screen"],
                    dense_start, dense_end,
                    stride_sec=0.8,           # user 指示
                    chunk_size=300,
                )
                nexus_explosion_times = sorted(dense_hits.get("nexus_explosion", []))
                end_times             = sorted(dense_hits.get("game_end_screen", []))
                logger.info(
                    f"  ✓ 密掃結果：nexus={len(nexus_explosion_times)}, "
                    f"game_end_screen={len(end_times)}"
                )

                # 重跑 cluster 邏輯訂 game_end_time（沿用 Step 2b 公式）
                all_hits = sorted([t for t in (end_times + nexus_explosion_times)
                                   if t >= game_start + MIN_GAME_END_SEC])
                if all_hits:
                    CLUSTER_GAP = 30.0
                    clusters: list[list[float]] = [[all_hits[0]]]
                    for t in all_hits[1:]:
                        if t - clusters[-1][-1] <= CLUSTER_GAP:
                            clusters[-1].append(t)
                        else:
                            clusters.append([t])
                    solid = [c for c in clusters if len(c) >= 2]
                    if solid:
                        if end_graph_time is not None:
                            valid = [c for c in solid if c[0] < end_graph_time]
                            game_end_time = valid[-1][0] if valid else solid[-1][0]
                        else:
                            game_end_time = solid[-1][0]
                        logger.info(
                            f"  ✓ 密掃 cluster 後 game_end_time = {game_end_time:.0f}s "
                            f"(共 {len(clusters)} cluster, {len(solid)} 多幀)"
                        )
                    else:
                        # 單幀也接受（密掃才找到，誤判機率低）
                        game_end_time = clusters[-1][0]
                        logger.warning(
                            f"  ⚠ 密掃無 ≥2 幀 cluster，採用最後單幀 = {game_end_time:.0f}s"
                        )
            except Exception as e:
                logger.error(f"  [Step 2b-dense] 密掃失敗：{e}")

    # ── Step 2c：suspected_game_end fallback（tower 密集窗口估算）────────────
    # nexus/end_graph/victory 全部失敗時的最後一道防線
    # 用 KillFeedDetector 的 tower 密集視窗算出的 suspected_game_end
    # 比 last_kill+30s 更準（tower 密集 = 基地破壞 ≈ game end）
    if game_end_time is None and _suspected_game_end_fb is not None:
        game_end_time = _suspected_game_end_fb
        logger.warning("=" * 70)
        logger.warning(
            f"[WARN] Step 2c fallback — nexus/end_graph/victory 全部未偵測到，"
            f"改用 tower 密集窗口 suspected_game_end = {game_end_time:.0f}s"
        )
        logger.warning("  R7（結尾必須含主堡爆炸）可能無法完全滿足，請人工複查")
        logger.warning("=" * 70)

    # ── Step 2d：真 fail-fast ─────────────────────────────────────────────
    if game_end_time is None:
        logger.warning(
            "[YOLO 掃描] Step 2d (真 fail-fast) — Step 2b + Step 2b-dense + suspected_game_end 都失敗，"
            "game_end_time=None。scan 將終止，這場 highlight 放棄（鐵則 R7 強制）。"
        )

    return game_end_time, end_graph_time, end_times, nexus_explosion_times, kill_feed_times_fb


def _detect_kill_feed_full(
    video_path: Path,
    game_start: float,
    scan_end: float,
    reuse_kill_feed: list[float] | None = None,
) -> tuple[list[float], int, float | None]:
    """Step 5 — 全場 Kill Feed 偵測。

    若 Step 2c 已跑過 kill_feed fallback（全場掃描），直接 filter 重用，
    省一次掃描；否則在 [game_start, scan_end] 重新掃一次。
    """
    logger.info(f"[YOLO 掃描] Step 5 — Kill Feed 偵測 {game_start:.0f}~{scan_end:.0f}s...")

    # Case 1：Step 2c 已跑過全場 kill_feed → filter 即可
    if reuse_kill_feed:
        kill_feed_times = [t for t in reuse_kill_feed if game_start <= t <= scan_end]
        logger.info(
            f"  ✓ Kill Feed 重用 Step 2c 結果：{len(kill_feed_times)} 個 kill"
            f"（原 {len(reuse_kill_feed)} 個 → filter 到掃描範圍內）"
        )
        # Step 2c 沒有 tower_count 與 suspected_game_end 資料 → 用預設值
        return kill_feed_times, 0, None

    # Case 2：重新掃描
    try:
        from highlight.detectors.kill_feed_detector import KillFeedDetector
        _project_root = Path(__file__).parent.parent
        _model = _project_root / "assets" / "yolo_models" / "kill_feed_yolo11s.pt"
        if not _model.exists():
            logger.warning(f"  Kill Feed 跳過：找不到模型 {_model}")
            return [], 0, None

        kf_detector = KillFeedDetector(
            model_path=_model, kill_conf=0.55, tower_conf=0.3,
            sample_interval=1.0, merge_window=5.0, device=0,
        )
        kf_result = kf_detector.detect(video_path, game_start, scan_end)
        kill_feed_times    = kf_result.kill_feed_times
        tower_count        = len(kf_result.tower_times)
        suspected_game_end = kf_result.suspected_game_end
        logger.info(
            f"  ✓ Kill Feed：{len(kill_feed_times)} 個 kill / tower={tower_count} / "
            f"sus_end={suspected_game_end}"
        )
        return kill_feed_times, tower_count, suspected_game_end
    except Exception as e:
        logger.warning(f"  Kill Feed 偵測失敗：{e}")
        return [], 0, None


def _detect_flash(det, game_start: float, scan_end: float) -> list[float]:
    """Step 6 — Flash（閃現使用）偵測。"""
    logger.info(f"[YOLO 掃描] Step 6 — Flash 偵測 {game_start:.0f}~{scan_end:.0f}s...")
    try:
        flash_times = det.get_flash_times(game_start, scan_end)
        logger.info(f"  [OK] Flash：{len(flash_times)} 次閃現")
        return flash_times
    except Exception as e:
        logger.warning(f"  Flash 偵測失敗：{e}")
        return []


def _detect_hp_disappear(det, game_start: float, kill_feed_times: list[float]) -> list[float]:
    """Step 7 — HP Disappear 偵測（英雄血條消失節點，kill ±15s 細掃）。

    原理：英雄倒地時 HUD 血條會消失。掃描 kill_feed 前後各 15s 範圍，
    找「上一幀有 hp_bar、下一幀無」的轉變時刻。
    """
    if not kill_feed_times:
        logger.info("[YOLO 掃描] Step 7 — 跳過（無 kill_feed_times）")
        return []

    HP_LOOKBACK = 15.0
    HP_STRIDE   = 0.8
    MIN_PERSIST = 1.0

    # 合併重疊掃描範圍
    raw_ranges = sorted(
        (max(game_start, kt - HP_LOOKBACK), kt + HP_LOOKBACK)
        for kt in kill_feed_times
    )
    merged_ranges = []
    for s, e in raw_ranges:
        if merged_ranges and s <= merged_ranges[-1][1]:
            merged_ranges[-1] = (merged_ranges[-1][0], max(merged_ranges[-1][1], e))
        else:
            merged_ranges.append((s, e))

    total_span = sum(e - s for s, e in merged_ranges)
    logger.info(
        f"[YOLO 掃描] Step 7 — HP Disappear 偵測"
        f"（{len(kill_feed_times)} kill → {len(merged_ranges)} 範圍, "
        f"{total_span:.0f}s, stride={HP_STRIDE}s）..."
    )

    try:
        frame_counts = det._detect_multi_classes_in_ranges(
            ["blue_champ_hp_bar", "red_champ_hp_bar"],
            merged_ranges,
            stride_sec=HP_STRIDE,
        )

        persist_frames = int(MIN_PERSIST / HP_STRIDE)
        hp_disappear_times = []
        prev_total = 0
        persist_count = 0
        for ts, counts in frame_counts:
            total = counts.get("blue_champ_hp_bar", 0) + counts.get("red_champ_hp_bar", 0)
            if total >= 1:
                persist_count += 1
            else:
                if prev_total >= 1 and persist_count >= persist_frames:
                    hp_disappear_times.append(ts)
                persist_count = 0
            prev_total = total

        logger.info(f"  [OK] HP Disappear：{len(hp_disappear_times)} 個節點")
        return hp_disappear_times
    except Exception as e:
        logger.warning(f"  HP Disappear 偵測失敗：{e}")
        return []


def _detect_scene_changes(video_path: Path, game_start: float, duration: float) -> list[float]:
    """Step 8 — 用 FFmpeg scene filter 偵測場景切換點。"""
    try:
        from highlight.detectors.scene_change_detector import SceneChangeDetector
    except ImportError as e:
        logger.warning(f"[Scene Change] 載入失敗，跳過：{e}")
        return []

    logger.info("[YOLO 掃描] Step 8 — 偵測 scene change（FFmpeg）...")
    det = SceneChangeDetector(video_path, threshold=0.3)
    times = det.detect(start_sec=max(0.0, game_start), end_sec=duration)
    logger.info(f"  scene_changes: {len(times)} 個切換點")
    return times


def main():
    parser = argparse.ArgumentParser(description="掃描單場 LOL VOD 產生剪輯用 JSON")
    parser.add_argument("video", help="影片路徑")
    parser.add_argument(
        "--out-dir",
        default=str(paths.output_dir()),
        help=f"JSON 輸出目錄（預設 {paths.output_dir()}）",
    )
    parser.add_argument("--skip-yolo", action="store_true", help="跳過 YOLO 掃描（已有 scene JSON 時使用）")
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
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = video_path.stem

    scene_path = out_dir / f"{stem}_scene.json"

    duration = get_video_duration(video_path)
    logger.info(f"影片長度：{duration / 60:.1f} 分鐘")

    # ── YOLO 掃描 ─────────────────────────────────────────────────────────────
    if not args.skip_yolo:
        scene_data = run_yolo_scan(video_path, scene_path, duration)
    else:
        logger.info(f"[跳過] YOLO 掃描（使用現有 {scene_path.name}）")
        scene_data = json.loads(scene_path.read_text(encoding="utf-8")) if scene_path.exists() else {}

    # ── 完成摘要 ──────────────────────────────────────────────────────────────
    bp_end    = scene_data.get("bp_end")
    bp_start  = scene_data.get("bp_start")
    game_end  = scene_data.get("game_end_time")

    print("\n" + "=" * 60)
    print("掃描完成！")
    print(f"  YOLO 掃描 -> {scene_path}")
    print()
    print(f"  BP_END   = {bp_end}  （{int(bp_end or 0) // 60}:{int(bp_end or 0) % 60:02d}）")
    print(f"  BP_START = {(bp_start or (bp_end - 75) if bp_end else None)}")
    print(f"  game_end = {game_end}")
    print()
    print("下一步：執行精華剪輯")
    print(f"  python tools/clip.py \\")
    print(f"    --video \"{video_path}\" \\")
    print(f"    --scene \"{scene_path}\" \\")
    print(f"    --output \"{paths.final_dir() / f'{stem}_highlights.mp4'}\"")
    print("=" * 60)


if __name__ == "__main__":
    main()

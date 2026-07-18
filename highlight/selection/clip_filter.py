"""精華片段過濾與選段邏輯。

純邏輯函式（不依賴檔案 I/O 或 FFmpeg），包含 R2~R7 的各個過濾步驟與輔助工具。
"""

import logging

logger = logging.getLogger(__name__)

# ── 剪輯精度參數 ──────────────────────────────────────────────────────────────
CLIP_BUFFER        = 4.0   # 物件戰鬥段的前置緩衝（obj_time - 4s 當起點）
HERALD_LEAD_IN_SEC = 3.0   # herald 比其他物件前置更短（只要 3s）
REPLAY_TRAIL_SEC   = 2.0   # replay 消失後最多延伸 2s（Phase 29：4 → 2）
KILL_TAIL_MIN_SEC  = 1.5   # 擊殺後最短保護期
KILL_TAIL_MAX_SEC  = 4.0   # 修 AA：2→4。LCP 一波打架雙方拉開沒擊殺，多留 2s 緩衝。零誤判（不看 hp/flash）

# Phase 45+：trim_kill_tails per-battle-type 加成（在 KILL_TAIL_MAX 之上多保留秒數）
# 規則（使用者第二輪調整）：
#   solo / small / obj_solo                  → +3s（hp@+4.5s ≈ 4~5s tail）
#   teamfight / obj_small / obj_teamfight    → +4s（hp@+5.5s ≈ 5.5~6s tail）
#   *_combat（apply_objective_filter）       → 0
TAIL_BONUS_BY_TYPE: dict[str, float] = {
    "solo_kill":                3.0,    # → 4~5s
    "small_skirmish":           3.0,    # → 4~5s
    "teamfight":                6.0,    # → 7.5~8s（修 E：4→6，大規模團戰要更長）
    "objective_solo_kill":      3.0,    # → 4~5s
    "objective_small_skirmish": 4.0,    # → 5.5~6s
    "objective_teamfight":      6.0,    # → 7.5~8s（修 E）
}
GAME_END_TRAIL_SEC = 5.0   # 遊戲結束後保留 5s
MIN_HIGHLIGHT_SEC  = 480.0 # 精華總長下限（Phase 39：10→8 分鐘）

# ── V3：物件戰鬥過濾 ──────────────────────────────────────────────────────────
OBJ_COMBAT_WINDOW     = 15.0  # 物件血條後觀察 15s
OBJ_NO_COMBAT_TAIL    = 2.0   # 無戰鬥 → 2s 收尾
OBJ_REPLAY_WINDOW     = 60.0  # replay 距 objective ≤ 60s → Objective_Replay

# ── Scene Change 邊界鎖定（Phase 37）─────────────────────────────────────────
SCENE_LEAD_IN_AFTER  = 0.5   # scene change 後多久才當作 clip 起點
SCENE_DEAD_TAIL_SEC  = 2.0   # scene change 後 X 秒沒事件 → 截掉後段
SCENE_TAIL_KEEP_SEC  = 1.0   # scene change 後保留多少秒當收尾

# ── 硬鐵則（Phase 40 / Phase 42 調整）────────────────────────────────────────
# 使用者規格：擊殺片段必須包含「導致擊殺的前因過程」（團聚、走位、大招蓄力等）。
# 原 10s 過度壓縮 → Phase 42 放寬到 25s，給團戰鋪陳留空間。
# Objective_Replay clip 例外：保留 45s 前置直播（R6），標記為 "objective_replay_45s"。
MAX_LEAD_SEC               = 25.0   # 非 objective_replay 的 clip 起點上限（含前因鋪陳）
OBJECTIVE_REPLAY_LEAD_SEC  = 45.0   # Objective_Replay 保留 45s（R6）

# Phase 39：物件戰鬥保護時間減半（走路畫面多）
OBJECTIVE_PROTECT_SEC = {
    "baron":    30.0,
    "dragon":   30.0,
    "herald":   20.0,
    "voidgrub": 15.0,
}

PROTECTED_LABELS = {"game_end", "victory_visual", "end_graph_visual"}


# ── Phase 44：戰鬥聚類參數（以「一場戰鬥」為剪輯單位）─────────────────────
# 使用者核心訴求：每場戰鬥（單挑/小規模/大會戰）都是一個獨立片段，不再黏成大段。
# 演算法：把 kill_feed + hp_disappear + battle_flash 事件按時間聚類。
# 兩事件間隔 > BATTLE_GAP_SEC 視為不同戰鬥。
BATTLE_GAP_SEC     = 15.0   # 兩事件 > 15s 算不同戰鬥（Phase 44a 實測折衷：25s 黏太多，10s 切太碎）
BATTLE_SOLO_LEAD   = 15.0   # 單挑前置
BATTLE_SMALL_LEAD  = 18.0   # 小規模前置（2~3 death）
BATTLE_LARGE_LEAD  = 25.0   # 大會戰前置（4+ death）
BATTLE_TAIL        = 10.0   # 統一尾巴（後續 trim_kill_tails 會精修）
BATTLE_OBJ_WINDOW  = 30.0   # battle ±30s 內有 objective_event → 升級為 objective battle
BATTLE_FLASH_COMBO = 5.0    # flash 在 kill 前 5s 內 → 算戰鬥用閃現


# ─────────────────────────────────────────────────────────────────────────────
# 工具函式
# ─────────────────────────────────────────────────────────────────────────────

def detect_battles(
    kill_times: list[float],
    hp_disappear_times: list[float],
    flash_times: list[float],
    objective_events: list[dict],
    game_start: float,
    game_end: float,
) -> list[dict]:
    """
    Phase 44 核心：把所有戰鬥事件按時間聚類成「一場戰鬥」。

    輸入：
      kill_times         — verified kill_feed（已過 cross_validate_kills）
      hp_disappear_times — 英雄血條消失節點（倒地更精準，不受 replay 影響）
      flash_times        — 所有閃現使用
      objective_events   — scene.json 的 objective_events（baron/dragon/herald/voidgrub）
      game_start, game_end — 限定範圍

    輸出：list[dict]，每場戰鬥一個 clip，欄位：
      start, end, type (solo_kill/small_skirmish/teamfight, 可選前綴 objective_),
      score, kill_count, death_count, cluster_id

    聚類原則：
      事件池 = kill + hp_disappear + 戰鬥用閃現（5s 內跟著 kill 的）
      相鄰事件間隔 ≤ BATTLE_GAP_SEC(25s) 算同場戰鬥
      戰鬥規模以 hp_disappear 為準（kill_feed 可能 over-count，hp 比較接近真實死亡數）
    """
    # 1. 合併戰鬥事件池（限縮在 game_start~game_end 內）
    battle_flash = [
        t for t in flash_times
        if any(t <= kt <= t + BATTLE_FLASH_COMBO for kt in kill_times)
    ]
    raw_events = sorted(set(
        [round(t, 1) for t in kill_times           if game_start <= t <= game_end] +
        [round(t, 1) for t in hp_disappear_times   if game_start <= t <= game_end] +
        [round(t, 1) for t in battle_flash         if game_start <= t <= game_end]
    ))
    if not raw_events:
        return []

    # 2. 按 BATTLE_GAP_SEC 聚類
    clusters: list[list[float]] = [[raw_events[0]]]
    for t in raw_events[1:]:
        if t - clusters[-1][-1] <= BATTLE_GAP_SEC:
            clusters[-1].append(t)
        else:
            clusters.append([t])

    # 3. 每個 cluster 轉成 battle clip
    battles: list[dict] = []

    def _dedup_nearby(events: list[float], min_gap: float = 3.0) -> list[float]:
        """Phase 44：把 min_gap 內的相鄰事件合成一個（同一次真實死亡的多幀 hit 算一個）。"""
        if not events:
            return []
        events = sorted(events)
        out = [events[0]]
        for t in events[1:]:
            if t - out[-1] > min_gap:
                out.append(t)
        return out

    for i, cluster in enumerate(clusters):
        c_start, c_end = cluster[0], cluster[-1]
        kills_in  = [k for k in kill_times         if c_start - 3 <= k <= c_end + 3]
        deaths_in = [h for h in hp_disappear_times if c_start - 3 <= h <= c_end + 3]
        # Phase 44：近距離事件去重，解決 kill_feed/hp_disappear over-count 造成分類虛高
        #   - kill_feed 圖標停留 8~12s 每秒偵測 → 同一擊殺多個 hit 合為 1
        #   - hp_disappear 倒地動畫 + 鏡頭切換可能多幀 → 合為 1
        kills_dedup  = _dedup_nearby(kills_in,  min_gap=3.0)
        deaths_dedup = _dedup_nearby(deaths_in, min_gap=3.0)
        # 戰鬥規模：以 hp_disappear（去重後）為準，比 kill_feed 更接近真實
        n_deaths = len(deaths_dedup) if deaths_dedup else len(kills_dedup)

        if n_deaths <= 1:
            battle_type = "solo_kill"
            lead = BATTLE_SOLO_LEAD
        elif n_deaths <= 3:
            battle_type = "small_skirmish"
            lead = BATTLE_SMALL_LEAD
        else:
            battle_type = "teamfight"
            lead = BATTLE_LARGE_LEAD

        # objective 加權：附近有 baron/dragon/herald/voidgrub → 升級為物件戰
        obj_nearby = [
            ev for ev in (objective_events or [])
            if c_start - BATTLE_OBJ_WINDOW <= ev.get("time", -1) <= c_end + BATTLE_OBJ_WINDOW
        ]
        obj_label = ""
        if obj_nearby:
            battle_type = f"objective_{battle_type}"
            lead = max(lead, BATTLE_LARGE_LEAD)
            obj_label = obj_nearby[0].get("type", "")

        # Phase 45+ 修 2：teamfight 與 objective_teamfight 多前置 8 秒
        # （多 kill_feed 的團戰需要更多前因鋪陳）
        if "teamfight" in battle_type:
            lead += 8.0
        # Phase 45+ 修 C：small_skirmish 與 obj_small_skirmish 多前置 4 秒
        elif "small_skirmish" in battle_type:
            lead += 4.0

        start = max(game_start, c_start - lead)
        end   = min(game_end,   c_end   + BATTLE_TAIL)

        battles.append({
            "start":       start,
            "end":         end,
            "type":        battle_type,
            "score":       1000 + 100 * n_deaths,
            "kill_count":  len(kills_dedup),   # Phase 44：去重後的合理值
            "death_count": n_deaths,
            "kill_raw":    len(kills_in),      # 原始 kill_feed 數（debug 用）
            "death_raw":   len(deaths_in),     # 原始 hp_disappear 數（debug 用）
            "cluster_id":  i,
            "obj_type":    obj_label,
        })

    return battles


def remove_replay_regions(
    clips: list[dict],
    replay_segments: list[dict],
    min_keep_sec: float = 10.0,
    pre_replay_buffer: float = 2.5,
    post_replay_buffer: float = 4.0,
) -> list[dict]:
    """
    Phase 44：把 replay 區段從每個 clip 裡挖掉。

    如果一個 clip 中間有 replay：
      - replay 前的部分 → 保留為一個 sub-clip
      - replay 後的部分 → 保留為另一個 sub-clip
      - replay 本身的區段 → 丟棄
    切出來低於 min_keep_sec 的碎片直接丟棄。

    5/10 v12：pre 從 1.0 → 2.5（user 要求再多砍 1.5s 防 replay 圖卡前一閃即逝）：
      pre_replay_buffer  = 2.5 (replay 浮水印前 2.5 秒砍掉，含 YOLO 偵測延遲 + 圖卡漸進)
      post_replay_buffer = 4.0 (LCK 「REPLAY → LIVE」轉場圖卡 2~3s + 1s 安全餘量)
      總砍 6.5s。

    replay_segments 格式：[{"start": t0, "end": t1}, ...]
    """
    if not replay_segments:
        return clips

    sorted_replays = sorted(replay_segments, key=lambda r: r["start"])
    result: list[dict] = []

    for clip in clips:
        c_start, c_end = clip["start"], clip["end"]
        # 找出跟此 clip 重疊的 replay（含前後緩衝）
        # 修 F (revised)：前置 2s + 後置 5s，挖掉 LCK 轉場動畫
        overlapping = [
            (max(r["start"] - pre_replay_buffer, c_start),
             min(r["end"]   + post_replay_buffer, c_end))
            for r in sorted_replays
            if (r["end"] + post_replay_buffer) > c_start
            and (r["start"] - pre_replay_buffer) < c_end
        ]

        if not overlapping:
            result.append(clip)
            continue

        # 依序把 replay 區段挖掉
        cursor = c_start
        fragments: list[tuple[float, float]] = []
        for r_start, r_end in overlapping:
            if cursor < r_start:
                fragments.append((cursor, r_start))
            cursor = max(cursor, r_end)
        if cursor < c_end:
            fragments.append((cursor, c_end))

        # 把合乎最短長度的碎片加入結果
        for i, (fs, fe) in enumerate(fragments):
            if fe - fs < min_keep_sec:
                continue
            new_clip = dict(clip)
            new_clip["start"] = fs
            new_clip["end"]   = fe
            if len(fragments) > 1:
                new_clip["type"] = f"{clip.get('type','clip')}_split{i+1}"
                # 修 I：split 的非首段強制觸發 xfade 過渡
                # → video_editor 看到 transition_before=True 就會套 xfade fadeblack 0.5s
                #   + acrossfade 0.5s（音訊漸入漸出），避免瞬間斷音
                if i > 0:
                    new_clip["transition_before"] = True
            result.append(new_clip)

    return result


def cross_validate_kills(
    kill_feed_times: list[float],
    hp_disappear_times: list[float],
    window_sec: float = 18.0,
) -> tuple[list[float], list[float]]:
    """
    Phase 41：用 hp_disappear（英雄血條消失）交叉驗證 kill_feed YOLO 偵測結果。

    一個 kill_feed 在 ±window_sec 內若有 hp_disappear，則視為 verified（真擊殺）；
    否則視為 unverified（可能是 minion / tower kill_feed false positive）。

    回傳 (verified, unverified)。
    unverified 的 kill 仍然保留在 kill_feed_times 不丟掉，但 density 過濾不採信。

    為什麼用 ±18s（Phase 42 從 12 調寬）：
      - hp_disappear stride=0.8s
      - kill 後英雄倒地動畫 1~3s
      - hp_disappear 可能漏偵（YOLO 模型仍在改進中）
      - 原本 12s 太緊，容易把真擊殺（尤其先手爆發 kill）誤判成 unverified。
    """
    if not kill_feed_times:
        return [], []
    if not hp_disappear_times:
        # 無 hp_disappear 資料 → 全部視為 unverified，但 caller 應決定要不要照舊用
        return [], list(kill_feed_times)

    hp_sorted = sorted(hp_disappear_times)
    verified = []
    unverified = []
    for kt in kill_feed_times:
        # 二分搜尋最接近的 hp_disappear
        import bisect
        idx = bisect.bisect_left(hp_sorted, kt)
        candidates = []
        if idx < len(hp_sorted):
            candidates.append(hp_sorted[idx])
        if idx > 0:
            candidates.append(hp_sorted[idx - 1])
        if any(abs(hp - kt) <= window_sec for hp in candidates):
            verified.append(kt)
        else:
            unverified.append(kt)

    logger.info(
        f"  [kill 交叉驗證] {len(kill_feed_times)} kill_feed → "
        f"verified {len(verified)} / unverified {len(unverified)} "
        f"(window=±{window_sec:.0f}s)"
    )
    return verified, unverified


def filter_isolated_unverified_kills(
    kill_feed_times: list[float],
    hp_disappear_times: list[float],
    flash_times: list[float] | None = None,
    cluster_gap: float = 15.0,
    hp_window: float = 18.0,
    flash_window: float = 18.0,
) -> list[float]:
    """
    Phase 45+ 修 A：smart cross-validate（取代「全 pool 過濾」舊邏輯）
    修 AG：孤立 kill 驗證放寬為「hp 或 flash」（救 BLG vs JDG 17:44 first blood
            那種 hp_disappear 模型漏抓但有閃現支撐的真戰鬥）

    規則：
      1. 把 kill_feed 按時間聚類（gap < cluster_gap 算同一場戰鬥）
      2. cluster 含 ≥2 kill → 全部保留（信任多殺一起發生）
      3. cluster 只有 1 kill（孤立）→ 必須符合下列任一才保留：
         - ±hp_window 內有 hp_disappear
         - ±flash_window 內有 flash（修 AF 後 flash 已經很準）

    動機：
      - v5 model kill_feed 已經很準（42 vs 真實 39，1.08x）
      - 舊 cross_validate_kills 把全 pool 用 hp 過濾，會誤殺像 herald 多殺
        但 hp_disappear 模型沒抓到的真戰鬥
      - 新邏輯只對「孤立 1 kill」做 hp/flash 驗證，避免誤殺多殺 cluster
    """
    if not kill_feed_times:
        return []

    sorted_kills = sorted(kill_feed_times)

    # 1. 聚類
    clusters: list[list[float]] = [[sorted_kills[0]]]
    for k in sorted_kills[1:]:
        if k - clusters[-1][-1] <= cluster_gap:
            clusters[-1].append(k)
        else:
            clusters.append([k])

    # 2. 過濾
    kept: list[float] = []
    n_dropped_isolated = 0
    n_kept_cluster = 0
    n_kept_isolated_hp = 0
    n_kept_isolated_flash = 0
    for cluster in clusters:
        if len(cluster) >= 2:
            kept.extend(cluster)
            n_kept_cluster += len(cluster)
        else:
            kt = cluster[0]
            has_hp    = any(abs(kt - hp) <= hp_window for hp in (hp_disappear_times or []))
            has_flash = any(abs(kt - f)  <= flash_window for f in (flash_times or []))
            if has_hp:
                kept.append(kt)
                n_kept_isolated_hp += 1
            elif has_flash:
                kept.append(kt)
                n_kept_isolated_flash += 1
            else:
                n_dropped_isolated += 1

    logger.info(
        f"  [smart 過濾] {len(kill_feed_times)} kill_feed → 保留 {len(kept)}"
        f"（多殺 cluster {n_kept_cluster} 個 + 孤立有 hp {n_kept_isolated_hp} 個"
        f" + 孤立有 flash {n_kept_isolated_flash} 個 / "
        f"丟掉孤立無支撐 {n_dropped_isolated} 個）"
    )
    return kept


def snap_to_hp_disappear(
    target_sec: float,
    hp_disappear_times: list,
    window: tuple = (1.5, 6.0),
) -> float | None:
    """
    Phase 35：找擊殺後英雄血條消失的時刻當作 clip 收尾節點。

    從 target_sec + window[0] ~ target_sec + window[1] 範圍內，
    找最早的 hp_disappear time（英雄倒地瞬間）。

    回傳：該時間（None 代表此擊殺找不到對應的血條消失，由原邏輯處理）
    """
    if not hp_disappear_times:
        return None
    lo = target_sec + window[0]
    hi = target_sec + window[1]
    candidates = [t for t in hp_disappear_times if lo <= t <= hi]
    if not candidates:
        return None
    return min(candidates)


def dedup_clips(
    clips: list[dict],
    battle_times: list[float] | None = None,
    bridge_sec: float = 2.0,
) -> list[dict]:
    """
    排序並合併重疊的 clip。

    battle_times: kill + flash 事件時間點列表。
      提供時：以兩段的「最後/最早事件間隔」決定是否合併。
      間隔 ≤ bridge_sec → 同一場會戰，合併。
      間隔 > bridge_sec → 不同會戰，切斷：
        - B.start 調整為 max(B.start, B.first_event - bridge_sec)（只往後縮，不往前拉）
        - B.transition_before = True
        - 調整後若 B.start 仍 < A.end（保護區碰撞）→ 仍合併
      未提供時：退回舊行為（重疊即合併）。
    """
    clips.sort(key=lambda c: c["start"])
    deduped: list[dict] = []

    for clip in clips:
        if not deduped:
            deduped.append(clip)
            continue

        prev = deduped[-1]

        # 無 battle_times → 舊行為：重疊即合併
        if battle_times is None:
            if clip["start"] < prev["end"]:
                prev["end"] = max(prev["end"], clip["end"])
            else:
                deduped.append(clip)
            continue

        prev_events = [t for t in battle_times if prev["start"] <= t <= prev["end"]]
        cur_events  = [t for t in battle_times if clip["start"] <= t <= clip["end"]]

        # 任一方無事件 → 退回重疊合併
        if not prev_events or not cur_events:
            if clip["start"] < prev["end"]:
                prev["end"] = max(prev["end"], clip["end"])
            else:
                deduped.append(clip)
            continue

        prev_last = max(prev_events)
        cur_first = min(cur_events)
        gap = cur_first - prev_last

        # Phase 45+ 修 D：兩段「沒重疊」就完全不動 cur clip 的 start
        # 原本邏輯會把 cur.start 強行拉到 cur_first - bridge_sec，
        # 結果 detect_battles 設好的 lead 33s 被縮成 2s，戰鬥前因消失
        if clip["start"] >= prev["end"]:
            # 沒重疊 → 直接保留原始 clip（保留 detect_battles 設定的 lead）
            deduped.append(clip)
            continue

        if gap <= bridge_sec:
            # 有重疊 + 同一場會戰：合併
            prev["end"] = max(prev["end"], clip["end"])
        else:
            # 有重疊 + 不同會戰：切斷
            # 修 AC：保留 18s lead（避免砍掉「打架前的醞釀畫面」），
            # 之前用 cur_first - bridge_sec(2s) 把 lead 全砍光，導致「人已死才出來」。
            # 新邏輯：B.start = max(原 clip.start, prev.end)
            #   - 原 clip.start 通常含 detect_battles 給的 BATTLE_*_LEAD (15~25s)
            #   - 但若原 start 跟 prev.end 重疊 → 用 prev.end 緊鄰避免重疊
            #   - 不再砍到 cur_first - 2s = 完全沒 lead
            PROTECTED_LEAD_SEC = 18.0   # small_skirmish 標準 lead
            orig_start = clip["start"]
            new_start  = max(orig_start, prev["end"])
            # 保險：若 new_start 之後 lead 仍 < 18s（即 cur_first - new_start < 18），
            #       不動（保留至少緊鄰 prev.end 的 max lead 可能性）
            new_clip   = dict(clip)
            new_clip["start"]            = new_start
            new_clip["transition_before"] = True

            if new_clip["start"] >= clip["end"] - 2.0:
                # 調整後 start 太晚（剩餘 < 2s）→ 強制合併
                logger.debug(
                    f"  [dedup] gap={gap:.1f}s > {bridge_sec}s 但調整後 "
                    f"B.start={new_start:.0f} 太晚（與 B.end {clip['end']:.0f} 差 < 2s），強制合併"
                )
                prev["end"] = max(prev["end"], new_clip["end"])
            else:
                lead_actual = cur_first - new_start
                logger.info(
                    f"  [dedup] gap={gap:.1f}s > {bridge_sec}s → 切斷，"
                    f"B.start {orig_start:.0f}→{new_start:.0f}s "
                    f"(prev.end={prev['end']:.0f}, lead={lead_actual:.0f}s)，加入轉場"
                )
                deduped.append(new_clip)

    return deduped


# ─────────────────────────────────────────────────────────────────────────────
# V3：輔助函式
# ─────────────────────────────────────────────────────────────────────────────

def has_combat_signal(
    obj_time: float,
    kill_feed_times: list[float],
    flash_times: list[float],
    window: float = OBJ_COMBAT_WINDOW,
) -> bool:
    """物件血條出現後 window 秒內是否有擊殺或閃現信號（視覺戰鬥訊號）。"""
    t_end = obj_time + window
    if any(obj_time <= t <= t_end for t in kill_feed_times):
        return True
    if any(obj_time <= t <= t_end for t in flash_times):
        return True
    return False


def classify_replays(
    replay_segs: list[dict],
    objective_events: list[dict],
    window: float = OBJ_REPLAY_WINDOW,
) -> tuple[list[dict], list[dict]]:
    """
    將 replay 分為 Objective_Replay 和 Standard_Replay。
    Objective_Replay：replay 開始距離任一物件事件 ≤ window 秒。
    """
    obj_replays, std_replays = [], []
    for rep in replay_segs:
        nearby = any(abs(rep["start"] - ev["time"]) <= window for ev in objective_events)
        (obj_replays if nearby else std_replays).append(rep)
    logger.info(f"  [Replay 分類] Objective: {len(obj_replays)}, Standard: {len(std_replays)}")
    return obj_replays, std_replays


def _enforce_live_before_replay_single(
    clip: dict,
    first_replay: dict,
    game_start_sec: float,
) -> dict | None:
    """R6：確保 Objective_Replay 前有足夠直播（至少 40s），否則推前或刪除。"""
    live_before = first_replay["start"] - clip["start"]
    if live_before >= 40:
        return clip

    required_start = first_replay["start"] - 45.0
    if required_start < game_start_sec:
        logger.info(
            f"  [R6 刪除] clip {clip['start']:.0f}~{clip['end']:.0f}s "
            f"Objective_Replay 前不足，往前超出遊戲範圍，整段刪除"
        )
        return None

    new_start = max(game_start_sec, required_start)
    clip = dict(clip)
    clip["start"] = new_start
    logger.info(
        f"  [R6 推前] → {new_start:.0f}s，"
        f"確保 {first_replay['start']:.0f}s replay 前有 "
        f"{first_replay['start'] - new_start:.0f}s 直播"
    )
    return clip


def _find_original_start(
    replay_start: float,
    kill_feed_times: list[float],
    game_start_sec: float,
    look_back: float = 45.0,
) -> float:
    """
    E3：往前找 look_back 秒內的 kill 作為 Original_Start。
    找到 → kill - 5s；找不到 → replay_start - look_back。
    """
    t_start = max(game_start_sec, replay_start - look_back)
    kills = [t for t in kill_feed_times if t_start <= t <= replay_start]
    if kills:
        return max(game_start_sec, max(kills) - 5.0)
    return t_start


# ─────────────────────────────────────────────────────────────────────────────
# 核心邏輯
# ─────────────────────────────────────────────────────────────────────────────

def split_into_active_clips(
    clip_start: float,
    win_end: float,
    search_end: float | None = None,
) -> list[dict]:
    """
    把評分窗口初始化成單一 clip。

    歷史備註：原本會依解說靜音（segments）切成多個子片段，並在 R2 視覺鎖定下
    以 kill_feed 阻止過切。解說分析模組已移除後，此函式實質上只是生成
    [clip_start, win_end+10]（截至 search_end）的單段 seed clip，供後續
    apply_objective_filter / dedup 等函式進一步修整。
    """
    if search_end is None:
        search_end = win_end + 50
    end = min(win_end + 10, search_end)
    return [{"start": clip_start, "end": end, "transition_before": True}]


def apply_objective_filter(
    all_sub_clips: list[dict],
    objective_events: list[dict],
    kill_feed_times: list[float],
    flash_times: list[float],
    game_start_sec: float,
) -> list[dict]:
    """
    V3 物件戰鬥過濾器。

    有戰鬥信號 → 保留，延伸至 obj_time + OBJECTIVE_PROTECT_SEC
    無戰鬥信號 → 若已有 clip 覆蓋，不延伸；若無覆蓋，完全略過
    herald 特別：前置緩衝只需 HERALD_LEAD_IN_SEC（3s）
    """
    for ev in objective_events:
        obj_t    = ev["time"]
        obj_type = ev.get("type", "").lower()

        if obj_t < game_start_sec:
            continue

        combat = has_combat_signal(obj_t, kill_feed_times, flash_times)
        protect_sec = OBJECTIVE_PROTECT_SEC.get(obj_type, 45.0)
        pre_buffer  = HERALD_LEAD_IN_SEC if obj_type == "herald" else CLIP_BUFFER

        if combat:
            target_end = obj_t + protect_sec
        else:
            target_end = obj_t + OBJ_NO_COMBAT_TAIL
            logger.info(
                f"  [物件過濾] {obj_type} @{obj_t:.0f}s 無戰鬥信號，"
                f"僅保留 {OBJ_NO_COMBAT_TAIL}s"
            )

        covered = False
        for clip in all_sub_clips:
            if clip["start"] <= obj_t <= clip["end"] + 30:
                if combat and target_end > clip["end"]:
                    logger.info(
                        f"  [物件延伸] {obj_type} @{obj_t:.0f}s → "
                        f"延伸至 {target_end:.0f}s"
                    )
                    clip["end"] = target_end
                covered = True
                break

        if not covered:
            if combat:
                clip_start = max(game_start_sec, obj_t - pre_buffer)
                logger.info(
                    f"  [物件新增] {obj_type} @{obj_t:.0f}s → "
                    f"{clip_start:.0f}~{target_end:.0f}s"
                )
                all_sub_clips.append({
                    "start": clip_start,
                    "end":   target_end,
                    "score": 600,
                    "type":  f"{obj_type}_combat",
                    "transition_before": True,
                })
            else:
                logger.info(
                    f"  [物件略過] {obj_type} @{obj_t:.0f}s 無戰鬥信號且無 clip 覆蓋，跳過"
                )

    return all_sub_clips


def trim_kill_tails(
    clips: list[dict],
    kill_feed_times: list[float],
    flash_times: list[float],
    objective_events: list[dict],
    hp_disappear_times: list | None = None,
    max_tail: float = KILL_TAIL_MAX_SEC,
    roll_window: float = 10.0,
) -> list[dict]:
    """
    擊殺後切於「整場團戰最後一個 kill 的收尾節點」。

    Phase 41-v4：滾動延伸 — 避免團戰還沒打完就切斷
      從 clip 內最後一個 kill 開始，往後每 roll_window 秒內若還有 kill
      → 視為同一場團戰，繼續往後滾（允許超出原 clip.end）
      → 直到連續 roll_window 秒無 kill

    收尾節點優先序（使用者要求「根據 red/blue_champ_hp_bar 消失當節點」）
      1) hp_disappear（final_kill 後 1.5~6s 內）+ 1.5s  ← 優先
      2) final_kill + max_tail  ← 保底

    切點 > 原 clip.end → 延伸（團戰延續）
    切點 < 原 clip.end → 截短（後段是垃圾走位）
    """
    obj_times  = [ev["time"] for ev in (objective_events or [])]
    all_events = sorted(set((kill_feed_times or []) + (flash_times or []) + obj_times))
    sorted_kills = sorted(kill_feed_times or [])

    extended = 0
    trimmed  = 0
    hp_used  = 0
    hard_used = 0

    for clip in clips:
        if clip.get("type") in PROTECTED_LABELS:
            continue

        kills_in = [t for t in sorted_kills if clip["start"] <= t <= clip["end"]]
        if not kills_in:
            continue

        # Phase 41-v4：滾動延伸 — 找整場團戰的最後一個 kill（可超出 clip.end）
        rolling = kills_in[-1]
        while True:
            next_k = next(
                (t for t in sorted_kills if rolling < t <= rolling + roll_window),
                None,
            )
            if next_k is None:
                break
            rolling = next_k
        final_kill = rolling

        # Phase 45+：依 battle_type 加 tail bonus（*_combat 不加，預設 0）
        ctype = (clip.get("type") or "").lower()
        bonus = TAIL_BONUS_BY_TYPE.get(ctype, 0.0)

        # 決定切點：優先 hp_disappear，否則硬切
        hp_end = snap_to_hp_disappear(final_kill, hp_disappear_times or [])
        if hp_end is not None:
            cut_at = hp_end + 1.5 + bonus
            tag = f"hp@{hp_end:.1f}+{1.5+bonus:.1f}s"
            hp_used += 1
        else:
            cut_at = final_kill + max_tail + bonus
            tag = f"hard@+{max_tail+bonus:.1f}s"
            hard_used += 1

        orig_end = clip["end"]

        if cut_at > orig_end + 0.5:
            # 延伸：滾動找到 clip.end 之後還有 kill
            clip["end"] = cut_at
            logger.info(
                f"  [Kill 延續] {clip['start']:.0f}~{orig_end:.0f}s → 延伸至 {cut_at:.1f}s "
                f"(rolling {kills_in[-1]:.0f}→{final_kill:.0f}s, {tag})"
            )
            extended += 1
        elif cut_at < orig_end - 0.5:
            # 截短：clip 後段是垃圾走位。但要先確認 cut_at 之後到 orig_end 沒其他事件
            if any(cut_at < t <= orig_end for t in all_events):
                continue
            clip["end"] = cut_at
            logger.info(
                f"  [Kill 收尾] {clip['start']:.0f}~{orig_end:.0f}s → 截至 {cut_at:.1f}s "
                f"(final kill @{final_kill:.0f}s, {tag})"
            )
            trimmed += 1
        # else: cut_at ≈ orig_end，不動

    if extended or trimmed:
        logger.info(
            f"  [Kill 收尾] 總計：延伸 {extended} 段 / 截短 {trimmed} 段 "
            f"(hp_disappear {hp_used} / 硬切 {hard_used})"
        )

    # Fix #2: 尾部空白截短 — clip 結尾若距離最後事件 > 8s（純走路）→ 截到 last_event + 5s
    # 對應 user 抱怨 g2 05:45-06:03（seg6 small_skirmish 最後事件 1385 vs end 1395 = 10s 走路）
    empty_trimmed = 0
    for clip in clips:
        if clip.get("type") in PROTECTED_LABELS:
            continue
        events_in = [e for e in all_events if clip["start"] <= e <= clip["end"]]
        if not events_in:
            continue
        last_event = events_in[-1]
        empty_tail = clip["end"] - last_event
        if empty_tail > 8.0:
            new_end = last_event + 5.0
            if new_end < clip["end"] - 0.5:
                logger.info(
                    f"  [尾部空白截短] {clip['start']:.0f}~{clip['end']:.0f}s "
                    f"→ 截至 {new_end:.0f}s (last event @{last_event:.0f}s, 空白 {empty_tail:.0f}s)"
                )
                clip["end"] = new_end
                empty_trimmed += 1
    if empty_trimmed:
        logger.info(f"  [尾部空白截短] 總計截短 {empty_trimmed} 段")

    return clips


def apply_victory_protection(
    all_sub_clips: list[dict],
    victory_times: list[float],
    game_end: float | None,
    end_clip_lookback: float = 45.0,
    nexus_times: list[float] | None = None,
    score: int = 2000,
) -> list[dict]:
    """
    R7 視覺層：偵測到 victory/nexus_explosion → 新增獨立結尾段
    [vt - end_clip_lookback, max(target_end_candidates)]。

    type = 'victory_visual'（受 PROTECTED_LABELS 保護，不會被 trim / 截斷）；
    若與既有段輕微重疊，由 dedup_clips 處理合併。

    Phase 45+ 修 O：用 nexus_times 校正 trail。
      LoL 流程：inhib 全推、nexus 暴露 → 立刻顯示 Victory! 字樣 → 1~10s 後才播 nexus 爆炸動畫
      scan_video 的 game_end_time 取 victory 字樣首幀（保守判斷遊戲結束）
      但「主堡爆炸動畫」常晚 5~10s 才出現，trail = game_end + 5s 不夠涵蓋
      校正：取 game_end ±30s 內且晚於 game_end 的 nexus，trail 延伸到 nexus_max + 5s
    """
    if not victory_times:
        return all_sub_clips

    vt = min(victory_times)   # 取最早的 victory 時間點

    # Phase 41 safety：vt 必須在 game_end ±120s 內才合理
    # 避免之前 pipeline 的 bug：vt 抓到下一場 BP/結算（3525s），遠超過 game_end（2760s）
    if game_end is not None and abs(vt - game_end) > 120.0:
        logger.warning(
            f"  [R7] victory_times[0]={vt:.0f}s 距 game_end={game_end:.0f}s 超過 120s，"
            f"視為 false positive，跳過 victory 結尾段"
        )
        return all_sub_clips

    target_end = vt + GAME_END_TRAIL_SEC
    if game_end:
        target_end = max(target_end, game_end + GAME_END_TRAIL_SEC)

    # 修 O：nexus 爆炸動畫常比 victory 字樣晚 5~10s，trail 校正
    # 5/9 修 P：t > game_end 嚴格大於 → t >= game_end - 5 容忍重合
    # （5/9 LCP GZvsCFO g1 case：nexus@2485s 跟 game_end@2485s 同 timestamp，
    #  舊邏輯不觸發校正 → highlight 漏主堡爆炸動畫）
    if nexus_times and game_end is not None:
        late_nexus = [t for t in nexus_times if abs(t - game_end) <= 30.0 and t >= game_end - 5.0]
        if late_nexus:
            nexus_max = max(late_nexus)
            new_end = nexus_max + GAME_END_TRAIL_SEC
            if new_end > target_end:
                logger.info(
                    f"  [R7 nexus 校正] nexus @{nexus_max:.0f}s 晚於 game_end {nexus_max - game_end:.0f}s，"
                    f"trail 從 {target_end:.0f}s 延伸到 {new_end:.0f}s（含主堡爆炸動畫）"
                )
                target_end = new_end

    start = max(0.0, vt - end_clip_lookback)

    # 若既有段已完整覆蓋此區間（罕見），不重複新增
    for clip in all_sub_clips:
        if clip["start"] <= start + 5 and clip["end"] >= target_end - 5:
            logger.info(
                f"  [R7] victory @{vt:.0f}s 已被段 {clip['start']:.0f}~{clip['end']:.0f}s 覆蓋"
            )
            return all_sub_clips

    all_sub_clips.append({
        "start": start,
        "end":   target_end,
        "score": score,
        "type":  "victory_visual",
        "transition_before": True,
    })
    logger.info(f"  [R7] 新增 victory 結尾段 {start:.0f}~{target_end:.0f}s  score={score}")
    return all_sub_clips


def enforce_kill_coverage(
    clips: list[dict],
    kill_feed_times: list[float],
    game_start_sec: float,
    pre_roll: float = 25.0,    # Phase 42：10 → 25，與新 MAX_LEAD_SEC 一致（含前因鋪陳）
    post_roll: float = 8.0,    # Phase 40：6 → 8，讓首殺補段更完整
    score: int = 1000,
    hp_disappear_times: list | None = None,
) -> list[dict]:
    """
    鐵則：每一個擊殺時間點都必須被某個 output clip 涵蓋。

    對未被涵蓋的 kill 強制新增獨立段 [kill - pre_roll, <end>]。
    pre_roll 固定 25s（Phase 42），與 MAX_LEAD_SEC 鐵則一致，包含擊殺前因鋪陳。

    clip 結尾節點優先順序
      1) hp_disappear（英雄倒地瞬間）+ 2.5s    ← Phase 40：1.5 → 2.5
      2) kill + post_roll                       ← 硬切保底
    """
    if not kill_feed_times:
        return clips

    def _covered(kt: float) -> bool:
        return any(c["start"] <= kt <= c["end"] for c in clips)

    added = 0
    hp_used = 0
    for kt in kill_feed_times:
        if _covered(kt):
            continue
        start = max(game_start_sec, kt - pre_roll)

        # clip 結尾節點 — 優先 hp_disappear，否則硬切 kill + post_roll
        hp_end = snap_to_hp_disappear(kt, hp_disappear_times or [])
        if hp_end is not None:
            end = hp_end + 2.5
            hp_used += 1
            tag = f"hp_disappear@{hp_end:.1f}s"
        else:
            end = kt + post_roll
            tag = f"hard@+{post_roll:.0f}s"

        clips.append({
            "start":             start,
            "end":               end,
            "score":             score,
            "type":              "kill_enforce",
            "transition_before": True,
        })
        added += 1
        logger.info(
            f"  [擊殺鐵則] kill @{kt:.1f}s 未被覆蓋 → 新增 {start:.0f}~{end:.0f}s ({tag})"
        )

    if added:
        logger.info(f"  [擊殺鐵則] 共補入 {added} 個擊殺段（hp_disappear 收尾 {hp_used} 個）")
        clips.sort(key=lambda c: c["start"])

    return clips


def enforce_end_graph_coverage(
    clips: list[dict],
    end_graph_time: float | None,
    duration: float | None = None,
    lookback: float = 5.0,
    trail: float = 15.0,
    score: int = 2000,
) -> list[dict]:
    """
    Phase 45+ 修 G：確保「結算圖表出現前 N 秒」有切過去。

    end_graph_time 是 scan_video Step 2a 偵測到的賽後結算表第一幀。
    新增 [end_graph - lookback, end_graph + trail] 為純 BGM 段（mute_original_audio）。

    主堡爆炸的完整覆蓋由 apply_victory_protection (R7 + 修 O nexus 校正) 負責，
    這裡不再延伸前段尾，避免重複/打架。

    特殊情況：
      - end_graph_time = None → 直接 skip
      - 已有 PROTECTED clip 完整涵蓋此區間 → 不重複新增
    """
    if end_graph_time is None or end_graph_time <= 0:
        return clips

    target_start = max(0.0, end_graph_time - lookback)
    target_end = end_graph_time + trail
    if duration is not None:
        target_end = min(target_end, duration + 5.0)
    if target_end - target_start < 5.0:
        return clips

    # 已有 PROTECTED clip 完整覆蓋此區間 → skip
    for c in clips:
        if (c.get("type") in PROTECTED_LABELS and
            c["start"] <= target_start + 2 and
            c["end"] >= target_end - 2):
            logger.info(
                f"  [end_graph] 已被段 {c['start']:.0f}~{c['end']:.0f}s ({c.get('type')}) 覆蓋"
            )
            return clips

    clips.append({
        "start":              target_start,
        "end":                target_end,
        "score":              score,
        "type":               "end_graph_visual",
        "transition_before":  True,
        "mute_original_audio": True,   # 修 L：結算圖表只保留 BGM、去掉原音
    })
    logger.info(
        f"  [end_graph] 新增結算圖表段 {target_start:.0f}~{target_end:.0f}s "
        f"(end_graph @{end_graph_time:.0f}s, 純 BGM, lookback={lookback}s trail={trail}s)"
    )
    return clips


class NexusCoverageError(Exception):
    """5/9 R7-strict 鐵則：highlight 結尾必須包含「遊戲結束畫面」+ 5s。

    5/15 設計修正（user 指示）：把 `nexus_explosion` 跟 `game_end_screen` 視為**語意等價**
    （兩個 class 長相不同，但都代表遊戲結束）→ 任一被偵測到都算過關。
    enforce 函式接收的 `end_game_signals = nexus_explosion ∪ game_end_screen`。

    raise 時機：
      A) scan_video 兩個 class 都沒偵測到任何 hit（極罕見，需 scan_video Step 2b-dense 兜底）
      B) 雖偵測到，但 enforce 後仍無 clip 涵蓋（邏輯矛盾）

    clip_worker 看到 exit code 4 mark FATAL_NO_NEXUS 不 retry（沿用既有機制）。
    寧可漏一支 highlight，也不出沒遊戲結束畫面的爛 highlight。
    """


def enforce_continuous_last_kill_to_nexus(
    clips: list[dict],
    end_game_signals: list[float] | None,
    *,
    end_buffer_sec: float = 5.0,
) -> list[dict]:
    """5/9 R7-strict 鐵則（user 訴求）：最後一個擊殺到遊戲結束畫面不可跳過。

    5/15 設計修正：原本只認 `nexus_explosion_times`，現在改成 `end_game_signals`
    = `nexus_explosion ∪ game_end_screen`（user 指示：兩個 class 等價，任一抓到都算）。
    參數名沒改是維持向後相容（caller 也可繼續傳 nexus_times）。

    保證 highlight 結尾段連續包含到 max(end_game_signals) + end_buffer_sec。
    具體做法：找出最後一個 game-content clip（排除 victory_visual / end_graph_visual /
    game_end PROTECTED_LABELS），如果它的 end < end_game_max + end_buffer_sec，**直接延伸**
    它的 end 到 end_game_max + end_buffer_sec — 不另外新增段，避免中間出現 gap。

    raises:
        NexusCoverageError: end_game_signals 空 / None（兩個 class 都沒抓到），
                            或延伸後仍無 clip 包含 end_game + buffer。
    """
    if not end_game_signals:
        raise NexusCoverageError(
            "scan_video 兩個 end_game class 都沒偵測到（nexus_explosion + game_end_screen 皆空）"
            "→ 無法保證遊戲結束畫面進 highlight"
        )

    nexus_max = max(end_game_signals)
    required_end = nexus_max + end_buffer_sec

    sorted_clips = sorted(clips, key=lambda c: c["start"])
    # 找最後一個「game content」段（排除所有純結尾段，用 PROTECTED_LABELS 統一）
    # 5/11 fix H1：原本只排除 victory_visual / end_graph_visual，但 enforce_game_end_coverage
    # 會 append type='game_end'（也在 PROTECTED_LABELS 內），若它存在會被誤當「最後 game content」
    # 延伸 → 延伸沒意義（它本身就是結尾段）→ 偶發 fail-fast 殺掉本來能出的 highlight。
    last_game_clip = None
    for c in reversed(sorted_clips):
        if c.get("type") in PROTECTED_LABELS:
            continue
        last_game_clip = c
        break

    if last_game_clip is None:
        raise NexusCoverageError(
            "filter 後沒任何 game-content clip 可定位最後一波 → 無法 enforce nexus 連續"
        )

    if last_game_clip["end"] < required_end:
        old_end = last_game_clip["end"]
        last_game_clip["end"] = required_end
        logger.info(
            f"  [R7-strict] 延伸最後段尾 {old_end:.0f}s → {required_end:.0f}s "
            f"(end_game_max={nexus_max:.0f}s + buffer={end_buffer_sec:.0f}s 連續包含遊戲結束畫面)"
        )
    else:
        logger.info(
            f"  [R7-strict] 最後段尾 {last_game_clip['end']:.0f}s >= "
            f"end_game_max+buffer {required_end:.0f}s ✓"
        )

    # 雙保險：再確認最終真有 clip 涵蓋 end_game_max 那一刻
    if not any(c["start"] <= nexus_max <= c["end"] for c in clips):
        raise NexusCoverageError(
            f"延伸後仍無 clip 涵蓋 end_game_max @{nexus_max:.0f}s（邏輯矛盾）"
        )
    return clips


def enforce_game_end_coverage(
    clips: list[dict],
    game_end: float | None,
    lookback: float = 45.0,
    score: int = 2000,
) -> list[dict]:
    """
    鐵則：最後必須有一段包含 game_end 時刻。
    若輸出段中沒有任何段結尾 >= game_end，強制新增結尾段。
    受 PROTECTED_LABELS 保護，不會被 trim 截斷。
    """
    if not clips or not game_end:
        return clips

    for c in clips:
        if c["end"] >= game_end - 2 and c.get("type") in PROTECTED_LABELS:
            return clips

    if any(c["end"] >= game_end + GAME_END_TRAIL_SEC - 2 for c in clips):
        return clips

    start = max(0.0, game_end - lookback)
    end = game_end + GAME_END_TRAIL_SEC
    clips.append({
        "start":             start,
        "end":               end,
        "score":             score,
        "type":              "game_end",
        "transition_before": True,
    })
    logger.info(f"  [結束鐵則] 新增 game_end 段 {start:.0f}~{end:.0f}s  score={score}")
    clips.sort(key=lambda c: c["start"])
    return clips


def enforce_max_lead_in(
    clips: list[dict],
    kill_feed_times: list[float],
    flash_times: list[float],
    objective_events: list[dict],
    max_lead: float = MAX_LEAD_SEC,
    obj_replay_lead: float = OBJECTIVE_REPLAY_LEAD_SEC,
) -> list[dict]:
    """
    硬鐵則：clip 起點最多從第一個事件往前 `max_lead` 秒。

    例外：
      - PROTECTED_LABELS (game_end / victory / victory_visual)：不動
      - clip['lead_in_rule'] == 'objective_replay_45s'：保留 obj_replay_lead 秒

    事件來源：kill_feed + flash + objective_events。若 clip 內無任何事件，不動。
    """
    if not clips:
        return clips

    obj_times = [ev["time"] for ev in (objective_events or [])]
    all_events = sorted(set(
        (kill_feed_times or []) + (flash_times or []) + obj_times
    ))
    if not all_events:
        return clips

    pulled = 0
    for clip in clips:
        if clip.get("type") in PROTECTED_LABELS:
            continue

        # Objective_Replay 例外：保留 45s 前置直播
        if clip.get("lead_in_rule") == "objective_replay_45s":
            lead_limit = obj_replay_lead
        else:
            lead_limit = max_lead

        events_in = [t for t in all_events if clip["start"] <= t <= clip["end"]]
        if not events_in:
            continue

        first_event = min(events_in)
        max_start = first_event - lead_limit
        if clip["start"] < max_start - 0.5:   # 至少差半秒才動
            # Phase 41 safety：拉近後新 start 不能超過 clip.end - 2s（否則 clip 不合法）
            safe_start = min(max_start, clip["end"] - 2.0)
            if safe_start <= clip["start"]:
                continue   # 沒空間拉近，跳過
            logger.info(
                f"  [max_lead] {clip['start']:.0f}~{clip['end']:.0f}s "
                f"→ 起點拉至 {safe_start:.1f}s (first_event@{first_event:.1f}s - {lead_limit:.0f}s)"
            )
            clip["start"] = safe_start
            clip["transition_before"] = True
            pulled += 1

    if pulled:
        logger.info(f"  [max_lead 鐵則] 共調整 {pulled} 段起點")
    return clips


# ─────────────────────────────────────────────────────────────────────────────
# Scene Change 邊界鎖定（Phase 37）
# ─────────────────────────────────────────────────────────────────────────────

def apply_scene_change_boundaries(
    clips: list[dict],
    scene_changes: list[float],
    kill_feed_times: list[float],
    flash_times: list[float],
    objective_events: list[dict],
    lead_in_after: float = SCENE_LEAD_IN_AFTER,
    dead_tail_sec: float = SCENE_DEAD_TAIL_SEC,
    tail_keep_sec: float = SCENE_TAIL_KEEP_SEC,
) -> list[dict]:
    """
    用 scene change 鎖定 clip 邊界，砍掉跟主事件無關的旁支畫面。

    兩個動作：
      1. clip 起點拉近：第一個事件前的最後一個 scene change + lead_in_after
         （避免 clip 包含「換場景前」的別場戰鬥）
      2. clip 終點截斷：clip 內若有 scene change 後 dead_tail_sec 秒沒事件
         → clip 終點 = scene change + tail_keep_sec
         （避免 clip 包含「換場景後」的別場走位）

    PROTECTED_LABELS 的 clip（game_end / victory）不動。
    """
    if not scene_changes or not clips:
        return clips

    sc_sorted = sorted(scene_changes)
    obj_times = [ev["time"] for ev in (objective_events or [])]
    all_events = sorted(set(
        (kill_feed_times or []) + (flash_times or []) + obj_times
    ))

    pulled_in = 0
    trimmed   = 0

    for clip in clips:
        if clip.get("type") in PROTECTED_LABELS:
            continue

        c_start = clip["start"]
        c_end   = clip["end"]

        events_in_clip = [t for t in all_events if c_start <= t <= c_end]
        if not events_in_clip:
            continue   # 沒事件就不該存在這個 clip，留給其他過濾去處理

        first_event = min(events_in_clip)
        last_event  = max(events_in_clip)

        # ── 1. 起點拉近：first event 之前的最後一個 scene change + lead_in_after
        sc_before_first = [s for s in sc_sorted if c_start <= s < first_event]
        if sc_before_first:
            new_start = sc_before_first[-1] + lead_in_after
            # 保護：不能晚於 first_event - 1s（避免把 first event 也排除掉）
            new_start = min(new_start, first_event - 1.0)
            if new_start > c_start + 0.5:   # 至少差半秒才動
                logger.info(
                    f"  [scene 起點] {c_start:.1f}~{c_end:.1f}s "
                    f"→ 起點拉近至 {new_start:.1f}s "
                    f"(scene@{sc_before_first[-1]:.1f}s, first_event@{first_event:.1f}s)"
                )
                clip["start"] = new_start
                clip["transition_before"] = True
                c_start = new_start
                pulled_in += 1

        # ── 2. 終點截斷：clip 範圍內、last_event 之後的 scene change，後續無事件
        sc_after_last = [s for s in sc_sorted if last_event < s <= c_end]
        for sc in sc_after_last:
            window_end = min(sc + dead_tail_sec, c_end)
            events_in_window = [t for t in all_events if sc < t <= window_end]
            if not events_in_window:
                new_end = sc + tail_keep_sec
                if new_end < c_end - 0.5:   # 至少差半秒才動
                    logger.info(
                        f"  [scene 截尾] {c_start:.1f}~{c_end:.1f}s "
                        f"→ 終點截至 {new_end:.1f}s "
                        f"(scene@{sc:.1f}s 後 {dead_tail_sec}s 無事件)"
                    )
                    clip["end"] = new_end
                    trimmed += 1
                break   # 第一個死 scene change 就截，不再往後檢查

    if pulled_in or trimmed:
        logger.info(
            f"  [scene change 邊界] 起點拉近 {pulled_in} 段、終點截尾 {trimmed} 段"
        )
    return clips

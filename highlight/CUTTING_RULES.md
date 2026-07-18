# 剪輯細節規則對照表

修改任何數值前先讀這張表。所有值跟程式碼對齊。

來源：
- 選段主流程：[`pipeline/clip.py`](pipeline/clip.py)
- 過濾 / 收尾 / 鐵則：[`selection/clip_filter.py`](selection/clip_filter.py)
- BP 音訊窗口：[`selection/bp_analyzer.py`](selection/bp_analyzer.py)
- BP / 音樂 / FFmpeg：[`config.yaml`](config.yaml)

---

## 1. 選段方式（事件聚類）

`detect_battles()` 把 kill_feed + hp_disappear + 戰鬥用 flash 事件**聚類**為「一場戰鬥 = 一個 clip」。

| 參數 | 值 | 說明 |
|---|---|---|
| `BATTLE_GAP_SEC` | **15s** | 兩相鄰事件間隔 > 15s → 視為不同戰鬥 |
| `BATTLE_SOLO_LEAD` | **15s** | 單挑（1 死）clip 起點：first_event - 15s |
| `BATTLE_SMALL_LEAD` | **18s** | 小規模（2~3 死）clip 起點：first_event - 18s |
| `BATTLE_LARGE_LEAD` | **25s** | 大會戰（4+ 死）clip 起點：first_event - 25s |
| `BATTLE_TAIL` | **10s** | 統一尾巴（後續 `trim_kill_tails` 會精修） |
| `BATTLE_OBJ_WINDOW` | **30s** | 戰鬥 ±30s 內有 objective → 升級為 objective_battle |
| `BATTLE_FLASH_COMBO` | **5s** | flash 在 kill 前 5s 內 → 算戰鬥用閃現 |
| `NUM_CLIPS` | **20** | 最大選段數 |

---

## 2. 事件權重

| 事件 | 分數 | 說明 |
|---|:---:|---|
| `KILL_SCORE_VISUAL` | **+1000** | 每個擊殺視覺加分（鐵則：每個 kill 必須入選） |
| `KILL_GUARANTEE_SCORE` | **1000** | `enforce_kill_coverage` 補段的分數 |
| `GAME_END_SCORE` | **+2000** | 結尾段（victory / end_graph）分數（鐵則：必須入選） |
| `OBJECTIVE_SCORE_VISUAL` | **+60** | 每個 objective 事件加分 |
| `FLASH_SCORE_VISUAL` | **+30** | 每個閃現使用加分 |
| `COMBO_BONUS` / `COMBO_WINDOW_SEC` | **+100 / 5s** | flash 後 5s 內有 kill → outplay 加成 |

---

## 3. Clip 邊界 / 鋪陳上限

| 項目 | 值 | 說明 |
|---|:---:|---|
| `MAX_LEAD_SEC` | **25s** | 非 objective_replay 的 clip 起點不能比第一個事件早 25s 以上 |
| `OBJECTIVE_REPLAY_LEAD_SEC` | **45s** | Objective_Replay 例外：保留 45s 前置直播（R6） |
| `CLIP_LEAD_IN` | **45s** | R6 同義常數 |
| `CLIP_BUFFER` | **4s** | 物件戰鬥段前置緩衝 |
| `HERALD_LEAD_IN_SEC` | **3s** | Herald 前置 |
| `MAX_SINGLE_CLIP_SEC` | **300s** | 單一 clip 硬上限（`PROTECTED_LABELS` 豁免） |

`MAX_LEAD_SEC=25` 是「上限」不是必定值。實際鋪陳由 `BATTLE_*_LEAD` 給（15/18/25）。

---

## 4. 擊殺後收尾（`trim_kill_tails`）

每個 clip 末尾基於戰鬥類型給 tail 加成，再用 hp_disappear / pause 收尾。

| 項目 | 值 | 說明 |
|---|:---:|---|
| `KILL_TAIL_MIN_SEC` | **1.5s** | 擊殺後最短保護期 |
| `KILL_TAIL_MAX_SEC` | **4.0s** | 基本上限 |
| solo_kill / small_skirmish | +3s | 共 ~4~5s tail |
| objective_solo_kill | +3s | 共 ~4~5s tail |
| objective_small_skirmish | +4s | 共 ~5.5~6s tail |
| teamfight / objective_teamfight | +6s | 共 ~7.5~8s tail |

---

## 5. 擊殺鐵則（`enforce_kill_coverage`）

對「沒被現有 clip 覆蓋的 kill」強制新增獨立 clip：

| 項目 | 值 | 說明 |
|---|:---:|---|
| `pre_roll` | **25s** | 補段起點：kill - 25s（與 MAX_LEAD_SEC 一致） |
| `post_roll` | **8s** | 沒 hp_disappear 時的硬切保底 |
| hp_disappear 收尾 | **+2.5s** | 有 hp_disappear → end = hp_disappear + 2.5s |
| 補段 score | **1000** | type=`kill_enforce`，跟 KILL_SCORE_VISUAL 同級 |

---

## 6. Replay 處理

| 項目 | 值 | 說明 |
|---|:---:|---|
| `REPLAY_TRAIL_SEC` | **2s** | Replay 消失後最多延伸 2s |
| `OBJ_REPLAY_WINDOW` | **60s** | Replay 起點距任一 objective ≤ 60s → Objective_Replay |
| Objective_Replay | 保留 + 強制 ≥ 45s 直播 | R6；不足會推前；超出範圍整段刪 |
| Standard_Replay | 截掉 | 只保留前面直播；< 5s 整段丟 |
| `MIN_HIGHLIGHT_SEC` | **480s（8 分）** | Pass1 總長 < 此值 → Pass2 補回 Standard_Replay |

---

## 7. 物件戰鬥保護（R5）

`OBJECTIVE_PROTECT_SEC`：

| 物件 | 保留 | 無戰鬥時 |
|---|:---:|:---:|
| baron | **30s** | 2s |
| dragon | **30s** | 2s |
| herald | **20s** | 2s |
| voidgrub | **15s** | 2s |

| 項目 | 值 | 說明 |
|---|:---:|---|
| `OBJ_COMBAT_WINDOW` | **15s** | 物件出現後 15s 觀察期：有 kill / flash → 有戰鬥 |
| `OBJ_NO_COMBAT_TAIL` | **2s** | 無戰鬥時的保留時間 |
| baron / elder_dragon 特別延伸 | obj + 90s | 高優先補充（`apply_objective_filter` 內） |

---

## 8. 遊戲結束（R7）

| 項目 | 值 | 說明 |
|---|:---:|---|
| `GAME_END_TRAIL_SEC` | **5s** | game_end 之後保留 5s |
| `PROTECTED_LABELS` | `{game_end, victory_visual, end_graph_visual}` | 這些 label 的 clip 不被 trim |
| game_end 信號優先順序 | victory > nexus_explosion > end_graph + last_kill > suspected_game_end > scene_json HUD 消失 > ffprobe 全長 | 在 `pipeline/clip.py` 主邏輯 |

---

## 9. BP 片頭（`config.yaml` + `bp_analyzer.py`）

| 項目 | 值 | 說明 |
|---|:---:|---|
| `bp_duration` | **45s** | fallback 秒數（動態長度失敗時用） |
| `bp_duration_min` | **35s** | 動態長度下限 |
| `bp_duration_max` | **65s** | 動態長度上限 |
| `bp_post_pick_wait` | **42s** | 平淡局 fallback：bp_end - 42s 當錨點 |
| `bp_rms_offset` | **750s** | RMS 搜尋範圍：bp_end 往前 750s |
| `bp_excitement_threshold` | **1.5** | 激動局判斷閾值（peak / median） |
| `bp_excitement_scale` | **3.0** | 激動比例達此值時 clip 達最大長度 |
| `bp_ui_interval` | scan_video 給 | 搜尋範圍 + 最終窗口都 clamp 在此區間（只用有 BP 畫面的地方） |
| 轉場 | 0.5s fadeblack xfade | R3 鐵律 |

---

## 10. 去重 / 合併（`dedup_clips`）

| 項目 | 值 | 說明 |
|---|:---:|---|
| `bridge_sec` | **2s** | 兩段最後/最早事件間隔 ≤ 2s → 同場會戰，合併 |

---

## 11. Scene Change 邊界鎖定

用 FFmpeg scene filter 偵測導播切鏡，調整 clip 邊界：

| 項目 | 值 | 說明 |
|---|:---:|---|
| `scene.threshold` | **0.3** | FFmpeg scene filter 差異度（0~1） |
| `SCENE_LEAD_IN_AFTER` | **0.5s** | 最近前一個 scene change + 0.5s 作為 clip 起點 |
| `SCENE_DEAD_TAIL_SEC` | **2s** | scene change 後 2s 沒 kill/flash/objective → 截掉後段 |
| `SCENE_TAIL_KEEP_SEC` | **1s** | 截斷時保留 1s 當收尾 |

`PROTECTED_LABELS` 不受此規則影響。

---

## 12. 強制鐵則（必須執行）

| # | 鐵則 | 函式 |
|---|---|---|
| 1 | 每個 kill_feed 都必須被某個 clip 覆蓋 | `enforce_kill_coverage` |
| 2 | 最後段必須包含 game_end 時刻 | `enforce_game_end_coverage` |
| 3 | clip 結尾必須含 end_graph 過渡 | `enforce_end_graph_coverage` |
| 4 | clip 起點最多從第一個事件往前 25s（objective_replay 例外 45s） | `enforce_max_lead_in` |
| 5 | Objective_Replay 前必須 ≥ 45s 直播（R6） | `apply_objective_filter` |
| 6 | 轉場必須 0.5s fadeblack（R3） | `video_editor` |
| 7 | BP 段硬綁 `bp_ui` YOLO 偵測區間 | `bp_analyzer.analyze_bp_clip_window` |
| 8 | game_end 後的 kill_feed 全部砍掉 | `pipeline/clip.py` 主邏輯 |
| 9 | 孤立 unverified kill：±18s 內無 hp_disappear / flash → 丟棄 | `filter_isolated_unverified_kills` |
| 10 | 最終 segments 按時間升序排列 | `select_highlights` 尾端硬排序 |

# highlight/ — 核心剪輯系統

給一支英雄聯盟比賽 VOD → 產出 10~15 分鐘 highlight 影片。
不依賴 `automation/` 或 `dashboard/`，可以單機 / 跨機器搬遷單獨使用。

---

## 三步驟主流程

```
input VOD (35min ~ 4hr)
      ↓
  ① split_vod    → 切成單場 game.mp4（>2hr 自動分割）
      ↓
  ② scan_video   → YOLO 跑全片，輸出 scene.json（事件清單）
      ↓
  ③ clip          → 套 7 條剪輯鐵律選段 + FFmpeg 拼接 + BGM
      ↓
output: <game>_highlights.mp4
```

每一步都是獨立 module，可單獨跑（也可 `--skip-split` 跳過分割）。

---

## 子資料夾用途

```
highlight/
├── main.py            CLI 入口（協調 ① ② ③）
├── config.yaml        音樂 / YOLO / FFmpeg 編碼參數
├── CLAUDE.md          剪輯邏輯硬性鐵則（給 AI 看的）
├── CUTTING_RULES.md   剪輯參數對照表（改值前看這個）
│
├── pipeline/          三步驟主流程實作
├── detectors/         純偵測層（看到什麼）
├── selection/         選段評分層（值不值得剪）
├── rendering/         影片組裝（剪出來）
├── utils/             共用 helper
├── training/          YOLO 訓練腳本
└── assets/            模型 / 音樂 / template / cookies
```

### 各子資料夾 1 句話

| 子資料夾 | 一句話 | 主要檔案 |
|---|---|---|
| `pipeline/` | 三步驟：分割 → 掃描 → 剪輯 | `split_vod.py` / `scan_video.py` / `clip.py` |
| `detectors/` | 跑 YOLO，看影片裡發生什麼 | `yolo_detector.py`（主）/ `kill_feed_detector.py` / `end_graph_detector.py` / `scene_change_detector.py` |
| `selection/` | 算分、套剪輯鐵則、選哪些段值得剪 | `clip_filter.py`（核心）/ `bp_analyzer.py` / `timeline_validator.py` / `segments.py` |
| `rendering/` | FFmpeg xfade 串接 + BGM + 解說壓音 | `video_editor.py` / `music_library.py` |
| `utils/` | 路徑 helper、VOD metadata 反查、FFmpeg path | `paths.py` / `utils.py` / `vod_metadata.py` / `scene_view.py` |
| `training/` | YOLO 訓練 / 上傳 Roboflow / 截 ROI 樣本 | `train_*.py` / `extract_*.py` / `upload_to_roboflow.py` |
| `assets/` | 4 個 YOLO 模型 + 21 首 NCS BGM + 1 個 template + Bilibili cookies | `yolo_models/` / `music/` / `templates/` |

### 設計分層原則

```
detectors（看到什麼）→ selection（值不值得剪）→ rendering（剪出來）
```

修錯時順著資料流找層別：
- 偵測不到事件 → `detectors/`
- 事件偵測到但選段選錯 → `selection/`
- 選段對但影片爛 → `rendering/`

---

## 啟動方式

```powershell
$env:KMP_DUPLICATE_LIB_OK="TRUE"
$py = "$env:USERPROFILE\anaconda3\envs\lol-env\python.exe"

# 1. 已切好的單場（推薦，跳過 split 步驟）
& $py -m highlight.main "E:\videos\split\LCK_20260520_g1_T1vsGEN.mp4" --skip-split

# 2. 從 DB 撈
& $py -m highlight.main --from-game <game_id>
& $py -m highlight.main --from-broadcast <broadcast_id>
```

### CLI flag

| Flag | 用途 |
|---|---|
| `--skip-split` | 跳過分割（VOD 已是單場） |
| `--force-rescan` | 強制重跑視覺偵測，忽略 scene.json cache |
| `--teams CFO,SHF` | 只處理檔名含這些隊伍縮寫的場次 |
| `--kill-model <path>` | 覆寫 kill_feed YOLO 模型路徑 |
| `--end-graph-model <path>` | 覆寫 end_graph YOLO 模型路徑 |
| `--from-game <id>` | 從 DB `broadcast_games` 表撈 game_path |
| `--from-broadcast <id>` | 從 DB `broadcasts` 表撈所有 games |

### Return code

| Code | 含義 |
|---|---|
| 0 | 成功 |
| 2 | FATAL_NO_BP（沒抓到 BP，違反鐵則 R1） |
| 3 | FATAL_NO_END（沒抓到結尾，違反鐵則 R7） |
| 其他 | 失敗（看 `_tmp/logs/cut_progress.log`） |

---

## 4 個 YOLO 模型

`highlight/assets/yolo_models/`：

| 檔案 | 類別數 | 用途 |
|---|---|---|
| `lol_detector.pt` | 11 | 主模型：BP UI / game_end_screen / nexus_explosion / baron / dragon / champ_hp / replay 等 |
| `champ_hp_detector.pt` | 4 | 血條偵測：blue/red 英雄血條 + voidgrub + ignore（HP disappear scan 用） |
| `kill_feed_yolo11s.pt` | 2 | 擊殺廣播 + 防禦塔（end_graph fallback 用） |
| `end_graph_yolov8n.pt` | 1 | 賽後總表（反推 game_end fallback） |

模型版本控管見 [`CLAUDE.md`](CLAUDE.md) 的「.onnx 必須與 .pt 同次訓練產出」規則。

---

## 7 條剪輯鐵律（摘要）

| # | 規則 | 狀態 |
|---|---|---|
| R1 | BP 只擷取第二輪 ban（第 4 個 ban 位）到英雄交換完畢 | 🟡 近似 |
| R2 | 擊殺 / 會戰不能提早截斷：延伸到區域 5s 內無新傷害 | 🟡 近似 |
| R3 | 轉場必須是 0.5s 黑色淡入淡出 | ✅ |
| R4 | 戰鬥前 padding 受 `MAX_LEAD_SEC=25s` 限制（依規模 15/18/25s） | ✅ |
| R5 | Baron / Elder Dragon 拉扯期完整保留 | ✅ |
| R6 | 絕對不能先出現 Replay 才出現直播畫面 | 🟡 近似 |
| R7 | 最後一段必須包含主堡爆炸 + 勝利畫面 | ✅ |

完整實作細節 + 改進清單見 [`CLAUDE.md`](CLAUDE.md) 跟 [`../reports/_archive/希望AI後續改進的判斷邏輯整理.docx`](../reports/_archive/希望AI後續改進的判斷邏輯整理.docx)。

---

## 影片輸入輸出路徑

跨機器搬遷時可用 `VIDEO_DIR` / `OUTPUT_DIR` env 覆寫實體位置。

| 用途 | 路徑 helper | 預設 |
|---|---|---|
| 待剪 VOD 輸入 | `paths.split_dir()` / `paths.lol_games_vods_dir()` | `E:/videos/split/` / `E:/videos/lol_games_vods/` |
| YOLO 掃描結果 | `paths.scan_dir()` | `E:/videos/scan/` |
| 最終 highlight 輸出（手動剪） | `paths.finals_dir()` | `E:/videos/finals/` |
| 最終 highlight 輸出（自動剪） | `paths.final_dir()` | `F:/lol-highlights/output/final/` |

詳細命名規則：[`../automation/NAMING.md`](../automation/NAMING.md)。

---

## 想了解更多？

| 主題 | 文件 |
|---|---|
| 剪輯硬性鐵則 / 模型對齊 / 邊界 | [`CLAUDE.md`](CLAUDE.md) |
| 剪輯參數對照表（lead/tail/buffer/gap/window） | [`CUTTING_RULES.md`](CUTTING_RULES.md) |
| 影片命名規則 | [`../automation/NAMING.md`](../automation/NAMING.md) |
| 共用工程鐵則（路徑 / .env / lol-env / emoji） | [`../CLAUDE.md`](../CLAUDE.md) |

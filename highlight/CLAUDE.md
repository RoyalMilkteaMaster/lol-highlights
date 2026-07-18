# highlight/ 剪輯系統規則

給 AI 看的硬性規則。**任何修改前先看完這份**。
專案結構、子資料夾用途、CLI 用法看 [`README.md`](README.md)。
共用工程鐵則（路徑 / .env / lol-env / 禁 emoji）看 [`../CLAUDE.md`](../CLAUDE.md)。

---

## 目的

給一支 VOD → 產出 10~15 分鐘 highlight。
**剪輯品質優先於速度**：偵測 / 選段都允許多花時間，但**絕不能漏關鍵段或剪錯**。

---

## 7 條剪輯鐵律（修改 selection / rendering 時必看）

> 數值會變、精神不變。**所有具體數值請看 [`CUTTING_RULES.md`](CUTTING_RULES.md)**，這裡只列「為什麼」。

| # | 規則 | 違反後果 | 實作狀態 |
|---|---|---|---|
| R1 | BP 只擷取第二輪 ban（第 4 個 ban 位）到英雄交換完畢 | BP 段太長無聊 / 太短沒交代陣容 | 🟡 bp_ui YOLO 偵測整段；精確第 4 ban 位待加強 |
| R2 | 擊殺 / 會戰片段不能提早截斷 | 看到擊殺就 cut，沒交代戰鬥延伸 | 🟡 戰鬥類型對應 tail 1.5~8s（見 CUTTING_RULES 第 4 節）+ HP disappear 偵測 |
| R3 | 轉場必須是 0.5s 黑色淡入淡出 | 硬切看起來像沒剪過 | ✅ `video_editor xfade=fadeblack:duration=0.5` |
| R4 | clip 起點不能比第一個事件早太多（避免無謂鋪陳） | padding 太長 → 觀眾跳過 | ✅ `MAX_LEAD_SEC=25s` 上限；實際 `BATTLE_*_LEAD=15/18/25s` 看規模 |
| R5 | Baron / Elder Dragon 拉扯期完整保留到物件被吃或一方撤退 | 拉扯沒給完 → 不知道結果 | ✅ baron/dragon 30s protection + 特別延伸 `obj+90s`（high priority obj）|
| R6 | 絕對不能先出現 Replay 才出現直播畫面 | 用回放當開場 → 觀眾以為斷掉 | 🟡 `OBJECTIVE_REPLAY_LEAD_SEC=45s` 強制 ≥45s 直播 |
| R7 | 最後一段必須包含主堡爆炸 + 勝利畫面 | 沒結尾 → highlight 看起來爛尾 | ✅ `enforce_game_end_coverage` + `enforce_end_graph_coverage`，組合 end_graph + nexus_explosion |

**強制鐵則的程式實作位置**（10 條 enforce_*）：[`CUTTING_RULES.md`](CUTTING_RULES.md) 第 12 節。
🟡 項目改進細節：[`../reports/_archive/希望AI後續改進的判斷邏輯整理.docx`](../reports/_archive/希望AI後續改進的判斷邏輯整理.docx)。

---

## `config.yaml` 是設定唯一來源

不在程式裡用硬編碼常數覆蓋 `config.yaml`。Emergency fallback 必須印 `WARNING`。

`highlight/config.yaml` 區段：

| Section | 內容 |
|---|---|
| `highlight` | 音量、loudnorm 響度標準化 |
| `music` | BP RMS 參數、遊戲段 BGM、BPM 範圍 |
| `output` | FFmpeg preset / crf |
| `kill_feed` | YOLO 模型路徑、conf、sample_interval、device |
| `end_graph` | 同上 |
| `ffmpeg_path` | FFmpeg 8.1 路徑（可被 env `FFMPEG_BIN` override） |

剪輯規則常數（lead/tail/buffer/window/gap）寫死在 `selection/clip_filter.py` 與 `pipeline/clip.py` 內 — 對照表 [`CUTTING_RULES.md`](CUTTING_RULES.md)。

---

## `.onnx` 必須與 `.pt` 同次訓練產出

否則 class 數對不上 → 偵測失敗 / 模型載入錯誤。

- `YOLODetector` 優先載 `.onnx`，沒有才 fallback `.pt`
- 匯出時兩者一起匯出
- 舊版 `.onnx` **必須刪掉**，不能留半套

---

## 4 個 YOLO 模型

`highlight/assets/yolo_models/`：

| 檔案 | 類別數 | 必要性 |
|---|---|---|
| `lol_detector.pt` | 11 | 必要（主模型：BP / nexus_explosion / objects） |
| `champ_hp_detector.pt` | 4 | 必要（blue/red champ HP + voidgrub HP + ignore），HP disappear scan |
| `kill_feed_yolo11s.pt` | 2 | 必要（擊殺廣播 + tower fallback） |
| `end_graph_yolov8n.pt` | 1 | 必要（賽後總表 game_end fallback） |

少任何一個都會炸。模型路徑寫死在 `config.yaml`。

---

## 音樂選擇規則

- **BP 段**：`config.yaml music.bp_music`（預設 `""` 空字串 = BP 段純原聲），`selection/bp_analyzer.py` 依音訊 RMS 動態挑窗口
- **遊戲段**：`rendering/video_editor.py::_pick_gameplay_music()` 從 BPM catalog 自動選，範圍 `gameplay_bpm_min: 120` ~ `gameplay_bpm_max: 130`（`config.yaml`），音量 `music_volume: 0.10`
- **解說壓音**：`rendering/video_editor.py` 用 `sidechaincompress threshold=0.08 ratio=4 attack=80 release=500 level_sc=0.9`（壓主播聲音變大時的 BGM）
- **加新音樂**：丟進 `assets/music/` + 跑 `python -m highlight.rendering.music_library scan` 重建 `catalog.json`；新檔 BPM 必須在 120~130 範圍才會被選

---

## 架構邊界（絕對不准跨）

```
highlight/
├── main.py            ← 允許 lazy import automation.db (--from-game / --from-broadcast)
├── utils/
│   └── vod_metadata.py ← 允許 lazy import automation.db (單向例外)
│
└── 其他全部子模組    ← 絕對禁止 import automation 任何東西
    pipeline/
    detectors/
    selection/
    rendering/
    training/
```

`highlight/` 絕對禁止 import `dashboard/`。

違反檢查：
```powershell
Select-String -Pattern '^(from|import)\s+(automation|dashboard)\.' `
  -Path highlight/pipeline/**/*.py, highlight/detectors/**/*.py, `
        highlight/selection/**/*.py, highlight/rendering/**/*.py, `
        highlight/training/**/*.py
# 預期：0 match
```

---

## 重點注意事項（修改前 checklist）

1. **改 `selection/clip_filter.py`（1251 行）前**：對照 [`CUTTING_RULES.md`](CUTTING_RULES.md) 確認每個常數的意義；改完手動跑一場驗證
2. **改 `detectors/yolo_detector.py` 切場規則**：必須同步改 `../automation/workers/live_boundary_builder.py`（mirror 關係，互相 grep `MIRROR FROM`）
3. **改音樂選曲**：BPM 必須維持 120~128 範圍，太慢/太快都會跟剪輯節奏脫節
4. **新增 YOLO 模型**：必須是 `.pt` + `.onnx` 同次匯出
5. **不要在 `selection/` / `rendering/` 直接寫 hardcode 常數**：放進 `config.yaml` 或 `clip_filter.py` 既有的常數區
6. **絕對禁止 emoji 在 stdout**：見 root [`../CLAUDE.md`](../CLAUDE.md) 鐵則 #4

---

## 修改剪輯邏輯後驗證

```powershell
$env:KMP_DUPLICATE_LIB_OK="TRUE"
$py = "$env:USERPROFILE\anaconda3\envs\lol-env\python.exe"

# 拿一場已驗證過的 game 跑，確認 returncode=0 + highlight.mp4 大小合理
& $py -m highlight.main "E:\videos\split\<已驗證單場>.mp4" --skip-split
# 預期：return 0、輸出 .mp4 約 200~500 MB、看一遍確認沒違反 R1~R7
```

如果改的是偵測層 → 必須跑 `--force-rescan` 強制重跑 YOLO，否則會用舊的 `scene.json` cache。

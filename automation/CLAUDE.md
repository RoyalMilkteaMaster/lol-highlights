# automation/ 自動化外層規則

給 AI 看的硬性規則。**任何修改 worker / scheduler / 爬蟲前先看完這份**。
專案結構、子資料夾用途、CLI 用法看 [`README.md`](README.md)。
共用工程鐵則（路徑 / .env / lol-env / 禁 emoji）看 [`../CLAUDE.md`](../CLAUDE.md)。

---

## 目的

- 把 `lolesports.com` 賽程 / YouTube 直播 / Bilibili VOD 自動化轉成「DB 任務 + 已切好的單場 .mp4」
- 餵給 `highlight/main.py` 出 highlight，不需要 user 介入
- 系統設計**穩定性優先於效能**：寧可慢、寧可重試多次，**絕不漏一場**

---

## 架構邊界（絕對不准跨）

```
automation/
├── 全部子模組 ← 可以 lazy import highlight.utils.paths（拿路徑 helper）
│              ← 絕對禁止 import highlight.detectors / selection / rendering / pipeline
│              ← 絕對禁止 import dashboard
│
└── workers/clip_worker.py ← 例外：可以 subprocess.Popen spawn highlight/main.py
```

違反檢查：
```powershell
Select-String -Pattern '^(from|import)\s+highlight\.(detectors|selection|rendering|pipeline)' `
              -Path automation/**/*.py
Select-String -Pattern '^(from|import)\s+dashboard\.' -Path automation/**/*.py
# 預期：0 match（automation/tests/ 內例外）
```

---

## 規則 1：`live_boundary_builder` ↔ `yolo_detector` Mirror

[`workers/live_boundary_builder.py`](workers/live_boundary_builder.py) mirror 了 [`../highlight/detectors/yolo_detector.py`](../highlight/detectors/yolo_detector.py) 的 `get_all_game_boundaries`（BP 聚合 / merge_gap / search 窗口）。

**改其中一邊的切場規則必須同步改另一邊**，否則：
- 離線剪輯（split_vod）切的場
- 在線剪輯（live_split_worker）切的場

會不一致 → 補錄會撞或漏。

互相 grep `MIRROR FROM` 找對應位置。差異點：
- `live_boundary_builder`：取**最晚**訊號 + 15s 收尾（包進 end_graph）
- `yolo_detector.get_all_game_boundaries`：取**最早** + 30s（精確 end）

---

## 規則 2：`live_split_worker` 訊號 + stride

同時跑兩個 YOLO 模型：
- `lol_detector` 抓 `bp_ui` / `game_end_screen` / `nexus_explosion`
- `end_graph_yolov8n` 抓 `end_graph`

`stride_sec: 7.5`（`config.yaml live_split` 區段）對 4hr LCK_Carry 救援率 100%（vs stride=30 只 25%）。

**改 stride 前先回歸測**，否則錄整場救援可能失敗。

狀態機：`IDLE → BP_PHASE → SEARCH_END → COOLDOWN → IDLE`
- `cooldown_duration_sec: 300`（從 600 降到 300，避免 series 2 漏切）

---

## 規則 3：雙顯卡 / 軟體互斥

單卡時 `live_split_worker` 跟 `clip_worker` 會搶 GPU：
- **軟體互斥**：scheduler 的 `pause_when_clip_running` 機制
- **雙卡**：`config.yaml live_split.device: 1` 讓 live_split 用副卡，clip_worker 預設 device 0

`YOLODetector` / `EndGraphDetector` 都接 `device` 參數。

---

## 規則 4：LPL Bilibili VOD 永遠拿最高 bit_rate 1080p

1080p 解析度 ≠ 高品質。同一 BV 不同 part 可能有：
- avc1 (4 Mbps)
- hev1 (1.5 Mbps)
- av01 (1.4 Mbps)

全列為 1080p，bit_rate 差 3 倍。**yt-dlp 預設偏好 av01 反而拿到爛貨**，會導致 YOLO 偵測 kill_feed / end_graph 全失常。

[`workers/lpl_downloader.py`](workers/lpl_downloader.py) selector 鏈：

```
bv*[height>=1080][vcodec~='avc1']+ba   優先 H.264 avc1（Bilibili 上 bit_rate 最高）
/ bv*[height>=1080]+ba                  退而求其次任何 1080p（--format-sort res,tbr）
```

**改 selector 前先 `yt-dlp -F <BV_URL>` 看實際 codecs 跟 bit_rate 分布**。
**沒 1080p 直接 fail**（不接受 720p / 480p）。

---

## 規則 5：所有外部 API 呼叫一定要 sleep

Leaguepedia / hupu / Bilibili / 任何外部 API（含 MediaWiki Cargo / requests / yt-dlp）呼叫之間強制 sleep 1-5 秒：

- **Leaguepedia rate limit**：被 ban 後**任何**新 query 都重置 cool down，要完全停手 1+ 小時才解除
- **hupu / 騰訊 / 火山引擎**：直接 IP block 或返回假資料
- **Bilibili anti-bot**：412 / 352 / 風控 → cookies 失效

絕對禁止：
- debug script 用 `requests.get(...)` 直接連發（必須走 `sources/` 內帶 sleep 的 helper）
- `for/while` loop 內無 sleep 連發 query
- 並行 multi-thread 打同一個 API

正確姿勢：
- 所有外部 API 呼叫包進 `_get_json` / `request_get` helper，內含 `_polite_sleep(base, jitter)` + `tenacity` exp-backoff retry
- disk cache（如 `cache/cargo/` 30 min cache）優先讀
- debug / probe 也走 helper，不可繞過

---

## 規則 6：clip_worker D 策略

`clip_worker` 拿到剛切好的 game 後**不馬上開剪**，先等 Leaguepedia indexing：

| 常數 | 值 | 用途 |
|---|---|---|
| `PRE_FETCH_WAIT_SEC` | `15 * 60` | cut+15min 才第一次 fetch |
| `RETRY_SEC` | `5 * 60` | 失敗 5min 後 retry |
| `CAP_AFTER_CUT_SEC` | `35 * 60` | cut+35min 還沒拿到 → fallback 純 YOLO |

實測：g1 / g2 多在 cut+17min 拿到 RPGI；g3 / 後段約 30% 落到 cap fallback。

---

## 規則 7：三層容錯防線（clip_worker 殭屍保護）

`clip_worker` 每 30s poll 掃殭屍 clip_job：

| 觸發條件 | 動作 |
|---|---|
| pid 死亡 + highlight 影片存在且 >100MB | orphan recover：`mark_done [ORPHAN-OK]` |
| pid 死亡 + highlight 缺 | `mark_zombie` + `broadcast_games.needs_recut=1` |
| `clip_job_<id>.stdout.log` > 30min 沒動 | deadlock：kill tree + `mark_zombie` |
| `run_min > 90` | hard cap：kill tree + `mark_zombie` |

signal handler（`SIGINT/SIGTERM`）→ kill child tree + `mark_zombie` + `sys.exit(0)`。

dashboard 會顯示 `[ZOMBIE]` 警報 + `needs_recut` 補剪建議。

---

## 規則 8：流水編號絕對唯一（14 位 BIGINT）

```
2026 05 04 01 003 2
YYYY MM DD LL NNN G
```

- `LL`：聯賽碼（`config.yaml leagues`）
- `NNN`：當天第幾場 001~999
- `G`：局數 1~9（0 = 系列賽 series_id）

範例：
- `series_id = 20260504010030` — 2026-05-04 LCK 當天第 003 場 BO5 系列賽
- `game_id   = 20260504010032` — 同系列賽的第 2 局

`external_ids` 表沿用既有 series_id；lolesports 後來重排賽程也不會位移。

---

## 規則 9：6 層去重機制（**永遠別繞過**）

| 層 | 機制 |
|---|---|
| 1 | `external_ids` 沿用查找（series_id / team_id 永遠不變） |
| 2 | PK 自身唯一 |
| 3 | `INSERT ... ON DUPLICATE KEY UPDATE` |
| 4 | 複合 UNIQUE：`teams (league_id, code)` / `games (series_id, game_number)` / `external_ids (entity_type, source, external_id)` |
| 5 | status 狀態機**只前進不倒退**（`completed > live > scheduled`） |
| 6 | `updated_at` 自動戳記 |

重複跑 3 次同樣指令 → row count 完全不變、series_id 完全相同。

**修 repositories.py 時不要為了「方便 retry」改成 UPSERT 覆蓋已 completed 狀態 — 會讓資料倒退**。

---

## 規則 10：Polymorphic Reference 由 Repo 程式碼保證

`external_ids.entity_id` 是 polymorphic 欄位，DB 無法用 FK 約束保證 `entity_type='team'` 時 `entity_id` 真的對到 `teams.team_id`。

完整性由 `db/repositories.py` 各 Repo 程式碼保證：**寫 `external_ids` 前，主表的 row 必須已存在**。

---

## 修改前 checklist

1. **改 `scheduler.py`（1135 行）**：必須確認新 cron job 跟現有 6 個 cron 不互相打架（grep `add_job` 看分布）
2. **改 `live_split_worker.py`（946 行）**：必須跟 `live_boundary_builder.py` 同步（見規則 1）
3. **改 `clip_worker.py`（732 行）**：殭屍三層容錯不能拿掉
4. **改 `repositories.py`（1576 行）**：尊重 6 層去重 + status 不倒退
5. **新增爬蟲 source**：必須走帶 sleep 的 helper（規則 5）
6. **新增 worker**：必須寫心跳到 `worker_heartbeats` 表（讓 dashboard 看得到）

---

## 修改 worker 後驗證

```powershell
# 1. import 鎖測試
& $py -c "import automation.workers.clip_worker; import automation.workers.live_split_worker; import automation.scheduler; print('[OK]')"

# 2. 確認改的 worker 跑得起來（30 秒後 Ctrl+C）
& $py -m automation.run --clip-worker

# 3. 確認新 code 有生效：重啟 4 worker
@(pid1, pid2, pid3, pid4) | ForEach-Object { Stop-Process -Id $_ -Force }
.\start.bat

# 4. 開 dashboard 確認心跳活著
# http://localhost:8765
```

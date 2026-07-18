# automation/ — 自動化外層

爬賽程、找直播 URL、錄影、邊錄邊切、寫 DB、排程，最後呼叫 `highlight/main.py` 出 highlight。
**所有常駐進程都在這**。

---

## 全自動流程一覽

```
scheduler（APScheduler 常駐）
  cron 09:00  daily_schedule_scrape (lolesports → DB)
  cron 09:30  daily_find_live (YouTube + Bilibili URL)
  cron 10:00  daily_cookie_check (Bilibili SESSDATA 過期警示)
  比賽 -3min  spawn streamlink_recorder
       │
       ├─ LCK / LCP YouTube live
       │      └ streamlink_recorder → .ts → live_split_worker（邊錄邊切 YOLO）
       │                                       IDLE → BP_PHASE → SEARCH_END → COOLDOWN
       │                                       每場切完 → INSERT broadcast_games + enqueue clip_jobs
       │
       └─ LPL Bilibili VOD
              └ lpl_downloader → yt-dlp + Firefox cookies 找 BV → 下載 1080p
                                  → INSERT broadcast_games + enqueue clip_jobs

clip_worker（常駐）
       └ pop clip_jobs → spawn highlight/main.py（D 策略 + 三層容錯）
                          → F:\lol-highlights\output\<game>_highlights.mp4
```

user 操作只一個：開機跑 `python -m automation.run --schedule`，或雙擊 root `start.bat`。

---

## 子資料夾用途

```
automation/
├── run.py               CLI 入口（dispatcher）
├── pipeline.py          ScraperPipeline：lolesports → DB
├── scheduler.py         APScheduler 常駐進程
├── downloaders.py       yt-dlp 包裝
├── config.yaml          設定唯一來源（聯賽碼 / 排程 / LPL 參數）
├── CLAUDE.md            自動化外層硬性規則
├── NAMING.md            影片命名規則
│
├── workers/             4 個常駐 worker
├── services/            業務邏輯（純函式）
├── sources/             外部資料來源 client
├── recorders/           streamlink + ffmpeg 包裝
├── transformers/        資料轉換（純函式）
├── db/                  MySQL 連線 / repositories / migrations
├── infra/               共用工具（heartbeat / log / cleanup / time）
├── tools/               一次性 CLI 工具（手動補錄 / debug）
└── tests/               unit tests
```

### 每個子資料夾一句話

| 子資料夾 | 一句話 | 主要檔案 |
|---|---|---|
| `workers/` | 4 個常駐 daemon | `clip_worker.py` / `live_split_worker.py` / `lpl_downloader.py` / `live_boundary_builder.py` |
| `services/` | 純函式業務邏輯（從 sources/ 拿料、寫進 DB） | `timeline_anchor.py` / `naming_finalizer.py` / `hupu_sync.py` / `vod_recovery_planner.py` |
| `sources/` | 跟外部 API 對話 | `lolesports.py`（主）/ `youtube_live.py` / `bilibili_live.py` / `bilibili_vod_finder.py` / `hupu_scores.py` / `timeline.py` |
| `recorders/` | 把直播流寫成 .mp4 / .ts | `streamlink_recorder.py`（主）/ `ts_concat.py` / `ffmpeg_utils.py` / `watchdog.py` / `stream_probe.py` |
| `transformers/` | 純函式：dict → dataclass，無副作用 | `id_generator.py` / `title_parser.py` / `broadcast_mapper.py` / `types.py` |
| `db/` | MySQL 連線 + CRUD + 14 個 migration | `connection.py` / `repositories.py` / `schema.sql` / `migrations/` |
| `infra/` | 跨模組共用基礎工具 | `log_setup.py` / `time_utils.py` / `heartbeat.py` / `cleanup.py` / `ntp_check.py` / `process_utils.py` |
| `tools/` | 一次性手動 CLI（給 debug / 補單用） | `recover_broadcast_game.py` / `ingest_youtube_vod.py` / `retry_record.py` / `reset_broadcast.py` / `merge_segments.py` / `games_admin.py` |
| `tests/` | 5 個 unit test | `test_broadcast_mapper.py` / `test_live_*.py` / `test_vod_metadata.py` / `test_yolo_detector_range.py` |

### 設計分層原則

```
sources（拉資料）→ transformers（純函式轉換）→ db.repositories（寫 DB）→ workers（常駐 daemon）→ tools（一次性手動）
```

修錯時順著資料流找層別：
- 資料源頭錯（lolesports / Bilibili API 改格式）→ `sources/`
- 資料轉換錯（隊伍 code / id 算錯）→ `transformers/`
- DB 寫入錯（去重 / status 倒退）→ `db/repositories/`
- 常駐 worker 行為錯 → `workers/`

---

## CLI 全景

```powershell
$py = "$env:USERPROFILE\anaconda3\envs\lol-env\python.exe"
$env:KMP_DUPLICATE_LIB_OK = "TRUE"

# ── 一次性建表 / migration ────────────────────────
& $py -m automation.run --init-db        # 建表（執行 schema.sql）
& $py -m automation.run --migrate        # 跑未執行的 migration

# ── 賽程 / 直播 URL（cron 內部會做）────────────────
& $py -m automation.run --leagues LCK,LPL --days-ahead 14
& $py -m automation.run --find-live --leagues LCK,LCP

# ── 常駐進程（背景跑）─────────────────────────────
& $py -m automation.run --schedule       # APScheduler（推薦：開機跑這個）
& $py -m automation.run --clip-worker    # clip_jobs consumer
& $py -m automation.run --live-split     # 邊錄邊切（通常 scheduler 會 spawn）

# ── 手動觸發單一動作（debug / 補單）──────────────
& $py -m automation.run --record <broadcast_id>
& $py -m automation.run --retry-record <broadcast_id>
& $py -m automation.run --reset-broadcast <broadcast_id>
& $py -m automation.run --merge-segments <broadcast_id>
& $py -m automation.run --download <broadcast_id>
& $py -m automation.run --lpl-download <broadcast_id>
& $py -m automation.run --cleanup --dry-run
```

`live_split` 旗標：`--once`（跑一輪退出）/ `--no-cut`（寫 DB 不切）/ `--no-enqueue`（切片但不 enqueue）。

---

## 快速開始

### 1. 建 MySQL 帳號 + DB
```sql
CREATE DATABASE lol_highlight CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'lol_crawler'@'localhost' IDENTIFIED BY '<password>';
GRANT ALL ON lol_highlight.* TO 'lol_crawler'@'localhost';
FLUSH PRIVILEGES;
```

### 2. 寫 root `/.env`
```
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=<帳號>
MYSQL_PASSWORD=
MYSQL_DB=lol_highlight
YOUTUBE_API_KEY=
```

### 3. 裝依賴
```powershell
python -m pip install -r requirements.txt
```

### 4. 一次性建表
```powershell
& $py -m automation.run --init-db
& $py -m automation.run --migrate
```

### 5. Bilibili Firefox cookies（LPL 用）

LPL 拉 Bilibili 官方 VOD，**必須**用 Firefox（Chrome cookie db 鎖檔 / Edge DPAPI 解不出）。

1. 開 Firefox → 登 https://www.bilibili.com/ → 勾「6 個月免登入」
2. cookies 自動存 `~/AppData/Roaming/Mozilla/Firefox/Profiles/<xxx>.default-release/cookies.sqlite`
3. yt-dlp 用 `--cookies-from-browser firefox` 自動讀

cookies 過期前 7 天會跳 Windows toast（cron 每天 10:00 檢查）。

### 6. 啟動
```powershell
.\start.bat
```
第一次執行會安裝並啟動 4 個 Windows 工作排程器 task；之後每次登入自動啟動，每分鐘 watchdog trigger 會補起異常退出的程序。用 `.\start.bat status|stop|start|restart|uninstall` 管理。

---

## 關鍵設定（`automation/config.yaml`）

最常需要改的幾個：

```yaml
scheduler:
  schedule_scrape_at: "09:00"                 # 每天爬賽程的時間
  find_live_at: "09:30"                       # 每天找直播 URL
  find_live_leagues: "LCK,LCP"                # LPL 不在這（走 lpl_downloader）
  schedule_recording_lead_minutes: 3          # 比賽前 N 分鐘啟錄影

  # LPL 流程
  lpl_post_match_offset_minutes: 80           # 比賽 + 80min 才找 BV
  lpl_max_retries: 12                         # 12 次 = 1 小時上限
  lpl_part_min_duration_sec: 1800             # 30 分鐘以下視為採訪
  lpl_cookies_from_browser: "firefox"         # 不能改 chrome / edge

  cookie_check_at: "10:00"
  cookie_alert_threshold_days: 7

live_split:
  poll_interval_sec: 300                      # 5 分鐘掃一次 .ts
  stride_sec: 7.5                             # YOLO 採樣間隔（不要亂改，回歸測過）
  cooldown_duration_sec: 300                  # 一場切完 cooldown（從 600 降到 300，避免 series 2 漏切）

cleanup:
  enabled: true
  daily_at: "03:00"
  lol_vods_keep_days: 3
  live_recordings_keep_days: 3
  lol_games_vods_keep_days: 3
  output_final_keep_days: 28
```

完整參數說明見 `config.yaml` 內註解。

---

## 想了解更多？

| 主題 | 文件 |
|---|---|
| 自動化外層硬性規則（worker / scheduler / 爬蟲限速 / 雙顯卡） | [`CLAUDE.md`](CLAUDE.md) |
| 影片命名規則（檔名格式 / broadcast_date 推算） | [`NAMING.md`](NAMING.md) |
| 共用工程鐵則（路徑 / .env / lol-env / emoji） | [`../CLAUDE.md`](../CLAUDE.md) |
| 完整開發紀錄 | [`../reports/開發流程記錄.docx`](../reports/開發流程記錄.docx) |

---

## 從 Python 回讀資料的最簡範例

```python
from automation.db.connection import mysql_conn

with mysql_conn as conn:
    with conn.cursor as cur:
        cur.execute("""
            SELECT s.match_code, s.match_date, ta.code AS team_a, tb.code AS team_b, s.status
            FROM series s
            JOIN teams ta ON s.team_a_id = ta.team_id
            JOIN teams tb ON s.team_b_id = tb.team_id
            WHERE s.match_date = %s AND s.league_id = %s
            ORDER BY s.match_time
        """, ('2026-05-20', 1))
        for row in cur.fetchall:
            print(row)
```

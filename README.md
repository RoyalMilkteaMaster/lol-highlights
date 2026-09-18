# LoL 比賽精華自動剪輯系統

這是一個英雄聯盟職業比賽精華的自動剪輯系統。

本專案從賽程與直播來源建立錄影工作，使用 YOLO 偵測 BP、擊殺與遊戲結束畫面，配合演算法得出比賽高光權重，最後以 FFmpeg 剪輯，輸出單場精華。專案包含手動剪輯、自動錄影、工作排程與唯讀監控儀表板。

目前主要支援 Windows 10/11、Python 3.10、MySQL 8、NVIDIA CUDA GPU 與 FFmpeg。
剪輯一場影片約耗時20~30分鐘

## 架構

```text
lolesports / YouTube / Bilibili / Hupu
                   |
                   v
automation/  -> MySQL -> clip_jobs
                   |
                   v
highlight/   -> split -> YOLO scan -> FFmpeg render
                   |
                   v
              OUTPUT_DIR/final/

dashboard/ 只讀取 automation 與 highlight 的狀態
```

三個子系統維持單向依賴：

- `highlight/`：影片分割、偵測、選段與渲染。
- `automation/`：賽程、直播、錄影、排程、資料庫與 worker。
- `dashboard/`：本機唯讀監控，預設 `http://127.0.0.1:8765/`。

## 快速開始（一鍵安裝）

需求：Windows 10/11 x64、NVIDIA 顯卡 + 已裝 driver、約 20 GB 磁碟空間、網路。
其他東西（Miniconda、Python 3.10 環境、ffmpeg、MySQL、YOLO 模型）腳本會自己裝。

```powershell
git clone https://github.com/RoyalMilkteaMaster/lol-highlights.git
Set-Location .\lol-highlights
.\setup.ps1
```

腳本會問 4 個問題（直接 Enter 用預設值），然後跳一次 UAC，接著全自動，約 20–40 分鐘：

| 問題 | 預設 |
|---|---|
| 影片輸入資料夾 | `<專案>ideos` |
| 剪輯輸出資料夾 | `<專案>\output` |
| MySQL 資料目錄（主程式固定裝在 `C:\Program Files\MySQL\MySQL Server 8.0`） | `<專案>\data\mysql` |
| YouTube API key／要不要看 LPL | 空／不要 |

> **沒有 YouTube API key 和 Bilibili 登入，系統無法即時接直播自動錄影剪輯。**
> 你只能自己下載好比賽影片、放進 `videos\manual_inbox`（或直接下指令），再由系統剪精華。
> 兩者都可以之後再補：key 填進 `.env` 的 `YOUTUBE_API_KEY`（取得：[Google Cloud Console](https://console.cloud.google.com/apis/credentials) → 建立憑證 → API 金鑰）；LPL 只要裝 Firefox 並用它登入 bilibili.com，系統會自動讀登入 cookie。

最後看到 `通過 7/7 — 環境健康，可以開工` 就完成。中途失敗：看 `deployment\setup_log.txt` 最後幾行，修正後**重跑 `.\setup.ps1`**，做過的步驟會自動跳過。

安裝完會有這些東西：

- `lol-env`：在 `%USERPROFILE%naconda3` 或 `miniconda3` 底下（沒 conda 的機器會裝 Miniconda）
- MySQL 8.0：程式在 `C:\Program Files\MySQL\MySQL Server 8.0`、資料在你選的目錄、服務 `MySQL80` 開機自動啟動；應用程式帳密隨機產生寫進 `.env`，root 密碼在 `deployment\mysql_root_password.txt`
- ffmpeg / ffprobe：`toolsfmpeg\`
- YOLO 模型：從 GitHub Release 下載到 `highlightssets\yolo_models\`，用 `SHA256SUMS` 校驗
- 設定檔：`.env`、`automation\config.yaml`、`highlight\config.yaml`（從對應的 `*.example` 複製）

### 沒顯卡的機器（只跑爬蟲）

```powershell
.\setup.ps1 -SkipGpuCheck
```

環境健診會略過 CUDA 與 YOLO GPU 兩項；賽程爬蟲、資料庫、儀表板照常可用，剪輯不行。

### 重新健診

```powershell
deployment\check_env.bat              # 沒顯卡加 --skip-gpu
```

### BGM

音樂檔因避免版權問題不進 Git，`highlightssets\music` 只附一首參考曲（NCS 公開授權）。
若要將精華加上完整曲庫，請用 `highlight/assets/music_urls.txt` 自行下載：

```powershell
python -m highlight.rendering.music_library download --urls .\highlightssets\music_urls.txt --out .\highlightssets\music
python -m highlight.rendering.music_library scan --dir .\highlightssets\music
```

使用音樂前請自行確認每首曲目的授權與署名要求。

<details>
<summary>手動安裝（不用 setup.ps1）</summary>

1. 安裝 Anaconda/Miniconda、MySQL 8、FFmpeg、NVIDIA driver。
2. `conda env create -f .\deployment\environment.yml`
3. 複製 `.env.example` → `.env`、`automation/config.example.yaml` → `automation/config.yaml`、`highlight/config.example.yaml` → `highlight/config.yaml`，填 `.env` 的 MySQL 帳密與路徑。
4. MySQL 建帳號：`CREATE USER 'lol_app'@'localhost' IDENTIFIED BY '...'; GRANT ALL ON lol_highlight.* TO 'lol_app'@'localhost';`
5. `python -m automation.run --init-db` 然後 `python -m automation.run --migrate`
6. 從 GitHub Release `models-v1` 下載四個 `.pt` 到 `highlight/assets/yolo_models/`，對照 `SHA256SUMS`。
7. `deployment\check_env.bat` 看到 7/7。

</details>

## 使用方式

### 手動剪輯一場 VOD

```powershell
$env:KMP_DUPLICATE_LIB_OK = "TRUE"
python -m highlight.main "C:\path\to\game.mp4" --skip-split
```

輸出位置由 `OUTPUT_DIR` 決定。

### 啟動自動化

```powershell
.\start.bat
```

第一次執行會安裝並啟動四個 Windows Scheduled Tasks：

- scheduler
- clip worker
- live split worker
- dashboard

管理指令：

```powershell
.\start.bat status
.\start.bat stop
.\start.bat start
.\start.bat restart
.\start.bat uninstall
```

### 控制允許執行時段

在 `automation/config.yaml` 設定：

```yaml
automation_window:
  enabled: true
  timezone: "Asia/Taipei"
  days: [mon, tue, wed, thu, fri, sat, sun]
  start: "09:00"
  end: "23:00"
```

也可以臨時暫停或恢復：

```powershell
python -m automation.run --automation-status
python -m automation.run --pause-system --pause-reason "maintenance"
python -m automation.run --pause-until "2026-07-18T22:00" --pause-reason "maintenance"
python -m automation.run --resume-system
```

## 哪些東西不在 Git 裡

| 東西 | 從哪來 |
|---|---|
| YOLO 模型權重（4 個 `.pt`） | GitHub Release [`models-v1`](https://github.com/RoyalMilkteaMaster/lol-highlights/releases/tag/models-v1)，`setup.ps1` 自動下載並用 `SHA256SUMS` 校驗 |
| BGM 曲庫 | 版權考量只附一首參考曲，其餘用 `music_urls.txt` 自行下載 |
| `.env`、`automation/config.yaml`、`highlight/config.yaml` | `setup.ps1` 從 `*.example` 產生，內含你的帳密與路徑 |
| MySQL 資料、影片、輸出、log、瀏覽器 cookies | 本機產生 |

這些檔案都在 `.gitignore`，不會被 commit；CI 的 `tests` workflow 也會擋下誤加的機密與大檔。

## 關於本專案

這是非官方 fan project。若有侵權行為煩請告知。

## 進一步文件

- [`highlight/README.md`](highlight/README.md)：剪輯流程與模型角色。
- [`automation/README.md`](automation/README.md)：排程、錄影、資料庫與 worker。
- [`automation/NAMING.md`](automation/NAMING.md)：影片與資料夾命名規則。
- [`AGENTS.md`](AGENTS.md)：架構邊界與開發規則。

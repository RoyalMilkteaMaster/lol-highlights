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

## 快速開始

### 1. 建立環境

安裝 Anaconda、MySQL 8、Git、FFmpeg 與支援 CUDA 的 NVIDIA driver，然後在 PowerShell 執行：

```powershell
git clone <your-repository-url> lol-highlights
Set-Location .\lol-highlights

conda env create -f .\deployment\environment.yml
conda activate lol-env
```

`deployment/environment.yml` 是目前已驗證的完整 Windows/CUDA 環境。若只更新 Python 套件，可使用：

```powershell
python -m pip install -r .\requirements.txt
```

### 2. 建立本機設定

```powershell
Copy-Item .\.env.example .\.env
Copy-Item .\automation\config.example.yaml .\automation\config.yaml
Copy-Item .\highlight\config.example.yaml .\highlight\config.yaml
```

編輯 `.env`，至少設定 MySQL 帳密。資料目錄與 FFmpeg 位置也由 `.env` 控制：

```dotenv
VIDEO_DIR=C:/lol-highlights-data/videos
OUTPUT_DIR=C:/lol-highlights-data/output
FFMPEG_BIN=
```

`FFMPEG_BIN` 留空代表 `ffmpeg` 與 `ffprobe` 已在 `PATH`。

### 3. 建立資料庫

先用 MySQL 管理者帳號授權應用程式帳號：

```sql
CREATE USER 'lol_crawler'@'localhost' IDENTIFIED BY 'your-password';
GRANT ALL ON lol_highlight.* TO 'lol_crawler'@'localhost';
FLUSH PRIVILEGES;
```

讓 `.env` 中的帳密與上面一致，再執行：

```powershell
$env:KMP_DUPLICATE_LIB_OK = "TRUE"
python -m automation.run --init-db
python -m automation.run --migrate
```

### 4. 安裝模型與音樂

四個 YOLO 權重不放進 Git history。檔名、大小與 SHA-256 位於 [`highlight/assets/yolo_models/README.md`](highlight/assets/yolo_models/README.md)。確認有再散布權後，從專案的 GitHub Release 下載並放進該目錄。

音樂檔因避免版權問題，同樣不進 Git。
若要將精華加上音樂，請自行至 `highlight/assets/music_urls.txt` 建立本機音樂庫：

```powershell
python -m highlight.rendering.music_library download `
  --urls .\highlight\assets\music_urls.txt `
  --out .\highlight\assets\music

python -m highlight.rendering.music_library scan `
  --dir .\highlight\assets\music
```

### 5. 驗證

```powershell
python -m unittest discover -s .\automation\tests -v
python .\deployment\_check_env_runner.py
```

環境檢查會驗證 Python、CUDA/PyTorch、FFmpeg、MySQL、資料目錄、四個 YOLO 模型與 GPU inference。

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

## 本專案並不包含yolo模型以及

- `.env`
- `**/config.yaml`
- browser cookies
- `deployment/db_dump_*.sql`
- YOLO `.pt` / `.onnx` / `.engine` 權重



## 關於本專案

這是非官方 fan project。若有侵權行為煩請告知。

## 進一步文件

- [`highlight/README.md`](highlight/README.md)：剪輯流程與模型角色。
- [`automation/README.md`](automation/README.md)：排程、錄影、資料庫與 worker。
- [`automation/NAMING.md`](automation/NAMING.md)：影片與資料夾命名規則。
- [`AGENTS.md`](AGENTS.md)：架構邊界與開發規則。

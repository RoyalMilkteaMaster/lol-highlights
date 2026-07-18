---

## 硬性鐵則（必須遵守，不能討價還價）

### 1. 路徑用 `Path(__file__).parent` 為基準
- 不寫死 `c:\Users\...`
- 不依賴 CWD
- 跨平台路徑請用 `highlight.utils.paths` 提供的 helper：
  - `split_dir() / scan_dir() / finals_dir() / final_dir()`
  - `live_recordings_dir() / lol_games_vods_dir() / manual_inbox_dir()`
  - `videos_dir() / output_dir()`（受 `VIDEO_DIR` / `OUTPUT_DIR` env 覆寫）

### 2. `.env` / `config.yaml` 不寫死
- 所有密鑰從 root `/.env` 讀（已 gitignore）
- `MYSQL_HOST/PORT/USER/PASSWORD/DB` / `ROBOFLOW_API_KEY` / `YOUTUBE_API_KEY`（可選）
- `**/config.yaml` 全部 gitignore，不要把密碼 commit 上去

### 3. 唯一正確的 Python：lol-env
- Windows 預設路徑：`$env:USERPROFILE\anaconda3\envs\lol-env\python.exe`
- PyTorch 2.11.0+cu128 / NVIDIA CUDA GPU / CUDA 12.8
- **base 環境會炸 YOLO**（CPU-only PyTorch → `Invalid CUDA device=0`）
- 必設環境變數：`KMP_DUPLICATE_LIB_OK=TRUE`（避 MKL/OpenMP 衝突）

### 4. 絕對禁止 emoji / arrow 在 stdout 路徑
Windows console 預設 `cp950`，`print` / `f-string` / `logger.msg(...)` 內含 emoji 會 throw `UnicodeEncodeError`、**整個進程死掉**。

- 禁止：`✓ ❌ → ⭐ ✅ ⚠️ 🎉 🔴 🟡 🟢` 等任何 emoji / 全形 arrow
- 替代：`[OK]` / `[X]` / `->` / `[!]` / `[WARN]` / `[FAIL]`
- **OK 的地方**：docstring / 註解 / dashboard HTML 字串（不會跑到 console stdout）
- 全 codebase 檢查：
  ```powershell
  Select-String -Pattern '✓|❌|→|⭐|✅|⚠️' -Path **/*.py
  ```

---

## 架構邊界（絕對不准跨）

三個資料夾各自獨立、單向依賴：

```
         ┌──────────────┐
         │ dashboard/   │  讀 highlight + automation（中性 read-only）
         └──┬────────┬──┘
            ↓        ↓
   ┌────────┴───┐  ┌─┴──────────┐
   │ highlight/ │  │ automation/│
   │            │  │            │
   │  剪輯邏輯  │  │ 爬蟲/排程  │
   └─────┬──────┘  └─────┬──────┘
         │               │
         │   spawn(...)  │
         │←──────────────┘
         │  clip_worker → highlight/main.py
         │
         ↓
    F:/lol-highlights/output/
```

### 允許跨界
- `automation/*` 可 lazy import `highlight.utils.paths`（用 helper 拿路徑）
- `automation/*` 可 lazy import `highlight` 公開的 detector factory；不准 import `highlight.detectors.*` 內部模組
- `highlight/utils/vod_metadata.py` 可 lazy import `automation.db.connection`（給 `main.py --from-game/--from-broadcast` 用）
- `dashboard/*` 可 import `highlight.utils.*` 跟 `automation.db.connection`（讀資料用）
- `automation/workers/clip_worker.py` spawn `highlight/main.py` 當子程序（subprocess.Popen）

### 絕對禁止跨界
- `highlight/detectors/` / `selection/` / `rendering/` / `training/` **永遠不准** import `automation`
- `highlight/` 任何子模組不准 import `dashboard`
- `automation/` 任何子模組不准 import `dashboard`
- `automation/` 不准直接 import `highlight.detectors.*`

違反檢查：
```powershell
& "$env:USERPROFILE\anaconda3\envs\lol-env\python.exe" `
  -m unittest automation.tests.test_architecture_boundaries
```

---

## 寫程式風格

### 模組化
1. 每個函式只負責一件事
2. 主流程放在 `main()`
3. 輸入、處理、輸出分開
4. 重複邏輯抽成函式
5. 程式變大才拆檔案 — 但**不要過度拆**，保持專案整潔易讀

### 註解
- 加**簡單**註解（不寫大段 docstring）
- 能 self-documenting 就好（變數名 / 函式名清楚一點）
- 不要寫顯而易見的東西（如 `# i = 0`）

### 新增檔案
- 若沒有必要不要新增 .py 檔
- 一次性 debug script 放 `_tmp/`（gitignored），用完就刪
- 不要為了「將來可能需要」而預先抽象

### 回應
- 用中文回應 user

---

## 修改 / 新增 / 刪除前的檢查清單

1. 你改的東西有沒有跨 `highlight/ ↔ automation/` 邊界？→ 看上面「架構邊界」
2. 程式內有用到絕對路徑嗎？→ 改成 `paths.xxx_dir()` helper
3. 有 emoji 嗎？→ 用 `[OK]` / `[X]` 替代
4. 有跑 `lol-env` Python 嗎？→ 不是會炸
5. 有 grep 確認沒漏改舊路徑嗎？→ 跑：
   ```powershell
   Select-String -Pattern 'web_scraper|debug_tools|core\.utils|core\.paths' -Path **/*.py
   # 預期：0 match（reports/_archive/ 除外）
   ```
6. 改完 worker 程式有重啟 worker 嗎？→ Stop-Process + `.\start.bat`

---

## 子系統規約（讀對應子系統時請進去看）

| 子系統 | 規約檔 | 重點 |
|---|---|---|
| [highlight/](highlight/CLAUDE.md) | `highlight/CLAUDE.md` | 7 條剪輯鐵律、4 個 YOLO 模型、音樂 BPM、`.onnx` 對齊 |
| [automation/](automation/CLAUDE.md) | `automation/CLAUDE.md` | scheduler / D 策略 / 雙顯卡 / Bilibili 1080p / 爬蟲必 sleep |
| [dashboard/](dashboard/CLAUDE.md) | `dashboard/CLAUDE.md` | Flask / CORS / port 8765 / 只讀不寫 |

完整開發歷史（不需要每次讀）：[`reports/開發流程記錄.docx`](reports/開發流程記錄.docx)。

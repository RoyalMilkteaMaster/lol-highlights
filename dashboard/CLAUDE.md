# dashboard/ — 監控工具

看 `highlight/` 跟 `automation/` 兩邊狀態的 stdlib HTTP server + HTML viewer。
中性區塊，不參與剪輯邏輯，純讀 log + DB 顯示。

共用工程鐵則見 root [`../CLAUDE.md`](../CLAUDE.md)。

---

## 子模組

```
dashboard/
├── watch_progress.py     唯讀 HTTP server（http://127.0.0.1:8765）
├── index.html            監控首頁
├── timeline_viewer.py    產生 timeline_viewer.html
└── timeline_viewer.html  剪輯結果視覺化（事件 / 選段 / 鐵則違規一目瞭然）
```

---

## 規約

### 只讀不寫
- 只 `SELECT` DB，不寫入
- 只 read log file，不寫 log（自己的 log 寫到 `_tmp/logs/dashboard.log`）
- 例外：`timeline_viewer.py` 寫 `timeline_viewer.html`（純檔案輸出，不動 DB）

### Port 8765
固定。雙擊根目錄 `儀表板.html` 會導向 `http://127.0.0.1:8765/`。

### 啟動
```powershell
python -m dashboard.watch_progress
```

或透過 root `start.bat` 一鍵啟（會跟 3 個 automation worker 一起起）。

---

## 跨界 import

dashboard 是中性區塊，可中立 import 兩邊：
- `from highlight.utils import paths`
- `from highlight.utils.scene_view import EventStore`（給 timeline_viewer）
- `from automation.db.connection import mysql_conn`（讀 worker 狀態 / clip_jobs）

但反向不行：`highlight/` 跟 `automation/` 都不准 import `dashboard`。

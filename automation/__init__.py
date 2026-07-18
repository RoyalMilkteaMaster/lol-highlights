"""LoL 賽程爬蟲 + 直播 URL 偵測 + 自動錄影 + MySQL 整合。

【根目錄】放 main 級模組（CLI 入口 / 常駐進程 / 主流程）+ 單檔模組：
- run.py         : CLI 入口
- pipeline.py    : scrape 主流程串接（含 find_live）
- scheduler.py   : APScheduler 常駐進程
- downloaders.py : yt-dlp 下載包裝（單檔）

【子資料夾】按功能分類：
- sources/       : 資料來源（lolesports / youtube_live / bilibili_live / bilibili_vod_finder / hupu_scores / timeline）
- transformers/  : 資料轉換（id_generator / title_parser / broadcast_mapper / types）
- db/            : MySQL 連線、Schema、Repository、Migration
- recorders/     : streamlink 錄影 + watchdog + ts_concat
- workers/       : 常駐 worker（clip_worker / live_split_worker / lpl_downloader / live_boundary_builder）
- services/      : 業務邏輯（timeline_anchor / naming_finalizer / hupu_sync / vod_recovery_planner）
- tools/         : 手動補錄 / reset / merge 等運維 CLI
- infra/         : 共用 helper（log_setup / time_utils / ntp_check / cleanup / heartbeat / process_utils）
- tests/         : unit tests

CLI 範例：
    python -m automation.run --leagues LCK,LPL --days-ahead 14
    python -m automation.run --find-live --leagues LCK,LCP,LPL
    python -m automation.run --extract-metadata "<vod 路徑>"
    python -m automation.run --download <broadcast_id>
    python -m automation.run --schedule           # 排程常駐進程
    python -m automation.run --clip-worker        # 剪輯 worker

詳見 automation/README.md。
"""

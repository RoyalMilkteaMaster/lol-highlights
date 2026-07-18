"""CLI 入口（automation 各功能 dispatcher）。

執行範例：

    # 一次性建表
    python -m automation.run --init-db

    # 跑 db/migrations/*.sql 中尚未執行的 migration
    python -m automation.run --migrate

    # 拉 LCK + LPL 未來 14 天賽程
    python -m automation.run --leagues LCK,LPL --days-ahead 14

    # 找 LCK / LCP / LPL 直播 URL
    python -m automation.run --find-live --leagues LCK,LCP,LPL

    # 對 VOD 檔做 metadata 反查
    python -m automation.run --extract-metadata "E:\\videos\\lol_vods\\xxx.mp4"

    # 從 broadcast_id 用 yt-dlp 下載
    python -m automation.run --download <broadcast_id>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")


def _setup_logging(log_name: str = "scraper") -> None:
    """設定 root logger：同時輸出終端機與每日輪替檔案。"""
    log_dir = Path(__file__).resolve().parent.parent / "_tmp" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{log_name}.log"

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    formatter = logging.Formatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # 清掉舊 handler 避免重複設定
    root.handlers.clear()

    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(formatter)
        root.addHandler(sh)

    fh = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        backupCount=14,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    fh._rotating_log_setup = True
    root.addHandler(fh)

    # APScheduler 每個 job 的開始/成功訊息沒有操作價值，只保留 warning/error。
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="automation",
        description="LoL 賽程爬蟲 + 直播 URL 偵測（lolesports / YouTube / Bilibili -> MySQL）",
    )

    # 既有指令
    parser.add_argument(
        "--init-db",
        action="store_true",
        help="一次性建表（執行 schema.sql）",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="跑 db/migrations/*.sql 中尚未執行過的 migration",
    )
    parser.add_argument(
        "--leagues",
        type=str,
        default="LCK",
        help="要爬的聯賽 code，逗號分隔（例：LCK,LPL,LEC）",
    )
    parser.add_argument(
        "--days-ahead",
        type=int,
        default=14,
        help="抓未來幾天的賽程（預設 14）",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=0,
        help="抓過去幾天的賽程（預設 0）",
    )

    parser.add_argument(
        "--find-live",
        action="store_true",
        help="找直播 URL（YouTube + Bilibili），對應到 broadcasts + broadcast_series",
    )
    parser.add_argument(
        "--extract-metadata",
        type=str,
        default=None,
        metavar="VOD_PATH",
        help="對 VOD 檔做 metadata 反查（debug 用），印 JSON",
    )
    parser.add_argument(
        "--download",
        type=int,
        default=None,
        metavar="BROADCAST_ID",
        help="從 DB 拉 broadcast 用 yt-dlp 下載到 lol_vods/",
    )
    # 路徑 lazy 解析（從 highlight.utils.paths 拿，VIDEO_DIR env override）
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from highlight.utils import paths as _paths
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(_paths.lol_vods_dir()),
        help=f"--download 輸出資料夾（預設 {_paths.lol_vods_dir()}）",
    )

    parser.add_argument(
        "--record",
        type=int,
        default=None,
        metavar="BROADCAST_ID",
        help="手動觸發 streamlink 錄影",
    )
    parser.add_argument(
        "--record-output-dir",
        type=str,
        default=str(_paths.live_recordings_dir()),
        help=f"--record 輸出目錄（預設 {_paths.live_recordings_dir()}）",
    )
    parser.add_argument(
        "--record-test-mode",
        action="store_true",
        help="--record 測試模式（跳過 min_valid_duration 檢查）",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="啟動 APScheduler 常駐進程",
    )
    parser.add_argument(
        "--clip-worker",
        action="store_true",
        help="啟動 clip_worker 常駐進程",
    )
    parser.add_argument(
        "--live-split",
        action="store_true",
        help="啟動 live_split_worker 邊錄邊切（YOLO + boundary builder）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="搭配 --live-split：跑一輪就退（debug 用）",
    )
    parser.add_argument(
        "--no-cut",
        action="store_true",
        help="搭配 --live-split：寫 DB 但不跑 ffmpeg cut",
    )
    parser.add_argument(
        "--no-enqueue",
        action="store_true",
        help="搭配 --live-split：切片但不 enqueue clip_jobs",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="手動觸發 cleanup（搭配 --dry-run）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="搭配 --cleanup 使用：只印該刪清單，不實際刪",
    )
    parser.add_argument(
        "--retry-record",
        type=int,
        default=None,
        metavar="BROADCAST_ID",
        help="重置狀態並直接補錄",
    )
    parser.add_argument(
        "--reset-broadcast",
        type=int,
        default=None,
        metavar="BROADCAST_ID",
        help="完全清掉 broadcast 狀態（含 lock + clip_jobs）",
    )
    parser.add_argument(
        "--merge-segments",
        type=int,
        default=None,
        metavar="BROADCAST_ID",
        help="對中斷 broadcast 的 ts 段手動合併成 mp4",
    )
    parser.add_argument(
        "--lpl-download",
        type=int,
        default=None,
        metavar="BROADCAST_ID",
        help="手動觸發 LPL downloader（從 Bilibili 找官方剪好的 BV 下載）",
    )

    control = parser.add_mutually_exclusive_group()
    control.add_argument(
        "--pause-system",
        action="store_true",
        help="暫停 scheduler 與 worker 撈新工作（既有工作會跑完）",
    )
    control.add_argument(
        "--pause-until",
        type=str,
        metavar="YYYY-MM-DDTHH:MM",
        help="暫停到指定的 Asia/Taipei 時間後自動恢復",
    )
    control.add_argument(
        "--resume-system",
        action="store_true",
        help="立刻解除手動暫停",
    )
    control.add_argument(
        "--automation-status",
        action="store_true",
        help="顯示目前是否允許自動化執行與下一次切換時間",
    )
    parser.add_argument(
        "--pause-reason",
        type=str,
        default="手動暫停",
        help="搭配 --pause-system / --pause-until 記錄原因",
    )

    return parser.parse_args()


# ── 各個子指令 ─────────────────────────────────────────────────────────────
def _run_init_db() -> None:
    from automation.db.init_db import init_database
    init_database()
def _run_migrate() -> None:
    from automation.db.init_db import run_migrations
    run_migrations()
def _run_scrape_schedule(args: argparse.Namespace) -> None:
    from automation.pipeline import ScraperPipeline

    leagues = [c.strip().upper() for c in args.leagues.split(",") if c.strip()]
    pipeline = ScraperPipeline()
    pipeline.run(
        league_codes=leagues,
        days_ahead=args.days_ahead,
        days_back=args.days_back,
    )


def _run_find_live(args: argparse.Namespace) -> None:
    """新指令：找 YT/BL 直播 URL，寫入 broadcasts + broadcast_series。"""
    from automation.pipeline import ScraperPipeline

    leagues = [c.strip().upper() for c in args.leagues.split(",") if c.strip()]
    pipeline = ScraperPipeline()
    pipeline.find_live(leagues=leagues, days_ahead=args.days_ahead)


def _run_extract_metadata(vod_path: str) -> None:
    """新指令：對單個 VOD 做 metadata 反查，印 JSON。"""
    from dataclasses import asdict
    from datetime import date

    from highlight.utils.vod_metadata import extract_metadata

    p = Path(vod_path)
    if not p.is_file():
        print(f"找不到檔案：{p}", file=sys.stderr)
        sys.exit(1)

    meta = extract_metadata(p)

    # date 物件不能 JSON 序列化，手動處理
    def _serialize(obj):
        if isinstance(obj, date):
            return obj.isoformat()
        return str(obj)

    payload = asdict(meta)
    payload["match_date"] = _serialize(meta.match_date) if meta.match_date else None
    payload["_vod_path"] = str(p)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_serialize))


def _run_download(broadcast_id: int, output_dir: str) -> None:
    """新指令：從 broadcast_id 用 yt-dlp 下載。"""
    from automation.downloaders import download_broadcast

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = download_broadcast(broadcast_id, out_dir)
    print(f"OK 下載完成：{result}")


# ── 子指令 dispatch ───────────────────────────────────────────────────────
def _run_record(broadcast_id: int, output_dir: str, test_mode: bool) -> None:
    from automation.recorders.streamlink_recorder import record_broadcast
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    record_broadcast(broadcast_id, out, test_mode=test_mode)


def _run_schedule() -> None:
    from automation.scheduler import run_scheduler
    run_scheduler()
def _run_clip_worker() -> None:
    from automation.workers.clip_worker import run_worker
    run_worker()
def _run_live_split(args: argparse.Namespace) -> None:
    from automation.workers.live_split_worker import run_worker as run_live
    run_live(
        once=args.once,
        dry_run=args.dry_run,
        no_cut=args.no_cut,
        no_enqueue=args.no_enqueue,
    )


def _run_cleanup(dry_run: bool) -> None:
    from automation.infra.cleanup import cleanup_old_files
    cleanup_old_files(dry_run_override=dry_run)


def _run_retry_record(broadcast_id: int, output_dir: str) -> None:
    from automation.tools.retry_record import retry_record
    retry_record(broadcast_id, Path(output_dir))


def _run_reset_broadcast(broadcast_id: int) -> None:
    from automation.tools.reset_broadcast import reset_broadcast
    reset_broadcast(broadcast_id)


def _run_merge_segments(broadcast_id: int) -> None:
    from automation.tools.merge_segments import merge_segments
    merge_segments(broadcast_id)


def _run_lpl_download(broadcast_id: int) -> None:
    """手動觸發 LPL downloader（也是 scheduler subprocess 跑這個）。"""
    from automation.workers.lpl_downloader import run_for_broadcast
    run_for_broadcast(broadcast_id)


def _run_automation_control(args: argparse.Namespace) -> bool:
    from automation.infra.control import (
        automation_status,
        parse_pause_until,
        pause_system,
        resume_system,
    )

    if args.pause_system:
        pause_system(reason=args.pause_reason)
        print(f"[OK] automation paused: {args.pause_reason}")
        return True
    if args.pause_until:
        until = parse_pause_until(args.pause_until)
        if until <= datetime.now(until.tzinfo):
            raise SystemExit("--pause-until 必須晚於現在")
        pause_system(until=until, reason=args.pause_reason)
        print(f"[OK] automation paused until {until.isoformat()}: {args.pause_reason}")
        return True
    if args.resume_system:
        changed = resume_system()
        print("[OK] automation resumed" if changed else "[OK] automation already allowed")
        return True
    if args.automation_status:
        print(json.dumps(automation_status(), ensure_ascii=False, indent=2))
        return True
    return False


def _log_name_for_args(args: argparse.Namespace) -> str:
    if args.schedule:
        return "scheduler"
    if args.clip_worker:
        return "clip_worker"
    if args.live_split:
        return "live_split_worker"
    if args.lpl_download is not None:
        return "lpl_downloader"
    if args.record is not None:
        return "recorder"
    return "scraper"


# ── 主流程 ────────────────────────────────────────────────────────────────
def main() -> None:
    args = _parse_args()
    _setup_logging(_log_name_for_args(args))

    if _run_automation_control(args):
        return

    if args.init_db:
        _run_init_db()
        return

    if args.migrate:
        _run_migrate()
        return

    if args.find_live:
        _run_find_live(args)
        return

    if args.extract_metadata:
        _run_extract_metadata(args.extract_metadata)
        return

    if args.download is not None:
        _run_download(args.download, args.output_dir)
        return

    # ── 子指令 dispatch ─────────────────────────────────────────────
    if args.record is not None:
        _run_record(args.record, args.record_output_dir, args.record_test_mode)
        return

    if args.schedule:
        _run_schedule()
        return

    if args.clip_worker:
        _run_clip_worker()
        return

    if args.live_split:
        _run_live_split(args)
        return

    if args.cleanup:
        _run_cleanup(args.dry_run)
        return

    if args.retry_record is not None:
        _run_retry_record(args.retry_record, args.record_output_dir)
        return

    if args.reset_broadcast is not None:
        _run_reset_broadcast(args.reset_broadcast)
        return

    if args.merge_segments is not None:
        _run_merge_segments(args.merge_segments)
        return

    if args.lpl_download is not None:
        _run_lpl_download(args.lpl_download)
        return

    # 預設：跑既有 schedule 爬蟲
    _run_scrape_schedule(args)


if __name__ == "__main__":
    main()

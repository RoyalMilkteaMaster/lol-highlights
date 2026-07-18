"""定期清理 > N 天的影片檔案。

- 優先用檔名 YYYYMMDD 判斷日期（檔案複製/移動會讓 mtime 失準）
- 拿不到才 fallback 用 mtime
- 預設 enabled=true / dry_run=false
- 檔案清完後掃一次空目錄 bottom-up rmdir，避免留空殼

保留期：
- lol_vods / split / live_recordings / lol_games_vods > 3 天
- scan / finals (E:/videos/finals) / timelines > 14 天
- output_final (F:/lol-highlights/output/final) > 28 天
- cache/raw (lolesports audit JSON, write-only) > 7 天
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from automation.infra.config import load_config as _load_config

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 抓檔名內的 YYYYMMDD（前後分隔符 `_` / `.` / 邊界），20xx 限定避免誤抓 e.g. timestamp
_DATE_RE = re.compile(r"(?:^|_)(20\d{6})(?:_|\.|$)")


def _extract_date_from_filename(name: str) -> date | None:
    """從檔名抓 YYYYMMDD，例：LCK_20260508_GENvsKRX_g2.mp4 → date(2026,5,8)。

    回 None 表示無法抓（讓 caller fallback 用 mtime）。
    """
    m = _DATE_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def cleanup_old_files(*, dry_run_override: bool | None = None) -> dict:
    """掃 5 個目錄，刪超過保留期的檔案。

    Args:
        dry_run_override: 強制 dry_run（None 用 config 設定）

    Returns: {deleted_count, freed_gb, errors, dry_run}
    """
    cfg = _load_config().get("cleanup", {}) or {}
    enabled = cfg.get("enabled", False)
    config_dry_run = cfg.get("dry_run", True)
    dry_run = dry_run_override if dry_run_override is not None else config_dry_run

    if not enabled and dry_run_override is None:
        logger.info("cleanup.enabled=false（config），跳過自動清理")
        return {"deleted_count": 0, "freed_gb": 0.0, "errors": [], "dry_run": True, "skipped": True}

    # 路徑讀 highlight.utils.paths，支援 VIDEO_DIR env override（跨機器搬遷用）
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from highlight.utils import paths as _paths
    _timelines_dir = _paths.timelines_dir()
    _cache_raw_dir = _PROJECT_ROOT / "automation" / "cache" / "raw"
    targets = [
        ("lol_vods",         _paths.lol_vods_dir(),         cfg.get("lol_vods_keep_days", 3)),
        ("split",            _paths.split_dir(),            cfg.get("split_keep_days", 3)),
        ("live_recordings",       _paths.live_recordings_dir(),       cfg.get("live_recordings_keep_days", 3)),
        ("lol_games_vods", _paths.lol_games_vods_dir(), cfg.get("lol_games_vods_keep_days", 3)),
        ("scan",             _paths.scan_dir(),             cfg.get("scan_keep_days", 14)),
        ("finals_videos",    _paths.finals_dir(),           cfg.get("finals_keep_days", 14)),
        ("output_final",     _paths.final_dir(),            cfg.get("output_final_keep_days", 28)),
        ("timelines",        _timelines_dir,                cfg.get("timelines_keep_days", 14)),
        ("cache_raw",        _cache_raw_dir,                cfg.get("cache_raw_keep_days", 7)),
    ]

    # cutoff 以「今日 00:00」為基準，避免時刻誤差
    today = date.today()
    deleted_count = 0
    freed_bytes = 0
    fallback_mtime_count = 0
    errors: list[str] = []

    logger.info("=== Cleanup 開始（dry_run=%s, today=%s）===", dry_run, today)

    for name, target_dir, keep_days in targets:
        if keep_days <= 0:
            continue
        if not target_dir.is_dir():
            logger.info("  [%s] %s 不存在，跳過", name, target_dir)
            continue
        cutoff_date = today - timedelta(days=keep_days)

        n_dir, bytes_dir, n_by_filename, n_by_mtime = 0, 0, 0, 0
        for f in target_dir.rglob("*"):
            if not f.is_file():
                continue
            try:
                # 優先用檔名 YYYYMMDD 判斷
                file_date = _extract_date_from_filename(f.name)
                date_src = "filename"
                if file_date is None:
                    # fallback mtime（舊命名沒含 YYYYMMDD 的場景）
                    file_date = datetime.fromtimestamp(f.stat().st_mtime).date()
                    date_src = "mtime"

                if file_date < cutoff_date:
                    size = f.stat().st_size
                    if dry_run:
                        logger.info("  [DRY] %s（%.1f MB, date=%s by %s）",
                                    f, size / 1024 / 1024, file_date, date_src)
                    else:
                        f.unlink()
                        logger.info("  [DEL] %s（%.1f MB, date=%s by %s）",
                                    f, size / 1024 / 1024, file_date, date_src)
                    n_dir += 1
                    bytes_dir += size
                    if date_src == "filename":
                        n_by_filename += 1
                    else:
                        n_by_mtime += 1
            except OSError as e:
                errors.append(f"{f}: {e}")

        if n_by_mtime > 0:
            fallback_mtime_count += n_by_mtime
        logger.info(
            "  [%s] 保留 %d 天 (cutoff=%s) → %d 檔（%.2f GB）超期%s（檔名判定 %d / mtime fallback %d）",
            name, keep_days, cutoff_date, n_dir, bytes_dir / 1024**3,
            "（dry_run，未刪）" if dry_run else "（已刪）",
            n_by_filename, n_by_mtime,
        )
        deleted_count += n_dir
        freed_bytes += bytes_dir

        # 檔案清完後，掃一次空目錄（bottom-up）刪掉空殼，避免留一堆空資料夾
        n_dir_removed = 0
        if not dry_run:
            for d in sorted(target_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if d.is_dir() and not any(d.iterdir()):
                    try:
                        d.rmdir()
                        n_dir_removed += 1
                    except OSError:
                        pass
        else:
            for d in target_dir.rglob("*"):
                if d.is_dir() and not any(d.iterdir()):
                    logger.info("  [DRY] 空目錄 %s", d)
                    n_dir_removed += 1
        if n_dir_removed > 0:
            logger.info("  [%s] 額外清掉 %d 個空目錄", name, n_dir_removed)

    summary = {
        "deleted_count": deleted_count,
        "freed_gb": freed_bytes / 1024**3,
        "errors": errors,
        "dry_run": dry_run,
        "skipped": False,
    }
    logger.info(
        "=== Cleanup 結束：%s %d 檔，%.2f GB%s ===",
        "dry-run 該刪" if dry_run else "已刪",
        deleted_count, summary["freed_gb"],
        f"（{len(errors)} 個錯誤）" if errors else "",
    )
    return summary


if __name__ == "__main__":
    import sys
    cleanup_old_files(dry_run_override=("--dry-run" in sys.argv))

"""ingest_youtube_vod：把已結束的 YouTube VOD「灌進」live_split_worker 的工作流（/3b 實戰測試）。

streamlink 不支援錄已結束的 VOD（只支援 live），所以對結束的 LCK / LCP VOD
必須用 yt-dlp 下載 + NVENC 切 90s .ts 來模擬 streamlink 的輸出。

主流程（5 個 phase + pre-flight）：
  pre-flight checks（NVENC / 磁碟 / raw_dir 殘留）
  yt-dlp metadata + DB row + broadcast_series mapping
  yt-dlp 下載 + ffmpeg(NVENC) 切 90s .ts.hidden 到 staging
  設 broadcast.raw_segments_dir + status='recording' + auto_clip=1
  feeder loop — 每 N 秒把一個 .ts.hidden rename 成 .ts 進 raw_dir
  收尾 — status='recorded' + 寫 _RECORDING_DONE.json

跑法：
  python -m automation.tools.ingest_youtube_vod <youtube_url> --league LCK [--pace 30] [--force-clean]

`--pace`（每段 .ts 間隔秒數）：
  0  = 一次到位（最快，但測不到 GPU 競爭）
  30 = 3x 加速（推薦壓測值，~1.5-2 hr）
  90 = 真實錄影節奏（~5 hr）

任何時候 Ctrl+C：staging 留著，重跑同樣命令會接續 reveal 剩下的 .hidden。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from automation.db.connection import mysql_conn
from automation.db.repositories import (
    BroadcastRepo,
    BroadcastSeriesRepo,
    BroadcastStateRepo,
)
from automation.downloaders import download_broadcast
from automation.infra.log_setup import setup_rotating_log
from automation.infra.config import load_config as _load_config
from automation.transformers.broadcast_mapper import map_broadcast_to_series
from automation.transformers.types import BroadcastDraft

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = _PROJECT_ROOT / "_tmp" / "logs"

logger = setup_rotating_log("ingest_youtube_vod", _LOG_DIR / "ingest_youtube_vod.log")

# 預設目錄（與 streamlink_recorder 對齊；走 highlight.utils.paths 支援 VIDEO_DIR env override）
from highlight.utils import paths as _paths
LOL_VODS_ROOT       = _paths.lol_vods_dir()
RAW_SEGMENTS_ROOT   = _paths.live_recordings_dir()
STAGING_ROOT        = _paths.videos_dir() / "_live_recordings_staging"
SEGMENT_TIME_SEC    = 90
MIN_DISK_FREE_GB    = 100
MIN_TS_SIZE_BYTES   = 100 * 1024   # 100 KB（chunk 完整性下限）


# ─────────────────────────────────────────────────────────────────────────────
class IngestError(RuntimeError):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight
# ─────────────────────────────────────────────────────────────────────────────
def _check_ffmpeg_with_nvenc() -> None:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise IngestError("ffmpeg 不在 PATH，請確認安裝")
    if "h264_nvenc" not in r.stdout:
        raise IngestError("ffmpeg 沒 h264_nvenc encoder（NVIDIA 顯卡 + 對應 ffmpeg build 才有）")


def _check_disk_free(path: Path, min_gb: int) -> None:
    """預設是 E: drive。"""
    import shutil as _sh
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    free_gb = _sh.disk_usage(str(path.parent)).free / (1024 ** 3)
    if free_gb < min_gb:
        raise IngestError(
            f"磁碟空間不足：{path.parent} 只剩 {free_gb:.1f} GB（需要 ≥ {min_gb} GB）"
        )


def _check_raw_dir_clean(raw_dir: Path, force_clean: bool) -> None:
    """若 raw_dir 有殘留 .ts，預設 abort 避免污染。"""
    if not raw_dir.exists():
        return
    leftover = list(raw_dir.glob("*_part_*.ts"))
    if not leftover:
        return
    if not force_clean:
        raise IngestError(
            f"raw_dir 有 {len(leftover)} 個殘留 .ts：{raw_dir}\n"
            f"重跑會污染測試。請手動清空、或加 --force-clean 自動清"
        )
    logger.warning("--force-clean 啟動：清掉 %d 個殘留 .ts", len(leftover))
    for p in leftover:
        p.unlink()
    # 也清 cache / cumulative
    for marker in ("_yolo_events_cache.json", "_cumulative.mp4", "_RECORDING_DONE.json"):
        f = raw_dir / marker
        if f.exists():
            f.unlink()


# ─────────────────────────────────────────────────────────────────────────────
# Metadata + DB row + broadcast_series mapping
# ─────────────────────────────────────────────────────────────────────────────
def _ytdlp_metadata(url: str) -> dict:
    """yt-dlp --dump-json 抓 metadata（不下載）。"""
    cmd = [sys.executable, "-m", "yt_dlp", "--dump-json", "--no-playlist", url]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise IngestError(f"yt-dlp metadata fetch 失敗：{r.stderr[-300:]}")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError as e:
        raise IngestError(f"yt-dlp 輸出無法 parse JSON：{e}") from e


def _build_draft_from_ytdlp(meta: dict, league_code: str, league_timezone: str) -> BroadcastDraft:
    """yt-dlp dump-json 結果 → BroadcastDraft（已結束 → source_status='ended'）。

    yt-dlp 的關鍵欄位：
      - id              : video_id
      - title           : 標題
      - channel_id      : 頻道
      - upload_date     : YYYYMMDD（UTC date 形式）
      - release_timestamp: live 開播 unix ts（VOD 也有）
      - timestamp       : 上傳 unix ts（fallback）
      - is_live / live_status: 'was_live' / 'is_live' / 'not_live'
    """
    video_id = meta["id"]
    title = meta.get("title", "")
    channel_id = meta.get("channel_id")

    # 開播時間：release_timestamp 優先，fallback timestamp
    ts = meta.get("release_timestamp") or meta.get("timestamp")
    scheduled_start_utc = (
        datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None
    )

    return BroadcastDraft(
        platform="youtube",
        external_id=video_id,
        channel_id=channel_id,
        league_code=league_code,
        league_timezone=league_timezone,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=title,
        scheduled_start_utc=scheduled_start_utc,
        actual_start_utc=scheduled_start_utc,
        source_status="ended",
    )


def _fetch_series_for_mapper(conn, match_date, league_id: int) -> list[dict]:
    """抄自 pipeline.py:362。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.series_id, ta.code AS team_a_code, tb.code AS team_b_code
            FROM series s
            JOIN teams ta ON ta.team_id = s.team_a_id
            JOIN teams tb ON tb.team_id = s.team_b_id
            WHERE s.match_date = %s AND s.league_id = %s
            ORDER BY s.match_time, s.series_id
            """,
            (match_date, league_id),
        )
        return [dict(r) for r in cur.fetchall()]


def _fetch_team_codes_for_league(conn, league_id: int) -> list[str]:
    """抄自 pipeline.py:381。"""
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM teams WHERE league_id=%s", (league_id,))
        return [r["code"] for r in cur.fetchall()]


def _phase1_create_db_rows(draft: BroadcastDraft, league_id: int) -> tuple[int, str]:
    """寫 broadcast row + series mapping。回傳 (broadcast_id, mapping_confidence)。"""
    with mysql_conn() as conn:
        repo = BroadcastRepo(conn)
        bs_repo = BroadcastSeriesRepo(conn)

        broadcast_id, is_new, action = repo.upsert_by_external(
            platform=draft.platform,
            external_id=draft.external_id,
            broadcast_date=draft.broadcast_date_local(),
            league_id=league_id,
            league_code=draft.league_code,
            league_timezone=draft.league_timezone,
            url=draft.url,
            title=draft.title,
            channel_id=draft.channel_id,
            scheduled_start_utc=draft.scheduled_start_utc,
            source_status=draft.source_status,
            confidence="medium",
        )
        conn.commit()
        logger.info("broadcasts upsert: id=%d, action=%s", broadcast_id, action)

        # mapping
        existing = bs_repo.get_series_for_broadcast(broadcast_id)
        mapping_conf = "skipped"
        if not existing:
            series_rows = _fetch_series_for_mapper(
                conn, draft.broadcast_date_local(), league_id
            )
            team_codes = _fetch_team_codes_for_league(conn, league_id)
            mappings = map_broadcast_to_series(draft, series_rows, team_codes)
            if mappings:
                for m in mappings:
                    bs_repo.add_mapping(
                        broadcast_id=broadcast_id,
                        series_id=m.series_id,
                        series_order=m.series_order,
                        mapping_confidence=m.confidence,
                        mapping_source=m.source,
                        mapping_reason=m.reason,
                    )
                conn.commit()
                mapping_conf = mappings[0].confidence
                logger.info(
                    "broadcast_series mapping: %d 筆，最高 confidence=%s",
                    len(mappings), mapping_conf,
                )
            else:
                mapping_conf = "failed"
                logger.warning(
                    "broadcast_series mapping FAILED — 標題 / series 表 / team_codes 對不上。"
                    " 影片工程仍可繼續，但賽程對應為空。"
                )
        else:
            # 既有 mapping，取第一筆
            mapping_conf = existing[0].get("mapping_confidence", "existing")
            logger.info("broadcast_series 已有 %d 筆 mapping，跳過", len(existing))

        return broadcast_id, mapping_conf


# ─────────────────────────────────────────────────────────────────────────────
# 下載 + 切段（到 staging）
# ─────────────────────────────────────────────────────────────────────────────
def _ffprobe_codec(path: Path) -> str:
    cmd = ["ffprobe", "-v", "error",
           "-select_streams", "v:0",
           "-show_entries", "stream=codec_name",
           "-of", "default=nw=1:nk=1", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise IngestError(f"ffprobe codec 失敗：{r.stderr[-200:]}")
    return r.stdout.strip().lower()


def _ffprobe_duration(path: Path) -> float:
    cmd = ["ffprobe", "-v", "error",
           "-show_entries", "format=duration",
           "-of", "default=nw=1:nk=1", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise IngestError(f"ffprobe duration 失敗：{r.stderr[-200:]}")
    return float(r.stdout.strip())


def _staging_is_complete(staging_dir: Path, prefix: str, src_dur: float) -> bool:
    """staging 是否已切完且完整：part 從 000 連續 + 每段 size > 100KB + 總時長 ≈ src_dur。"""
    if not staging_dir.is_dir():
        return False
    files = sorted(staging_dir.glob(f"{prefix}_part_*.ts.hidden"))
    if not files:
        return False
    # 連續性
    expected_idx = 0
    for f in files:
        try:
            idx = int(f.stem.replace(".ts", "").rsplit("_", 1)[-1])
        except ValueError:
            return False
        if idx != expected_idx:
            return False
        expected_idx += 1
    # 每段 size 下限
    for f in files:
        if f.stat().st_size < MIN_TS_SIZE_BYTES:
            return False
    # 總時長
    total = sum(_ffprobe_duration(f) for f in files)
    if abs(total - src_dur) > 5.0:   # 允許 5 秒誤差
        logger.warning("staging 總時長 %.1fs vs source %.1fs，差超過 5s", total, src_dur)
        return False
    return True


def _chunk_to_staging(mp4_path: Path, staging_dir: Path, prefix: str,
                       codec: str, src_dur: float) -> int:
    """把 mp4_path 切成 90s .ts.hidden 到 staging_dir，回傳段數。

    H.264 source → remux（-c copy）
    AV1 / VP9 → NVENC 轉 H.264
    """
    # 清舊 staging（idempotent）
    if staging_dir.exists():
        for old in staging_dir.glob(f"{prefix}_part_*.ts.hidden"):
            old.unlink()
    staging_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(staging_dir / f"{prefix}_part_%03d.ts.hidden")

    if codec == "h264":
        logger.info("source codec=h264 → remux 切段（快）")
        cmd = [
            "ffmpeg", "-y",
            "-i", str(mp4_path),
            "-c", "copy",
            "-bsf:v", "h264_mp4toannexb",
            "-f", "segment",
            "-segment_format", "mpegts",
            "-segment_time", str(SEGMENT_TIME_SEC),
            "-reset_timestamps", "1",
            "-segment_start_number", "0",
            output_template,
        ]
    else:
        logger.info("source codec=%s → NVENC 轉 H.264 切段（~13-20 min for 4hr）", codec)
        cmd = [
            "ffmpeg", "-y",
            "-i", str(mp4_path),
            "-map", "0:v:0",
            "-map", "0:a:0",
            "-c:v", "h264_nvenc",
            "-preset", "fast",
            "-cq", "23",
            "-c:a", "aac", "-b:a", "192k",  # opus/vorbis 不支援 mpegts，必須轉 aac
            # 不加 -bsf:v h264_mp4toannexb：NVENC 輸出已是 Annex B，加了反而炸
            "-f", "segment",
            "-segment_format", "mpegts",
            "-segment_time", str(SEGMENT_TIME_SEC),
            "-reset_timestamps", "1",
            "-segment_start_number", "0",
            output_template,
        ]

    logger.info("ffmpeg 開始 chunk → %s", staging_dir)
    r = subprocess.run(cmd, stderr=subprocess.PIPE, text=True, check=False)
    if r.returncode != 0:
        raise IngestError(f"ffmpeg chunk 失敗 rc={r.returncode}：{r.stderr[-500:]}")

    # 完整性 verify
    files = sorted(staging_dir.glob(f"{prefix}_part_*.ts.hidden"))
    n = len(files)
    if n == 0:
        raise IngestError("ffmpeg 跑完 rc=0 但 staging 沒產生任何 .ts.hidden")

    expected_idx = 0
    for f in files:
        try:
            idx = int(f.stem.replace(".ts", "").rsplit("_", 1)[-1])
        except ValueError:
            raise IngestError(f"無法 parse part 編號：{f.name}")
        if idx != expected_idx:
            raise IngestError(f"part 編號不連續：期望 {expected_idx} 拿到 {idx}")
        expected_idx += 1
        if f.stat().st_size < MIN_TS_SIZE_BYTES:
            raise IngestError(f"{f.name} size {f.stat().st_size} < 100KB")

    total = sum(_ffprobe_duration(f) for f in files)
    if abs(total - src_dur) > 5.0:
        raise IngestError(
            f"staging 總時長 {total:.1f}s 與 source {src_dur:.1f}s 差超過 5s"
        )

    logger.info("chunk OK：%d 段，總時長 %.1fs", n, total)
    return n


def _phase2_download_and_chunk(broadcast_id: int, prefix: str) -> tuple[Path, Path, int]:
    """回傳 (mp4_path, staging_dir, n_chunks)。"""
    LOL_VODS_ROOT.mkdir(parents=True, exist_ok=True)

    # 4) Idempotent download skip
    with mysql_conn() as conn:
        row = BroadcastRepo(conn).get_by_id(broadcast_id)
    rec = row.get("recording_path") if row else None
    if rec and Path(rec).is_file() and Path(rec).stat().st_size > 100 * 1024 * 1024:
        mp4_path = Path(rec)
        logger.info("recording_path 已存在 %s（%.1f GB），skip download",
                    mp4_path.name, mp4_path.stat().st_size / 1024**3)
    else:
        logger.info("yt-dlp 下載 broadcast_id=%s → %s", broadcast_id, LOL_VODS_ROOT)
        mp4_path = download_broadcast(broadcast_id, LOL_VODS_ROOT)

    # 5) ffprobe codec + duration
    codec = _ffprobe_codec(mp4_path)
    src_dur = _ffprobe_duration(mp4_path)
    logger.info("source: codec=%s, duration=%.1fs (%dh%02dm)", codec, src_dur,
                int(src_dur // 3600), int((src_dur % 3600) // 60))

    # 6/7) staging dir + chunk
    staging_dir = STAGING_ROOT / prefix
    if _staging_is_complete(staging_dir, prefix, src_dur):
        n = len(list(staging_dir.glob(f"{prefix}_part_*.ts.hidden")))
        logger.info("staging 已完整（%d 段），skip chunk", n)
    else:
        n = _chunk_to_staging(mp4_path, staging_dir, prefix, codec, src_dur)

    return mp4_path, staging_dir, n


# ─────────────────────────────────────────────────────────────────────────────
# 設 DB 狀態 = 'recording'
# ─────────────────────────────────────────────────────────────────────────────
def _phase3_db_set_recording(broadcast_id: int, raw_dir: Path, mp4_path: Path) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    with mysql_conn() as conn:
        s = BroadcastStateRepo(conn)
        s.set_paths(broadcast_id,
                    raw_segments_dir=str(raw_dir.resolve()),
                    recording_path=str(mp4_path.resolve()))
        s.update_status(broadcast_id, "recording")
        s.set_recording_started_at(broadcast_id)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcasts SET auto_clip=1 WHERE broadcast_id=%s",
                (broadcast_id,),
            )
        conn.commit()
    logger.info("DB 狀態：recording / auto_clip=1 / raw_dir=%s", raw_dir)


# ─────────────────────────────────────────────────────────────────────────────
# Feeder
# ─────────────────────────────────────────────────────────────────────────────
def _print_preflight_summary(*, broadcast_id, draft, prefix, raw_dir, staging_dir,
                              n_hidden, n_ts, pace, mapping_conf) -> None:
    print()
    print("=" * 70)
    print(" Pre-flight summary")
    print("=" * 70)
    print(f"  broadcast_id        : {broadcast_id}")
    print(f"  external_id (video) : {draft.external_id}")
    print(f"  league / date       : {draft.league_code} / {draft.broadcast_date_local()}")
    print(f"  prefix              : {prefix}")
    print(f"  raw_dir (worker 看) : {raw_dir}")
    print(f"  staging_dir         : {staging_dir}")
    print(f"  staging .hidden 數  : {n_hidden}")
    print(f"  raw_dir 已有 .ts    : {n_ts}")
    print(f"  pace                : {pace}s/段（feeder 間隔）")
    print(f"  mapping confidence  : {mapping_conf}")
    print("=" * 70)
    print()


def _phase4_feeder(staging_dir: Path, raw_dir: Path, prefix: str,
                    pace: int, broadcast_id: int) -> None:
    """逐段把 .ts.hidden rename 進 raw_dir。"""
    hiddens = sorted(staging_dir.glob(f"{prefix}_part_*.ts.hidden"))
    n = len(hiddens)
    if n == 0:
        logger.info("staging 已空（可能 feeder 之前跑完了），跳過")
        return

    logger.info("feeder 開始：%d 段待 reveal，pace=%ds", n, pace)
    revealed = 0
    try:
        for i, hidden in enumerate(hiddens):
            target = raw_dir / hidden.name.replace(".ts.hidden", ".ts")
            shutil.move(str(hidden), str(target))
            revealed = i + 1
            ts_min = (i + 1) * SEGMENT_TIME_SEC / 60
            logger.info("[%d/%d] revealed %s（累積 %.1f min）",
                        revealed, n, target.name, ts_min)
            if pace > 0 and i < n - 1:
                time.sleep(pace)
    except KeyboardInterrupt:
        logger.warning(
            "Feeder interrupted at %d/%d。DB status 仍是 'recording'。\n"
            "重跑同樣命令會接續 reveal 剩下的 .ts.hidden。",
            revealed, n,
        )
        raise


# ─────────────────────────────────────────────────────────────────────────────
# 收尾
# ─────────────────────────────────────────────────────────────────────────────
def _phase5_finalize(broadcast_id: int, raw_dir: Path, n_total: int,
                      pace: int, mapping_conf: str, src_codec: str) -> None:
    with mysql_conn() as conn:
        s = BroadcastStateRepo(conn)
        s.update_status(broadcast_id, "recorded")
        s.set_recording_ended_at(broadcast_id)

    marker = raw_dir / "_RECORDING_DONE.json"
    marker.write_text(
        json.dumps({
            "broadcast_id": broadcast_id,
            "total_parts": n_total,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "pace": pace,
            "mapping_confidence": mapping_conf,
            "src_codec": src_codec,
        }, indent=2),
        encoding="utf-8",
    )
    logger.info("收尾完成。worker 會繼續切 + clip。看 dashboard。")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_league_id(league_code: str) -> tuple[int, str]:
    """從 config.yaml 拿 league_id + timezone（hardcoded LCK→Asia/Seoul 等）。"""
    cfg = _load_config()
    leagues = cfg.get("leagues") or {}
    league_id = leagues.get(league_code) or leagues.get(league_code.upper())
    if league_id is None:
        raise IngestError(
            f"league={league_code} 不在 config.yaml leagues 對照表。"
            f" 已設定：{list(leagues.keys())}"
        )
    # YouTube channels list 對應 timezone
    tz_map = {ch["league"]: ch["timezone"]
              for ch in (cfg.get("youtube") or {}).get("channels", [])}
    tz = tz_map.get(league_code) or tz_map.get(league_code.upper())
    if tz is None:
        # fallback
        tz = {"LCK": "Asia/Seoul", "LCP": "Asia/Taipei",
              "LPL": "Asia/Shanghai", "LEC": "Europe/Berlin",
              "LCS": "America/Los_Angeles"}.get(league_code.upper(), "UTC")
    return int(league_id), tz


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="ingest_youtube_vod",
                                 description="把已結束的 YouTube LCK VOD 灌進 live_split_worker pipeline")
    p.add_argument("url", help="YouTube 直播 / VOD URL")
    p.add_argument("--league", required=True, help="LCK / LCP / LEC ...")
    p.add_argument("--pace", type=int, default=30,
                   help="feeder 每段間隔秒數（0=一次到位 / 30=3x 推薦壓測 / 90=真實 1x）")
    p.add_argument("--force-clean", action="store_true",
                   help="raw_dir 有殘留 .ts 自動清掉（否則預設 abort）")
    p.add_argument("--no-feeder", action="store_true",
                   help="只跑 + chunk，不跑 feeder（給 debug 用）")
    args = p.parse_args()

    league = args.league.upper()
    pace = max(0, args.pace)

    try:
        # ── ──
        logger.info("[] pre-flight checks")
        _check_ffmpeg_with_nvenc()
        _check_disk_free(STAGING_ROOT, MIN_DISK_FREE_GB)

        # ── ──
        logger.info("[] yt-dlp metadata + DB row + mapping")
        league_id, tz = _resolve_league_id(league)
        meta = _ytdlp_metadata(args.url)
        draft = _build_draft_from_ytdlp(meta, league, tz)
        logger.info("draft: video_id=%s title=%s broadcast_date=%s",
                    draft.external_id, draft.title[:60], draft.broadcast_date_local())

        broadcast_id, mapping_conf = _phase1_create_db_rows(draft, league_id)

        # prefix 對齊 streamlink_recorder 慣例（external_id[:8]）
        date_str = draft.broadcast_date_local().strftime("%Y%m%d")
        prefix = f"{league}_{date_str}_youtube_{draft.external_id[:8]}"
        raw_dir = RAW_SEGMENTS_ROOT / prefix

        _check_raw_dir_clean(raw_dir, args.force_clean)

        # ── ──
        logger.info("[] download + chunk to staging")
        mp4_path, staging_dir, n_chunks = _phase2_download_and_chunk(broadcast_id, prefix)
        src_codec = _ffprobe_codec(mp4_path)

        # ── ──
        logger.info("[] DB → recording / auto_clip=1")
        _phase3_db_set_recording(broadcast_id, raw_dir, mp4_path)

        # ── Pre-flight summary + 等 user 啟 worker ──
        n_ts = len(list(raw_dir.glob(f"{prefix}_part_*.ts")))
        _print_preflight_summary(
            broadcast_id=broadcast_id, draft=draft, prefix=prefix,
            raw_dir=raw_dir, staging_dir=staging_dir,
            n_hidden=n_chunks, n_ts=n_ts, pace=pace, mapping_conf=mapping_conf,
        )

        if args.no_feeder:
            print("--no-feeder：staging 已就緒，DB 'recording'。手動跑 feeder：")
            print(f"  python -m automation.tools.ingest_youtube_vod {args.url} "
                  f"--league {league} --pace {pace}")
            return 0

        if pace > 0:
            input("✏️  現在請開另一個視窗雙擊 自動錄影.bat，看 dashboard 確認 4 個 worker 都 green，"
                  "回來按 Enter 啟動 feeder（pace > 0 時 worker 必須先就位才有意義）...")

        # ── ──
        logger.info("[] feeder loop")
        _phase4_feeder(staging_dir, raw_dir, prefix, pace, broadcast_id)

        # ── ──
        logger.info("[] finalize")
        _phase5_finalize(broadcast_id, raw_dir,
                         n_total=n_chunks, pace=pace,
                         mapping_conf=mapping_conf, src_codec=src_codec)

        print()
        print("[OK] ingest 完成。worker 會繼續切 + clip，看 dashboard / logs/live_split_worker.log。")
        return 0

    except IngestError as e:
        logger.error("Ingest 中止：%s", e)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception:
        logger.exception("未預期例外")
        return 1


if __name__ == "__main__":
    sys.exit(main())

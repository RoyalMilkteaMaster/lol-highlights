"""一次性 VOD 補錄 script — 階段 0。

用途：broadcast 漏切某場 game 時，用官方 VOD URL + 已知時間範圍補錄一場。

關鍵設計（user 訴求）：
  ✅ yt-dlp `--download-sections` 只下載指定範圍（不抓整個 6 hr broadcast）
  ✅ main.py 對 partial.mp4 局部 YOLO（30-40 min 內容只跑 6-10 min YOLO）
  ✅ 整體流程跟既有 LPL 流程一致：INSERT broadcast_games + enqueue clip_job
  ✅ 配合 R7-strict 鐵則：沒抓到 nexus 自動 fail-fast 不出爛 highlight

用法範例：
  # 1) 補 KRX/GEN g2（broadcast 42, LCK 雙系列第二 series）
  python -m automation.tools.recover_broadcast_game \\
      --broadcast-id 42 \\
      --vod-url "https://www.youtube.com/watch?v=<LCK_VOD_ID>" \\
      --start-sec 14364 \\
      --end-sec 17200 \\
      --game-index 4 \\
      --series-id 20260508010020 \\
      --series-order 2 \\
      --team-a GEN --team-b KRX

  # 2) 補 BRO/NS g2（broadcast 45）— start 用 game 24 (BRO/NS g1) end_offset
  python -m automation.tools.recover_broadcast_game \\
      --broadcast-id 45 \\
      --vod-url "https://www.youtube.com/watch?v=<LCK_VOD_ID>" \\
      --start-sec 11328 \\
      --end-sec 14400 \\
      --game-index 5 \\
      --series-id 20260509010020 \\
      --series-order 2 \\
      --team-a BRO --team-b NS

  # 3) 單場 VOD（LCK 把 KRX/GEN g2 切成獨立 video）→ start=0 end=video_duration
  python -m automation.tools.recover_broadcast_game \\
      --broadcast-id 42 \\
      --vod-url "https://www.youtube.com/watch?v=<SINGLE_GAME_VOD_ID>" \\
      --start-sec 0 \\
      --end-sec 99999 \\
      --game-index 4 ...

  # 4) AUTO 模式：planner 自動推算 vod_url + start/end + team codes（階段 3 cron 用）
  python -m automation.tools.recover_broadcast_game \\
      --broadcast-id 42 --game-index 4 --auto
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from automation.db.connection import mysql_conn
from automation.db.repositories import (
    BroadcastGameRepo, BroadcastStateRepo, ClipJobRepo,
)
from automation.infra.log_setup import setup_rotating_log

_LOG_DIR = _PROJECT_ROOT / "_tmp" / "logs"
logger = setup_rotating_log("recover_broadcast_game", _LOG_DIR / "recover_broadcast_game.log")


def _make_output_path(league_code: str, broadcast_date, team_a: str, team_b: str,
                      game_index: int) -> Path:
    """命名跟 lpl_downloader._make_output_dir 一致：<LEAGUE>_<YYYYMMDD>_<TA>vs<TB>/_g<N>.mp4"""
    from highlight.utils import paths as _paths
    root = _paths.lol_games_vods_dir()
    league = league_code.upper()
    date_str = broadcast_date.strftime("%Y%m%d")
    team_a = team_a.upper()
    team_b = team_b.upper()
    prefix = f"{league}_{date_str}_{team_a}vs{team_b}"
    out_dir = root / prefix
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"{prefix}_g{game_index}.mp4"


def _is_mp4_complete(
    mp4_path: Path,
    expected_duration_sec: int,
    *,
    tolerance_sec: int = 10,
    min_size_mb: int = 100,
) -> tuple[bool, str]:
    """19：嚴格判別 mp4 是否完整。回 (ok, reason)。

    4 層 check 由快到慢，任一 fail 即視為 corrupt：
      L1: .part 仍存在 → yt-dlp 未完成 merge / 中斷
      L2: 檔案 size < min_size_mb (default 100 MB) → 明顯 partial
      L3: ffprobe duration 跟預期差 > tolerance_sec (default 10s) → 抓不全
      L4: ffmpeg null decode 看 container / H.264 NAL error → corrupt stream

    用途：取代之前「size > 1 GB → skip yt-dlp」的弱判斷
    （broadcast 84 g1 case：1.78 GB corrupt 1080p 被誤判完成）。
    """
    part_path = mp4_path.with_suffix(mp4_path.suffix + ".part")
    if part_path.exists():
        return False, f".part 還在（下載未完成）: {part_path.name}"

    if not mp4_path.is_file():
        return False, "mp4 不存在"

    size_mb = mp4_path.stat().st_size / 1024 / 1024
    if size_mb < min_size_mb:
        return False, f"size {size_mb:.0f} MB < {min_size_mb} MB"

    # L3: ffprobe duration
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(mp4_path)],
        capture_output=True, text=True, timeout=30,
        encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        return False, f"ffprobe rc={r.returncode}: {(r.stderr or '')[-200:]}"
    try:
        actual_duration = float(json.loads(r.stdout)["format"]["duration"])
    except Exception as e:
        return False, f"ffprobe duration 解析失敗: {e}"

    delta = abs(actual_duration - expected_duration_sec)
    if delta > tolerance_sec:
        return False, (
            f"duration {actual_duration:.0f}s 跟預期 {expected_duration_sec}s "
            f"差 {delta:.0f}s > {tolerance_sec}s"
        )

    # L4: ffmpeg null decode 抓 container/H.264 error
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(mp4_path),
         "-c", "copy", "-f", "null", "-"],
        capture_output=True, text=True, timeout=120,
        encoding="utf-8", errors="replace",
    )
    if r.stderr.strip():
        err_tail = r.stderr.strip()[-300:]
        return False, f"ffmpeg decode error: {err_tail}"

    return True, f"OK ({size_mb:.0f} MB, {actual_duration:.0f}s)"


def _ytdlp_download_section(vod_url: str, start_sec: int, end_sec: int,
                             output_path: Path) -> None:
    """yt-dlp --download-sections 只切指定範圍，1080p 高 bit_rate avc1 優先。"""
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "--no-warnings",
        "--no-playlist",
        "--download-sections", f"*{start_sec}-{end_sec}",
        "--force-keyframes-at-cuts",
        # 沿用 lpl_downloader 的 selector
        "-f", "bv*[height>=1080][vcodec~='avc1']+ba/bv*[height>=1080]+ba",
        "--format-sort", "res,tbr",
        "--merge-output-format", "mp4",
        "-o", str(output_path),
        "--no-progress",
        "--retries", "10",
        "--fragment-retries", "10",
        vod_url,
    ]
    logger.info("yt-dlp download_sections [%d, %d] → %s", start_sec, end_sec, output_path)
    # encoding='utf-8' errors='replace'：避免 Windows cp950 解碼 yt-dlp UTF-8 輸出失敗
    # 變成 stderr=None → r.stderr[-500:] 噴 TypeError
    r = subprocess.run(cmd, capture_output=True, text=True, check=False,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        err_tail = (r.stderr or "")[-500:] or "(stderr empty)"
        raise RuntimeError(f"yt-dlp 失敗 rc={r.returncode}: {err_tail}")
    if not output_path.is_file() or output_path.stat().st_size < 50 * 1024 * 1024:
        raise RuntimeError(
            f"輸出疑似不完整：{output_path} ({output_path.stat().st_size if output_path.exists() else 0} bytes)"
        )
    logger.info("[OK] 切下載完成 %.0f MB", output_path.stat().st_size / 1024 / 1024)


def _insert_game_and_enqueue(
    broadcast_id: int, game_index: int, output_path: Path,
    start_sec: int, end_sec: int,
    series_id: int | None, series_order: int | None,
    team_a: str, team_b: str,
    force: bool = False,
) -> int:
    """INSERT broadcast_games row（status='cut'）+ enqueue clip_job → clip_worker 自動跑 main.py。

    force=True：reset clip_job (done/failed → pending) 讓 clip_worker 重撈跑 main.py。
    """
    duration_sec = end_sec - start_sec
    with mysql_conn() as conn:
        repo = BroadcastGameRepo(conn)
        existing = repo.get_by_index(broadcast_id, game_index)
        if existing:
            logger.warning("game_index=%d 已存在 (game_id=%d)，先 mark cut + 更新 path",
                           game_index, existing["game_id"])
            game_id = existing["game_id"]
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE broadcast_games SET game_path=%s, status='cut', "
                    "  start_offset_sec=0, end_offset_sec=%s, "
                    "  series_id=%s, series_order=%s, team_a_code=%s, team_b_code=%s, "
                    "  end_source='manual', confidence=0.8, naming_provisional=FALSE "
                    "WHERE game_id=%s",
                    (str(output_path.resolve()), duration_sec,
                     series_id, series_order, team_a, team_b, game_id),
                )
        else:
            game_id = repo.insert(
                broadcast_id=broadcast_id, game_index=game_index,
                start_offset_sec=0.0,                # partial.mp4 內部從 0 開始
                end_offset_sec=float(duration_sec),
                start_source="manual", end_source="manual",
                status="cut", confidence=0.8,
                series_id=series_id, series_order=series_order,
                team_a_code=team_a, team_b_code=team_b,
                commit=False,
            )
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE broadcast_games SET game_path=%s, naming_provisional=FALSE WHERE game_id=%s",
                    (str(output_path.resolve()), game_id),
                )
            logger.info("INSERT game_id=%d broadcast_id=%d game_index=%d (%dvs%d series_id=%s)",
                        game_id, broadcast_id, game_index, ord(team_a[0]), ord(team_b[0]), series_id)

        # enqueue clip_job（--force：reset done/failed → pending 讓 clip_worker 重撈）
        if force:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE clip_jobs SET status='pending', retry_count=0, "
                    "       error_message=NULL, started_at=NULL, ended_at=NULL "
                    "WHERE game_id=%s",
                    (game_id,),
                )
                affected = cur.rowcount
            if affected:
                logger.info("[--force] reset clip_job game_id=%d → pending（覆蓋舊 highlight）", game_id)
            else:
                ClipJobRepo(conn).enqueue_or_reset_failed(game_id, commit=False)
        else:
            ClipJobRepo(conn).enqueue_or_reset_failed(game_id, commit=False)
        conn.commit()
        logger.info("enqueued clip_job for game_id=%d → clip_worker 30s 內會撈起跑 main.py + clip.py", game_id)
    return game_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="一次性 VOD 補錄 broadcast 漏切的 game（階段 0）"
    )
    parser.add_argument("--broadcast-id", type=int, required=True)
    parser.add_argument("--vod-url", type=str,
                        help="LCK / LCP / LPL 官方 VOD URL；--auto 時可不給")
    parser.add_argument("--start-sec", type=int,
                        help="VOD 內部秒數起點；--auto 時可不給")
    parser.add_argument("--end-sec", type=int,
                        help="VOD 內部秒數終點；--auto 時可不給")
    parser.add_argument("--game-index", type=int, required=True,
                        help="這場 game 在 broadcast 內的 index（broadcast-wide）")
    parser.add_argument("--series-id", type=int,
                        help="series_id（lolesports 14位）；不知道就 omit，後續 naming_finalizer cron 會補")
    parser.add_argument("--series-order", type=int,
                        help="series_order（series 內第幾局，1/2/3）；同上")
    parser.add_argument("--team-a", type=str, help="例：T1, GEN, BRO；--auto 時可不給")
    parser.add_argument("--team-b", type=str, help="--auto 時可不給")
    parser.add_argument("--auto", action="store_true",
                        help="自動用 vod_recovery_planner 推算 vod_url / start / end / team codes")
    parser.add_argument("--force", action="store_true",
                        help="強制重 recover（刪除既有 mp4 + reset clip_job 重撈）— 中斷救援用")
    args = parser.parse_args()

    # AUTO 模式：用 planner 補沒給的欄位
    if args.auto or not all([args.vod_url, args.start_sec is not None,
                              args.end_sec is not None, args.team_a, args.team_b]):
        from automation.services.vod_recovery_planner import plan_recovery
        plan = plan_recovery(args.broadcast_id, args.game_index)
        if not plan:
            logger.error("planner 推算失敗（broadcast %s game_index %d 缺資料？）",
                         args.broadcast_id, args.game_index)
            return 1
        args.vod_url      = args.vod_url      or plan.vod_url
        args.start_sec    = args.start_sec    if args.start_sec is not None else plan.start_sec
        args.end_sec      = args.end_sec      if args.end_sec is not None else plan.end_sec
        args.team_a       = args.team_a       or plan.team_a
        args.team_b       = args.team_b       or plan.team_b
        args.series_id    = args.series_id    or plan.series_id
        args.series_order = args.series_order or plan.series_order
        logger.info("[AUTO] planner 推算完成：%s vs %s [%d, %d] series=%s order=%s",
                    args.team_a, args.team_b, args.start_sec, args.end_sec,
                    args.series_id, args.series_order)

    logger.info("=" * 70)
    logger.info("VOD 補錄開始：broadcast=%d game_index=%d %s vs %s",
                args.broadcast_id, args.game_index, args.team_a, args.team_b)
    logger.info("VOD URL: %s", args.vod_url)
    logger.info("範圍: [%d, %d] = %d sec = %.1f min",
                args.start_sec, args.end_sec, args.end_sec - args.start_sec,
                (args.end_sec - args.start_sec) / 60)
    logger.info("=" * 70)

    # 1. Load broadcast 拿命名所需資訊
    with mysql_conn() as conn:
        broadcast = BroadcastStateRepo(conn).get_by_id(args.broadcast_id)
    if not broadcast:
        logger.error("broadcast_id=%s 不存在", args.broadcast_id)
        return 1

    # 2. 算輸出路徑
    output_path = _make_output_path(
        broadcast["league_code"], broadcast["broadcast_date"],
        args.team_a, args.team_b, args.game_index,
    )

    # --force：強制刪 mp4 重 download（中斷救援用，要拿 archive VOD 完整內容）
    if args.force and output_path.is_file():
        logger.warning("[--force] 刪除既有 mp4 強制重下載：%s", output_path)
        output_path.unlink()

    # 19：嚴格判別 mp4 是否完整（取代之前「size > 1 GB → skip」弱判斷）
    expected_dur = args.end_sec - args.start_sec
    if output_path.is_file():
        ok, reason = _is_mp4_complete(output_path, expected_dur)
    else:
        ok, reason = False, "mp4 不存在"

    if ok:
        logger.info("輸出檔已存在且完整 → skip yt-dlp（%s）", reason)
    else:
        if output_path.is_file():
            logger.warning("輸出檔不完整，覆蓋重下：%s（原因：%s）", output_path, reason)
            output_path.unlink()
        _ytdlp_download_section(args.vod_url, args.start_sec, args.end_sec, output_path)

    # 4. INSERT game + enqueue clip_job
    game_id = _insert_game_and_enqueue(
        args.broadcast_id, args.game_index, output_path,
        args.start_sec, args.end_sec,
        args.series_id, args.series_order,
        args.team_a, args.team_b,
        force=args.force,
    )

    print()
    print(f"[OK] VOD 補錄完成 game_id={game_id}")
    print(f"   檔案: {output_path}")
    print(f"   clip_worker 30s 內會撈起跑 main.py → highlight")
    print(f"   觀察：tail -f logs/clip_worker.log")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    sys.exit(main())

"""yt-dlp 下載包裝（給 --download <broadcast_id> 用）。

行為：
1. 從 DB 撈 broadcast row（platform / external_id / url / league_code / broadcast_date）
2. 組標準命名：<LEAGUE>_<YYYYMMDD>_<PLATFORM>_<shortid>.mp4
3. subprocess 跑 yt-dlp（YouTube + Bilibili 兩平台都用）
4. 完成後 UPDATE broadcasts.recording_path

注意：
- 不接 Streamlink（B phase 才做）
- yt-dlp 對 Bilibili live 也支援（雖然不如 Streamlink 穩，但夠手動用）
- 失敗時 raise + 印 yt-dlp stderr
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from automation.db.connection import mysql_conn
from automation.db.repositories import BroadcastRepo

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
def download_broadcast(broadcast_id: int, output_dir: Path) -> Path:
    """主流程。

    Args:
        broadcast_id: broadcasts.broadcast_id（DB PK）
        output_dir  : 輸出資料夾（如 E:\\videos\\lol_vods\\）

    Returns:
        實際輸出的 mp4 絕對路徑

    Raises:
        ValueError    : broadcast_id 找不到對應 row
        RuntimeError  : yt-dlp 執行失敗
    """
    # ── Step 1: 撈 broadcast row ──────────────────────────────────────
    with mysql_conn() as conn:
        repo = BroadcastRepo(conn)
        row = repo.get_by_id(broadcast_id)
    if row is None:
        raise ValueError(f"broadcast_id={broadcast_id} 找不到")

    platform     = row["platform"]
    external_id  = row["external_id"]
    league_code  = row["league_code"]
    broadcast_dt = row["broadcast_date"]
    url          = row["stream_url"] or row["vod_url"]
    if not url:
        raise ValueError(f"broadcast_id={broadcast_id} 沒有 URL（stream_url/vod_url 都空）")

    # ── Step 2: 組標準檔名 ─────────────────────────────────────────────
    short_id = _make_short_id(platform, external_id)
    date_str = broadcast_dt.strftime("%Y%m%d")
    filename_stem = f"{league_code}_{date_str}_{platform}_{short_id}"

    # 給 yt-dlp 的 -o 模板（讓 yt-dlp 自己決定副檔名）
    output_template = str(output_dir / f"{filename_stem}.%(ext)s")

    logger.info("開始下載 broadcast_id=%s → %s", broadcast_id, output_template)
    logger.info("  URL: %s", url)

    # ── Step 3: 跑 yt-dlp（用 python -m yt_dlp 避開 PATH 問題）─────────
    import sys
    cmd = [
        sys.executable,
        "-m", "yt_dlp",
        "--no-playlist",
        "-o", output_template,
        "--no-progress",
        # 優先抓 h264(avc1)：mpegts 原生支援，不需 NVENC 轉碼
        # av1/vp9 雖畫質相近但音訊常是 opus，無法 copy 進 ts → 切段會炸
        "-f", "bv*[vcodec~='avc1'][height>=1080]+ba/bv*[height>=1080]+ba/bv*+ba/b",
        "--merge-output-format", "mp4",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.error("yt-dlp stderr: %s", result.stderr[-1000:])
        raise RuntimeError(
            f"yt-dlp 失敗（rc={result.returncode}）：{result.stderr[:300]}"
        )

    # ── Step 4: 找實際產出的檔（yt-dlp 會自動加 .mp4 / .webm 等）──────
    actual = _find_output_file(output_dir, filename_stem)
    if actual is None:
        raise RuntimeError(
            f"yt-dlp 跑完 rc=0 但找不到輸出檔（template={output_template}）。"
            f"stdout: {result.stdout[-500:]}"
        )

    # ── Step 5: 寫回 recording_path ────────────────────────────────────
    with mysql_conn() as conn:
        repo = BroadcastRepo(conn)
        repo.update_recording_path(broadcast_id, str(actual.resolve()))
        conn.commit()
    logger.info("OK 已更新 broadcasts.recording_path")

    return actual


# ─────────────────────────────────────────────────────────────────────────────
def _make_short_id(platform: str, external_id: str) -> str:
    """命名規則：YouTube 取 video_id 前 6 字、Bilibili 取 room_id。"""
    if platform == "youtube":
        return external_id[:6]
    return external_id


def _find_output_file(output_dir: Path, stem: str) -> Path | None:
    """yt-dlp 完成後，找 <stem>.* 中最大的檔（避免拿到 .info.json 等 sidecar）。"""
    candidates = [p for p in output_dir.glob(f"{stem}.*")
                  if p.suffix.lower() in {".mp4", ".webm", ".mkv", ".m4a", ".ts", ".flv"}]
    if not candidates:
        return None
    # 取最大的（最有可能是主影片檔）
    candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
    return candidates[0]

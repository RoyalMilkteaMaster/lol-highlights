"""VOD 補錄範圍推算（階段 3 自動補錄核心）。

設計：
  給 broadcast_id + missing game_index → 推算
    - vod_url        ：broadcast.stream_url（YouTube live 結束會變 archive 同 URL）
    - start_sec      ：上一場 game.end_offset_sec - 30s（第一場 = 0）
    - end_sec        ：start_sec + 60 min（單場 BO3 內 ≤55 min + buffer）
    - team_a / team_b：用 naming_finalizer.derive_series_for_game_index 推算
    - series_id / order：同上

  範圍若超出 broadcast 總時長 → 自動截短。
  上一場 game 不存在（gap） → 回 None（無法精確推算）。

  recover_broadcast_game.py / scheduler cron 都呼叫這個 helper。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass

from automation.db.connection import mysql_conn
from automation.services.naming_finalizer import derive_series_for_game_index

logger = logging.getLogger(__name__)

# 用 broadcast 總時長 ÷ 總場數均分 + ±15 min padding
# 比「上一場 game.end_offset_sec + 60 min」更穩 — 不依賴前面 game 切的對不對。
_PADDING_SEC = 15 * 60  # ±15 min padding 兩端


@dataclass
class RecoveryPlan:
    broadcast_id: int
    game_index: int
    vod_url: str
    start_sec: int
    end_sec: int
    team_a: str
    team_b: str
    series_id: int | None
    series_order: int | None
    league_code: str

    def to_cli_args(self) -> list[str]:
        """轉成 recover_broadcast_game.py CLI args。"""
        args = [
            "--broadcast-id", str(self.broadcast_id),
            "--vod-url", self.vod_url,
            "--start-sec", str(self.start_sec),
            "--end-sec", str(self.end_sec),
            "--game-index", str(self.game_index),
            "--team-a", self.team_a,
            "--team-b", self.team_b,
        ]
        if self.series_id:
            args += ["--series-id", str(self.series_id)]
        if self.series_order:
            args += ["--series-order", str(self.series_order)]
        return args


def _fetch_broadcast(conn, broadcast_id: int) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT broadcast_id, league_code, broadcast_date, stream_url, vod_url, "
            "       actual_start_utc, actual_end_utc, scheduled_start_utc, scheduled_end_utc, "
            "       recording_started_at, recording_ended_at "
            "FROM broadcasts WHERE broadcast_id=%s",
            (broadcast_id,),
        )
        return cur.fetchone()


def _vod_duration_via_ytdlp(vod_url: str) -> int | None:
    """call yt-dlp 拿 VOD duration（秒），用來算總時長。

    對 LCK / LCP / LPL archive VOD 都有效。費時約 5-15 秒（不下載，只拿 metadata）。
    """
    try:
        r = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-playlist",
             "--print", "%(duration)s", vod_url],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if r.returncode != 0:
            logger.warning("[vod_planner] yt-dlp duration 失敗 rc=%s: %s",
                           r.returncode, r.stderr[-300:])
            return None
        out = r.stdout.strip().splitlines[-1] if r.stdout.strip() else ""
        return int(float(out)) if out and out != "NA" else None
    except Exception:
        logger.exception("[vod_planner] yt-dlp duration 例外")
        return None


def _broadcast_total_seconds(broadcast: dict) -> int | None:
    """fallback：從 actual_start/end 算 broadcast 總長；沒填用 scheduled / recording_started/ended。"""
    start = (broadcast.get("actual_start_utc")
             or broadcast.get("scheduled_start_utc")
             or broadcast.get("recording_started_at"))
    end = (broadcast.get("actual_end_utc")
           or broadcast.get("scheduled_end_utc")
           or broadcast.get("recording_ended_at"))
    if start and end:
        return int((end - start).total_seconds())
    return None


def _total_games_for_broadcast(conn, broadcast_id: int) -> int:
    """從 broadcast_series + series.score_a/score_b 累加總場數。

    若某 series 還在 inProgress（score 為 0/None）→ 用 best_of 上限保守估計。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.score_a, s.score_b, s.best_of, s.status
            FROM broadcast_series bs
            JOIN series s ON s.series_id = bs.series_id
            WHERE bs.broadcast_id = %s
            ORDER BY bs.series_order
            """,
            (broadcast_id,),
        )
        rows = list(cur.fetchall())
    total = 0
    for r in rows:
        played = (r["score_a"] or 0) + (r["score_b"] or 0)
        if played > 0:
            total += played
        else:
            # series 沒 score 用 best_of 保守估
            total += int(r["best_of"]) if r["best_of"] else 3
    return total


def plan_recovery(broadcast_id: int, game_index: int) -> RecoveryPlan | None:
    """主入口：自動推算補錄範圍 + team codes。

    範圍推算方法：
      total_duration_sec = actual_end_utc - actual_start_utc（broadcast 總長）
      total_games = sum(series.score_a + series.score_b)（總場數）
      第 N 場的時間切片 = ((N-1)/total_games × total) ~ (N/total_games × total)
      ±15 min padding 兩端 → 預留 BP / end_graph buffer
    """
    with mysql_conn() as conn:
        broadcast = _fetch_broadcast(conn, broadcast_id)
        if not broadcast:
            logger.warning("[vod_planner] broadcast %s 不存在", broadcast_id)
            return None

        vod_url = broadcast.get("vod_url") or broadcast.get("stream_url")
        if not vod_url:
            logger.warning("[vod_planner] broadcast %s 沒 vod_url / stream_url", broadcast_id)
            return None

        # 優先用 yt-dlp 拿 VOD 真實時長（最準），fallback 到 DB recording_started/ended_at
        total_sec = _vod_duration_via_ytdlp(vod_url)
        if not total_sec:
            total_sec = _broadcast_total_seconds(broadcast)
            if total_sec:
                logger.info("[vod_planner] yt-dlp duration 失敗，fallback DB 時長 %ds",
                            total_sec)
        if not total_sec:
            logger.warning(
                "[vod_planner] broadcast %s 拿不到 VOD 時長（yt-dlp + DB 都失敗）",
                broadcast_id,
            )
            return None

        total_games = _total_games_for_broadcast(conn, broadcast_id)
        if total_games <= 0:
            logger.warning("[vod_planner] broadcast %s 算不出總場數", broadcast_id)
            return None
        if game_index > total_games:
            logger.warning(
                "[vod_planner] broadcast %s game_index %d 超出總場數 %d",
                broadcast_id, game_index, total_games,
            )
            return None

        # 均分 + ±15 min padding
        slice_start = ((game_index - 1) / total_games) * total_sec
        slice_end = (game_index / total_games) * total_sec
        start_sec = max(0, int(slice_start) - _PADDING_SEC)
        end_sec = min(int(total_sec), int(slice_end) + _PADDING_SEC)
        logger.info(
            "[vod_planner] 均分推算：total=%ds (%dmin) 場數=%d 第%d場切片=[%d, %d] +padding=[%d, %d]",
            total_sec, total_sec // 60, total_games, game_index,
            int(slice_start), int(slice_end), start_sec, end_sec,
        )

    # team codes / series_id / series_order 推算
    derived = derive_series_for_game_index(broadcast_id, game_index)
    if not derived or not derived.get("team_a_code") or not derived.get("team_b_code"):
        logger.warning(
            "[vod_planner] broadcast %s game_index %d 無法 derive team codes（series 未 completed？）",
            broadcast_id, game_index,
        )
        return None

    series_order = None
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT series_order FROM broadcast_series "
                "WHERE broadcast_id=%s AND series_id=%s",
                (broadcast_id, derived["series_id"]),
            )
            row = cur.fetchone()
            if row:
                series_order = row["series_order"]

    plan = RecoveryPlan(
        broadcast_id=broadcast_id,
        game_index=game_index,
        vod_url=vod_url,
        start_sec=start_sec,
        end_sec=end_sec,
        team_a=derived["team_a_code"],
        team_b=derived["team_b_code"],
        series_id=derived["series_id"],
        series_order=series_order,
        league_code=broadcast["league_code"],
    )
    logger.info(
        "[vod_planner] broadcast=%d game_index=%d → %s vs %s [%d, %d] (%.1f min) league=%s",
        broadcast_id, game_index, plan.team_a, plan.team_b,
        plan.start_sec, plan.end_sec, (plan.end_sec - plan.start_sec) / 60,
        plan.league_code,
    )
    return plan


if __name__ == "__main__":
    # CLI: python -m automation.services.vod_recovery_planner <broadcast_id> <game_index>
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    if len(sys.argv) != 3:
        print("usage: python -m automation.services.vod_recovery_planner <broadcast_id> <game_index>")
        sys.exit(1)
    plan = plan_recovery(int(sys.argv[1]), int(sys.argv[2]))
    if not plan:
        print("無法推算（看 log）")
        sys.exit(1)
    print(plan)
    print("CLI args:", " ".join(plan.to_cli_args))

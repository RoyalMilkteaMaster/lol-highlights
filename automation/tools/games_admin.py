"""games_admin：手動運維 broadcast_games / detector_state。

4 個 sub-command：
- list             ：印 broadcast 的所有 game（status / 切點 / game_path）
- enqueue          ：把指定 game_id 重新 enqueue 到 clip_jobs
- delete           ：刪 broadcast_games row（連同它的 clip_jobs）
- reset-detector   ：把 broadcast_detector_state 改回 active（讓 worker 重跑）

跑法：
  python -m automation.tools.games_admin list <broadcast_id>
  python -m automation.tools.games_admin enqueue <game_id>
  python -m automation.tools.games_admin delete <game_id>
  python -m automation.tools.games_admin reset-detector <broadcast_id>
"""

from __future__ import annotations

import argparse
import sys

from automation.db.connection import mysql_conn
from automation.db.repositories import (
    BroadcastGameRepo,
    ClipJobRepo,
    DetectorStateRepo,
)


def _fmt_hms(sec: float | None) -> str:
    if sec is None:
        return "  None  "
    s = int(sec)
    return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"


# ─────────────────────────────────────────────────────────────────────────────
def cmd_list(broadcast_id: int) -> int:
    with mysql_conn() as conn:
        games = BroadcastGameRepo(conn).find_by_broadcast(broadcast_id)
        ds = DetectorStateRepo(conn).get(broadcast_id)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cj.game_id, cj.status AS job_status, cj.error_message "
                "FROM clip_jobs cj "
                "JOIN broadcast_games g ON g.game_id = cj.game_id "
                "WHERE g.broadcast_id=%s",
                (broadcast_id,),
            )
            jobs = {row["game_id"]: row for row in cur.fetchall()}

    if ds:
        print(f"detector_state: status={ds['detector_status']} "
              f"last_run={ds['last_run_at']} scanned_until={ds['last_scan_until_sec']}")
    else:
        print("detector_state: (no row)")

    if not games:
        print(f"broadcast {broadcast_id} 沒有 game row")
        return 0

    print()
    print(f"{'game_id':<10} {'idx':<4} {'status':<10} {'start':<10} {'end':<10} "
          f"{'src':<22} {'job':<10} game_path")
    print("-" * 110)
    for g in games:
        job = jobs.get(g["game_id"])
        job_status = job["job_status"] if job else "-"
        gp = g["game_path"] or ""
        print(
            f"{g['game_id']:<10} {g['game_index']:<4} {g['status']:<10} "
            f"{_fmt_hms(g['start_offset_sec']):<10} {_fmt_hms(g['end_offset_sec']):<10} "
            f"{(g['end_source'] or ''):<22} {job_status:<10} {gp}"
        )
    return 0


# ─────────────────────────────────────────────────────────────────────────────
def cmd_enqueue(game_id: int) -> int:
    with mysql_conn() as conn:
        repo = BroadcastGameRepo(conn)
        g = repo.get_by_id(game_id)
        if not g:
            print(f"找不到 game_id={game_id}", file=sys.stderr)
            return 2
        if not g["game_path"]:
            print(f"game_id={game_id} 還沒切（status={g['status']}），不能 enqueue",
                  file=sys.stderr)
            return 2
        job_id = ClipJobRepo(conn).enqueue_or_reset_failed(game_id)
        print(f"enqueued game_id={game_id} → job_id={job_id}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
def cmd_delete(game_id: int) -> int:
    """刪 broadcast_games row（CASCADE 會帶走它的 clip_jobs）。"""
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT broadcast_id, game_index FROM broadcast_games WHERE game_id=%s",
                        (game_id,))
            row = cur.fetchone()
            if not row:
                print(f"找不到 game_id={game_id}", file=sys.stderr)
                return 2
            cur.execute("DELETE FROM broadcast_games WHERE game_id=%s", (game_id,))
        conn.commit()
    print(f"deleted game_id={game_id} (broadcast={row['broadcast_id']} "
          f"game_index={row['game_index']})")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
def cmd_reset_detector(broadcast_id: int) -> int:
    """把 detector_status 改回 active，讓 live_split_worker 下輪重跑該 broadcast。"""
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcast_detector_state SET detector_status='active', "
                "  error_message=NULL "
                "WHERE broadcast_id=%s",
                (broadcast_id,),
            )
            affected = cur.rowcount
        conn.commit()
    if affected == 0:
        print(f"broadcast {broadcast_id} 沒有 detector_state row（worker 跑過再試）")
        return 1
    print(f"reset detector_status='active' for broadcast {broadcast_id}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    p = argparse.ArgumentParser(prog="games_admin",
                                description="broadcast_games 運維工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    s_list = sub.add_parser("list", help="列出 broadcast 的所有 game")
    s_list.add_argument("broadcast_id", type=int)

    s_enq = sub.add_parser("enqueue", help="把 game_id 重新放回 clip_jobs queue")
    s_enq.add_argument("game_id", type=int)

    s_del = sub.add_parser("delete", help="刪除 broadcast_games row")
    s_del.add_argument("game_id", type=int)

    s_rst = sub.add_parser("reset-detector",
                           help="重置 detector_state 讓 worker 重跑")
    s_rst.add_argument("broadcast_id", type=int)

    args = p.parse_args()

    if args.cmd == "list":
        sys.exit(cmd_list(args.broadcast_id))
    elif args.cmd == "enqueue":
        sys.exit(cmd_enqueue(args.game_id))
    elif args.cmd == "delete":
        sys.exit(cmd_delete(args.game_id))
    elif args.cmd == "reset-detector":
        sys.exit(cmd_reset_detector(args.broadcast_id))


if __name__ == "__main__":
    main()
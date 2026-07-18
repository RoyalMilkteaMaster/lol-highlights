"""手動覆寫某場 game 的 series_id / series_order / team_codes，
   並加 [MANUAL_OVERRIDE] flag 讓 naming_finalizer 不再碰它。

Usage:
  python -m automation.tools.manual_override_game <game_id> \\
         --series-id <series_id> --order <N> --team-a <CODE> --team-b <CODE> \\
         [--clear-timeline] [--mark-failed]

範例（5/20 broadcast 355 IRL game 1 漏錄、之後 g113 是 NS vs KT g1）：
  python -m automation.tools.manual_override_game 113 \\
         --series-id 20260520010020 --order 1 --team-a NS --team-b KT \\
         --clear-timeline

只標失敗跳過（不改 series 連結）：
  python -m automation.tools.manual_override_game 111 --mark-failed
"""
import argparse
from automation.db.connection import mysql_conn


def main():
    p = argparse.ArgumentParser(description="手動覆寫 game 的 series binding。")
    p.add_argument("game_id", type=int)
    p.add_argument("--series-id", type=int, help="覆寫 series_id")
    p.add_argument("--order", type=int, help="series_order（series 內第幾局）")
    p.add_argument("--team-a", help="team_a_code")
    p.add_argument("--team-b", help="team_b_code")
    p.add_argument("--clear-timeline", action="store_true",
                   help="清掉 timeline_external_id / source / duration，強制 clip_worker 重 fetch")
    p.add_argument("--mark-failed", action="store_true",
                   help="只標 [MANUAL_SKIP] 表示這場錄製失敗、naming_finalizer 不要碰")
    args = p.parse_args()

    sets = []
    params: list = []
    if args.series_id is not None:
        sets.append("series_id=%s"); params.append(args.series_id)
    if args.order is not None:
        sets.append("series_order=%s"); params.append(args.order)
    if args.team_a:
        sets.append("team_a_code=%s"); params.append(args.team_a)
    if args.team_b:
        sets.append("team_b_code=%s"); params.append(args.team_b)
    if args.clear_timeline:
        sets.append("timeline_external_id=NULL")
        sets.append("timeline_source=NULL")
        sets.append("timeline_game_duration_sec=NULL")
        sets.append("timeline_anchor_sec=NULL")
        sets.append("timeline_verified=0")
    # 一律加 manual flag 並清 naming_provisional
    flag = "[MANUAL_SKIP]" if args.mark_failed else "[MANUAL_OVERRIDE]"
    sets.append("error_message=%s"); params.append(f"{flag} manually set via CLI")
    sets.append("naming_provisional=0")

    params.append(args.game_id)
    sql = f"UPDATE broadcast_games SET {', '.join(sets)} WHERE game_id=%s"

    with mysql_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        cur.execute("SELECT game_id, series_id, series_order, team_a_code, team_b_code, "
                    "timeline_external_id, error_message FROM broadcast_games WHERE game_id=%s",
                    (args.game_id,))
        row = cur.fetchone()
        print(f"[OK] game {args.game_id} updated:")
        for k, v in row.items():
            print(f"  {k} = {v}")


if __name__ == "__main__":
    main()

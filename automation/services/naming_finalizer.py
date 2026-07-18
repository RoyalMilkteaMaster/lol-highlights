"""命名最終化：對 LCK / LCP 多 series broadcast 推算每 game 屬哪 series 並 rename。

設計：
  clip_worker 跑完 main.py → highlight 出爐 → 呼叫 try_finalize_naming(game_id):
    - 單 series broadcast（90% 場景，含 LPL bilibili_vod / LCK 單場直播）：
      不 refetch（命名已正確），直接設 naming_provisional=false。
    - 多 series broadcast（LCK 雙場 / 世界賽多場 BO3 連播）：
      refetch lolesports 比分 → 推算 game_index 屬哪 series → rename game.mp4 / highlight.mp4。
      失敗（前 series 仍 inProgress）→ 保留 naming_provisional=true，等 cron 重試。

  scheduler cron `_periodic_naming_retry` 每 5 min 撈 naming_provisional=true AND attempts < 4 重試。
  attempts=4 達上限 → 接受暫命名（不再重試）。

  最壞情況：lolesports 延遲超過 20 min，game 永遠暫命名（user 看到後手動 rename 或視覺接受）。
"""

from __future__ import annotations

import logging
import shutil
from datetime import date
from pathlib import Path

from automation.db.connection import mysql_conn

logger = logging.getLogger(__name__)

# highlight 輸出目錄（跟 main.py 一致）
# 改讀 highlight.utils.paths，支援跨機器 VIDEO_DIR env override
def _finals_dir() -> Path:
    from highlight.utils import paths as _paths
    return _paths.finals_dir()

# 保留舊變數名給依賴它的程式碼（lazy property）
_FINALS_DIR = _finals_dir


# ── refetch lolesports（重用 ScraperPipeline.run）──────────────────────────────
def refetch_lolesports_for_broadcast(broadcast_id: int) -> bool:
    """對 broadcast 對應 league 重 fetch lolesports，更新 series.score_a / score_b / status。

    回 True：refetch 完成（不代表 series 已 completed，要呼叫端 check）。
    回 False：refetch 失敗（log 已紀錄）。
    """
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT league_code, broadcast_date FROM broadcasts WHERE broadcast_id=%s",
                (broadcast_id,),
            )
            row = cur.fetchone()
    if not row:
        logger.warning("[naming_finalizer] broadcast %s 不存在", broadcast_id)
        return False

    league = row["league_code"]
    bcast_date = row["broadcast_date"]
    days_back = max(1, (date.today() - bcast_date).days + 1)

    try:
        from automation.pipeline import ScraperPipeline
        pipeline = ScraperPipeline()
        pipeline.run(league_codes=[league], days_ahead=0, days_back=days_back)
        logger.info(
            "[naming_finalizer] refetch lolesports league=%s days_back=%d 完成（broadcast %s）",
            league, days_back, broadcast_id,
        )
        return True
    except Exception:
        logger.exception("[naming_finalizer] refetch failed broadcast %s", broadcast_id)
        return False


def apply_hupu_fallback_for_broadcast(broadcast_id: int) -> int:
    """用虎撲 web API cache 補 lolesports 沒給的 series 比分。

    ：除 LCP 外**以 hupu 為準**。SeriesRepo.update_score_only 寫入時會把
    score_source='hupu' 鎖住，lolesports 後續 refetch 不再覆蓋 score / status。

    Flow：
        1. 先 sync_if_stale → 5 min 內 cache 還新就跳過、否則重抓 hupu API
        2. SELECT broadcast_series → 對每 series 查 hupu_match_scores
        3. hupu 有 COMPLETED 且 DB series 還非 completed → UPDATE 比分（forward-only）

    LCK 多半 hupu 跟 lolesports 一致，這邊兩來源寫一樣比分。
    LCP / LCS hupu 不報，fallback 跳過 → 仍 100% 靠 lolesports（score_source 保留 lolesports）。

    回傳：實際更新的 series 數（>=0）。失敗回 0（log 已紀錄）。
    """
    # On-demand sync（5 min throttle 防同 cron 內連發）
    try:
        from automation.services.hupu_sync import sync_if_stale
        sync_if_stale(max_age_minutes=5.0)
    except Exception:
        logger.exception("[hupu_fallback] sync_if_stale 失敗 — 仍嘗試讀現有 cache")

    try:
        with mysql_conn() as conn:
            from automation.db.repositories import HupuScoreRepo, SeriesRepo, TeamRepo
            hupu_repo = HupuScoreRepo(conn)
            team_repo = TeamRepo(conn)
            series_repo = SeriesRepo(conn, team_repo)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT bs.series_id, s.match_date, l.code AS league_code,
                           s.status, s.score_a, s.score_b,
                           ta.code AS team_a_code, tb.code AS team_b_code
                    FROM broadcast_series bs
                    JOIN series s ON s.series_id = bs.series_id
                    JOIN leagues l ON l.league_id = s.league_id
                    LEFT JOIN teams ta ON ta.team_id = s.team_a_id
                    LEFT JOIN teams tb ON tb.team_id = s.team_b_id
                    WHERE bs.broadcast_id = %s
                    """,
                    (broadcast_id,),
                )
                series_list = list(cur.fetchall())

        updated = 0
        for s in series_list:
            # 已 completed 的不動（避免反覆覆蓋）
            if s["status"] == "completed":
                continue
            # 隊伍 code 不齊跳過（資料異常，比對不準）
            if not s["team_a_code"] or not s["team_b_code"]:
                continue
            with mysql_conn() as conn:
                hupu_repo = HupuScoreRepo(conn)
                hupu_row = hupu_repo.find_for_series(
                    league_code=s["league_code"],
                    match_date=s["match_date"],
                    team_a_code=s["team_a_code"],
                    team_b_code=s["team_b_code"],
                )
            if hupu_row is None:
                continue
            # 只對 hupu COMPLETED 的更新（INPROGRESS / NOTSTARTED 沒幫助）
            if hupu_row["series_status"] != "COMPLETED":
                continue
            # 隊伍順序若反，score 對調
            if hupu_row["_reversed"]:
                hupu_a, hupu_b = hupu_row["score_b"], hupu_row["score_a"]
            else:
                hupu_a, hupu_b = hupu_row["score_a"], hupu_row["score_b"]
            with mysql_conn() as conn:
                team_repo = TeamRepo(conn)
                series_repo = SeriesRepo(conn, team_repo)
                ok = series_repo.update_score_only(
                    series_id=s["series_id"],
                    score_a=int(hupu_a),
                    score_b=int(hupu_b),
                    status="completed",
                )
                if ok:
                    conn.commit()
                    updated += 1
                    logger.info(
                        "[hupu_fallback] series %s 從 hupu 補比分：%s %d-%d %s (broadcast %s)",
                        s["series_id"], s["team_a_code"], hupu_a, hupu_b,
                        s["team_b_code"], broadcast_id,
                    )

        return updated
    except Exception:
        logger.exception("[hupu_fallback] broadcast %s 失敗（不影響 lolesports 流程）", broadcast_id)
        return 0


# ── 推算 game_index 屬哪 series ───────────────────────────────────────────────
def derive_series_for_game_index(broadcast_id: int, game_index: int) -> dict | None:
    """用 broadcast 對應 series 的比分推算 game_index 屬哪 series。

    演算法：
      effective_index = game_index + broadcast.series_index_offset
        - series_index_offset：漏錄前 N 場時 user 設 N（5/20 broadcast 355 例：漏 IRL g1 → offset=1）

      [MANUAL_SKIP] 不影響 cumulative（5/21 user 確認 Option A 語意）：
        失敗的 game (FATAL_NO_BP / zombie) IRL 真的發生過，只是 highlight 失敗。
        該 row 仍佔 series_order 名額，避免 cut content 跟 series_order 跑掉。
        naming_finalizer 在 try_finalize_naming entry 看 [MANUAL_SKIP] flag 直接 skip 不處理該 row。

      撈 broadcast_series 按 series_order 排
      對每 series 算 actual_games：
        - completed → score_a + score_b
        - inProgress：用 best_of 算 min/max bound
          - BO3 → min=2 / max=3，BO5 → min=3 / max=5
          - effective_index ≤ cumulative + min_games → 一定在這 series（safe，回）
          - effective_index > cumulative + max_games → 一定不在這 series（fall through）
          - 介於兩者之間 → 不知道（ambiguous），是 last series 用 max 兜底，否則回 None

    回傳 dict 含 {series_id, team_a_code, team_b_code, status, actual_games, local_game_index}
    或 None（推算不出）。
    """
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT bs.series_id, bs.series_order, s.best_of,
                       s.score_a, s.score_b, s.status,
                       ta.code AS team_a, tb.code AS team_b
                FROM broadcast_series bs
                JOIN series s ON s.series_id = bs.series_id
                LEFT JOIN teams ta ON ta.team_id = s.team_a_id
                LEFT JOIN teams tb ON tb.team_id = s.team_b_id
                WHERE bs.broadcast_id = %s
                ORDER BY bs.series_order
                """,
                (broadcast_id,),
            )
            series_list = list(cur.fetchall())

            # 拿 broadcast 的 series_index_offset（漏錄補償）
            cur.execute("SELECT series_index_offset FROM broadcasts WHERE broadcast_id=%s",
                        (broadcast_id,))
            _row = cur.fetchone() or {}
            offset = int(_row.get("series_index_offset") or 0)

    if not series_list:
        logger.warning("broadcast %s 沒對到任何 series", broadcast_id)
        return None

    # [MANUAL_SKIP] 不影響 cumulative（Option A 語意，5/21 確認）：
    #   IRL 真比賽發生過、只是 highlight 失敗 → 該 row 仍佔 series_order 一席
    #   naming_finalizer 在 try_finalize_naming entry 看 flag 就直接 skip 不處理該 row
    #   derive 自己不需要扣
    effective_index = game_index + offset
    if offset:
        logger.info(
            "[naming_finalizer] broadcast %s g%s effective_index=%d "
            "(game_index=%d + offset=%d)",
            broadcast_id, game_index, effective_index, game_index, offset,
        )

    cumulative = 0
    for idx, s in enumerate(series_list):
        is_last = (idx == len(series_list) - 1)
        completed = (s["status"] == "completed"
                     and s["score_a"] is not None and s["score_b"] is not None)
        bo = int(s["best_of"]) if s["best_of"] else 5
        min_games = bo // 2 + 1  # BO3→2, BO5→3, BO1→1
        max_games = bo

        if completed:
            actual_games = int(s["score_a"]) + int(s["score_b"])
        else:
            # inProgress：min/max bound 判斷
            #   ≤ cumulative + min_games → 一定在這 series
            #   > cumulative + max_games → 一定不在 → fall through 看下一個
            #   介於兩者之間 → ambiguous
            if effective_index <= cumulative + min_games:
                # safe assign
                return {
                    "series_id":         s["series_id"],
                    "team_a_code":       s["team_a"],
                    "team_b_code":       s["team_b"],
                    "status":            s["status"],
                    "actual_games":      max_games,
                    "local_game_index":  effective_index - cumulative,
                }
            if effective_index > cumulative + max_games:
                # 一定在後面的 series → 用 max_games 累積往下找
                cumulative += max_games
                continue
            # ambiguous（cumulative + min_games < effective_index ≤ cumulative + max_games）
            if is_last:
                # 最後一個 series：直接 assign（沒有後續 series 可推到）
                return {
                    "series_id":         s["series_id"],
                    "team_a_code":       s["team_a"],
                    "team_b_code":       s["team_b"],
                    "status":            s["status"],
                    "actual_games":      max_games,
                    "local_game_index":  effective_index - cumulative,
                }
            # 不是最後一個 series + ambiguous → 等比分定案
            logger.info(
                "[naming_finalizer] broadcast %s series_order %d inProgress 且 "
                "effective_index=%d 在 min/max 之間 ambiguous（min=%d, max=%d）"
                " → 等 hupu 比分 cron 重試",
                broadcast_id, s["series_order"], effective_index,
                cumulative + min_games, cumulative + max_games,
            )
            return None

        if cumulative < effective_index <= cumulative + actual_games:
            return {
                "series_id":         s["series_id"],
                "team_a_code":       s["team_a"],
                "team_b_code":       s["team_b"],
                "status":            s["status"],
                "actual_games":      actual_games,
                "local_game_index":  effective_index - cumulative,
            }
        cumulative += actual_games

    logger.warning(
        "[naming_finalizer] broadcast %s game_index=%d effective=%d 超出累積 (%d) — series 比分可能不對",
        broadcast_id, game_index, effective_index, cumulative,
    )
    return None


# ── rename game.mp4 + highlight.mp4 + UPDATE DB ───────────────────────────────
def rename_game_files(
    game_id: int,
    new_team_a: str,
    new_team_b: str,
    *,
    local_game_index: int | None = None,
) -> bool:
    """rename game.mp4 + highlight.mp4 + UPDATE DB（broadcast_games）。

    新命名：<LEAGUE>_<YYYYMMDD>_<TA>vs<TB>_g<N>.mp4
    N 用 local_game_index（series 內第幾局，非 broadcast-wide）— LCK 雙場 broadcast
    KRX/GEN g1 才不會被命成 "KRXvsGEN_g3"。沒傳則 fallback broadcast-wide game_index。

    若新檔案已存在 → 跳過 rename（避免覆蓋）。
    若 game.mp4 不存在 → return False。
    highlight.mp4 不存在 → 仍 rename game.mp4 + UPDATE DB（剪輯還沒跑或已被刪）。
    """
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT g.game_id, g.broadcast_id, g.game_index, g.game_path,
                       b.league_code, b.broadcast_date
                FROM broadcast_games g
                JOIN broadcasts b ON b.broadcast_id = g.broadcast_id
                WHERE g.game_id = %s
                """,
                (game_id,),
            )
            row = cur.fetchone()
    if not row:
        logger.warning("[naming_finalizer] game_id %s 不存在", game_id)
        return False

    league = row["league_code"]
    date_str = row["broadcast_date"].strftime("%Y%m%d")
    file_index = local_game_index if local_game_index is not None else row["game_index"]
    new_team_a = new_team_a.upper()
    new_team_b = new_team_b.upper()
    new_stem = f"{league}_{date_str}_{new_team_a}vs{new_team_b}_g{file_index}"

    if not row["game_path"]:
        logger.warning("[naming_finalizer] game %s 沒 game_path（可能還沒切）", game_id)
        return False
    old_game_path = Path(row["game_path"])
    if not old_game_path.is_file():
        # game.mp4 已被刪（清空間時人手動刪），不擋 finalize：
        # 仍 UPDATE DB team codes + 嘗試 rename highlight（如果還在）→ 讓 caller 可 clear prov。
        logger.info(
            "[naming_finalizer] game %s 實體檔已刪（清空間 case）→ 只 UPDATE DB，不動實體",
            game_id,
        )
        # 計算新檔名 + path（給 DB 用，雖然檔不存在）
        # Phase 55：folder 也用對的 series prefix（跟主邏輯一致）
        league_x = row["league_code"]
        date_str_x = row["broadcast_date"].strftime("%Y%m%d")
        file_idx_x = local_game_index if local_game_index is not None else row["game_index"]
        new_stem_x = f"{league_x}_{date_str_x}_{new_team_a.upper()}vs{new_team_b.upper()}_g{file_idx_x}"
        new_folder_x = f"{league_x}_{date_str_x}_{new_team_a.upper()}vs{new_team_b.upper()}"
        new_path_x = old_game_path.parent.parent / new_folder_x / f"{new_stem_x}.mp4"
        # 嘗試 rename highlight（如果還在）
        old_stem_x = old_game_path.stem
        old_h = _FINALS_DIR / f"{old_stem_x}_highlights.mp4"
        new_h = _FINALS_DIR / f"{new_stem_x}_highlights.mp4"
        if old_h.is_file() and not new_h.exists() and old_stem_x != new_stem_x:
            try:
                old_h.rename(new_h)
                logger.info("[naming_finalizer] [OK] rename highlight only %s → %s",
                            old_h.name, new_h.name)
            except OSError as e:
                logger.warning("[naming_finalizer] rename highlight 失敗：%s", e)
        # UPDATE DB（game_path 仍指 new_path，即使檔不存在 — 與既有命名規則對齊）
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE broadcast_games SET game_path=%s, team_a_code=%s, team_b_code=%s "
                    "WHERE game_id=%s",
                    (str(new_path_x.resolve()), new_team_a.upper(), new_team_b.upper(), game_id),
                )
            conn.commit()
        logger.info(
            "[naming_finalizer] [OK] DB updated (no file) game_id=%s team=%s vs %s",
            game_id, new_team_a.upper(), new_team_b.upper(),
        )
        return True

    old_stem = old_game_path.stem
    # Phase 55：folder 也用對的 series prefix（不只 file basename）
    new_folder_name = f"{league}_{date_str}_{new_team_a}vs{new_team_b}"
    new_folder = old_game_path.parent.parent / new_folder_name
    new_game_path = new_folder / f"{new_stem}.mp4"

    if old_stem == new_stem and old_game_path.parent == new_folder:
        logger.info("[naming_finalizer] game %s 已是新命名 %s", game_id, new_stem)
        return True

    # RACE GUARD：避免 main.py 跑中時 rename，會讓 cv2.VideoCapture 開到舊路徑失敗
    # clip_jobs.status='running' 時跳過 rename，等 main.py 跑完（done/failed）下輪 cron 再 rename。
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS n FROM clip_jobs WHERE game_id=%s AND status='running'",
                (game_id,),
            )
            if cur.fetchone()["n"] > 0:
                logger.info(
                    "[naming_finalizer] game %s clip_job running → 跳過 rename 等下輪 cron",
                    game_id,
                )
                return False

    # 1. move game.mp4 到新 folder + 新 file 名
    # Phase 55：new exists 時也要 UPDATE DB 同步（disk 可能已被前次 rename 但 DB 沒同步）
    new_folder.mkdir(parents=True, exist_ok=True)
    if new_game_path.exists():
        logger.warning(
            "[naming_finalizer] 新 path %s 已存在 → 跳過 move 但仍 UPDATE DB（保持 disk/DB 一致）",
            new_game_path,
        )
    else:
        try:
            shutil.move(str(old_game_path), str(new_game_path))
            logger.info("[naming_finalizer] [OK] move %s → %s",
                        old_game_path.name, new_game_path)
        except OSError as e:
            logger.error("[naming_finalizer] move game.mp4 失敗：%s", e)
            return False

    # 2. rename highlight.mp4（如果存在；folder 不變，highlights 一律在 _FINALS_DIR）
    old_highlight = _FINALS_DIR / f"{old_stem}_highlights.mp4"
    new_highlight = _FINALS_DIR / f"{new_stem}_highlights.mp4"
    if old_highlight.is_file():
        if new_highlight.exists():
            logger.warning("[naming_finalizer] highlight 新名 %s 已存在，不覆寫",
                           new_highlight)
        else:
            try:
                old_highlight.rename(new_highlight)
                logger.info("[naming_finalizer] [OK] rename highlight %s → %s",
                            old_highlight.name, new_highlight.name)
            except OSError as e:
                logger.warning("[naming_finalizer] rename highlight 失敗（保留 game.mp4 改名）：%s", e)

    # 3. UPDATE DB（即使 move 跳過也仍 UPDATE，確保 disk/DB 一致）
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE broadcast_games
                SET game_path=%s, team_a_code=%s, team_b_code=%s, naming_provisional=FALSE
                WHERE game_id=%s
                """,
                (str(new_game_path.resolve()), new_team_a, new_team_b, game_id),
            )
        conn.commit()
    logger.info("[naming_finalizer] [OK] DB updated game_id=%s team=%s vs %s path=%s",
                game_id, new_team_a, new_team_b, new_game_path)

    # 4. 舊 folder 若已空 → rmdir（避免 GENvsDNS/ 等殘留 prefix folder）
    old_folder = old_game_path.parent
    if old_folder != new_folder and old_folder.is_dir():
        try:
            if not any(old_folder.iterdir()):
                old_folder.rmdir()
                logger.info("[naming_finalizer] rmdir 空 folder %s", old_folder)
        except OSError:
            pass

    return True


# ── 主入口：對單一 game 嘗試確認命名 ──────────────────────────────────────────
def try_finalize_naming(game_id: int) -> bool:
    """嘗試對 game 確認最終命名。

    流程：
      1. broadcast 對應 series 數 ≤ 1 → 直接 done（單 series broadcast 命名永遠正確）
      2. 多 series → refetch lolesports → derive series → rename
      3. 失敗（前 series 仍 inProgress）→ 保留 naming_provisional=true，等 cron 重試

    回 True：命名已 finalized（清 naming_provisional）；False：仍待 retry。
    """
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT g.broadcast_id, g.game_index, g.team_a_code, g.team_b_code,
                       g.naming_provisional, g.naming_attempts, g.error_message,
                       (SELECT COUNT(*) FROM broadcast_series bs
                          WHERE bs.broadcast_id = g.broadcast_id) AS n_series
                FROM broadcast_games g
                WHERE g.game_id = %s
                """,
                (game_id,),
            )
            row = cur.fetchone()
    if not row:
        return False

    # Manual override：error_message 帶 [MANUAL_OVERRIDE] / [MANUAL_SKIP] prefix
    # → user 手動標過 series_order，naming_finalizer 完全不碰（不重設、不重 fetch）
    _err = (row.get("error_message") or "")
    if "[MANUAL_OVERRIDE]" in _err or "[MANUAL_SKIP]" in _err:
        logger.info(
            "[naming_finalizer] game %s 有 manual override flag → skip 整個 finalize 流程",
            game_id,
        )
        if row["naming_provisional"]:
            _clear_provisional(game_id)
        return True

    bid = row["broadcast_id"]
    n_series = int(row["n_series"] or 0)

    # 單 series broadcast → 命名永遠正確，直接清旗標
    if n_series <= 1:
        if row["naming_provisional"]:
            _clear_provisional(game_id)
        return True

    # 多 series → refetch lolesports
    refetch_ok = refetch_lolesports_for_broadcast(bid)
    if not refetch_ok:
        logger.warning("[naming_finalizer] refetch 失敗 game %s — 等 cron 重試", game_id)
        return False

    # lolesports 補完後，用虎撲 web API 補 lolesports 沒給的（特別是 LPL）
    apply_hupu_fallback_for_broadcast(bid)

    info = derive_series_for_game_index(bid, row["game_index"])
    if info is None:
        logger.info(
            "[naming_finalizer] game %s 暫無法推算 series（前 series 仍 inProgress） — 等 cron 重試",
            game_id,
        )
        return False

    # 永遠 link series_id + series_order + team_codes（5/20 g114 bug fix）
    # team_codes 一定要在 fetch_timeline_metadata 前寫對，否則拓錯場
    _set_series_link(game_id, info["series_id"], info["local_game_index"],
                     team_a_code=info.get("team_a_code"),
                     team_b_code=info.get("team_b_code"))

    # series_id 已 link → 跑 timeline anchor 對齊（補強 YOLO end_offset 偵測）
    # 失敗不影響 rename 流程（只是少了 timeline_verified flag）
    try:
        from automation.services.timeline_anchor import adjust_end_offset_for_game
        adjust_end_offset_for_game(game_id)
    except Exception:
        logger.exception("[naming_finalizer] timeline_anchor 失敗 game %s（不影響 rename）", game_id)

    # 永遠 call rename_game_files — 即使 team 名一樣，game_index (broadcast-wide)
    # 仍可能跟 local_game_index (series 內) 不同 → file 名 _g4_ 該變成 _g2_。
    # rename_game_files 內部 old_stem == new_stem 會 idempotent return True。
    ok = rename_game_files(
        game_id, info["team_a_code"], info["team_b_code"],
        local_game_index=info["local_game_index"],
    )
    if not ok:
        return False

    _clear_provisional(game_id)
    return True


def _set_series_link(game_id: int, series_id: int, local_game_index: int,
                     team_a_code: str | None = None, team_b_code: str | None = None) -> None:
    """把推算出的 series_id + series_order + team_codes 寫入 broadcast_games。

    series_order 用 local_game_index（series 內第幾局），跟 derive 回傳一致。
    team_codes 來自 series 表（5/20 g114 bug fix）：
      之前只寫 series_id+series_order，team_codes 留 live_split 寫的 broadcast 預設。
      但 fetch_timeline_metadata 用 team_codes JOIN teams 拿 full name 比對 Cargo，
      預設 T1/KRX 會配錯 → 拓到別場 timeline。
      現在一律覆寫成 series 對應的 team_a/b_code，下游 fetch 才會比對正確。
    """
    sets = ["series_id=%s", "series_order=%s"]
    params: list = [series_id, local_game_index]
    if team_a_code:
        sets.append("team_a_code=%s"); params.append(team_a_code)
    if team_b_code:
        sets.append("team_b_code=%s"); params.append(team_b_code)
    params.append(game_id)
    sql = f"UPDATE broadcast_games SET {', '.join(sets)} WHERE game_id=%s"
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    logger.info("[naming_finalizer] [OK] link game_id=%s → series_id=%s series_order=%s "
                "team=%s/%s",
                game_id, series_id, local_game_index, team_a_code, team_b_code)


def _clear_provisional(game_id: int) -> None:
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE broadcast_games SET naming_provisional=FALSE WHERE game_id=%s",
                (game_id,),
            )
        conn.commit()


# ── cron 入口：撈所有 naming_provisional=true 重試 ─────────────────────────────
def run_periodic_retry(max_attempts: int = 4) -> int:
    """每 5 min 跑一次。撈 naming_provisional=true AND naming_attempts < max_attempts 的 game，
    呼叫 try_finalize_naming。

    若仍失敗 → naming_attempts++（達 max 後 cron 不再撈到此 game）。
    回傳：成功 finalized 的 game 數。
    """
    with mysql_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT game_id FROM broadcast_games
                WHERE naming_provisional = TRUE
                  AND naming_attempts < %s
                  AND status IN ('cut','detecting')
                """,
                (max_attempts,),
            )
            game_ids = [r["game_id"] for r in cur.fetchall()]

    if not game_ids:
        return 0

    logger.info("[naming_finalizer] 重試命名 %d 個 game：%s", len(game_ids), game_ids)
    n_done = 0
    for gid in game_ids:
        ok = try_finalize_naming(gid)
        if ok:
            n_done += 1
        else:
            with mysql_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE broadcast_games SET naming_attempts=naming_attempts+1 "
                        "WHERE game_id=%s",
                        (gid,),
                    )
                conn.commit()
    logger.info("[naming_finalizer] 重試完：%d/%d finalized", n_done, len(game_ids))
    return n_done

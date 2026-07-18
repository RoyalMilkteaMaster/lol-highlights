"""Timeline anchor service — 對齊 game.mp4 end_offset。

對單一 game：
  1. 從 broadcast_games + series 拿到 league / teams / date / series_order
  2. 對應 timeline source：
     - LCK / LCP / 國際賽 → Leaguepedia V5 (cargo by date+team → RPGId → /Timeline)
     - LPL → Bilibili player/v2 view_points（要 BV id，從 broadcasts metadata 拿）
  3. flash icon anchor 偵測（對 game.mp4 跑 get_game_start_anchor）
  4. 算 expected_end = anchor + timeline_GAME_END/1000 + 15s outro
  5. 比 broadcast_games.end_offset_sec - start_offset_sec：
     - diff <= 30s: timeline_verified=1, needs_recut=0
     - diff > 30s 且 YOLO 切太早: needs_recut=1（flag，不自動 recut）
     - diff > 30s 且 YOLO 切太晚: 接受（多錄 outro 不致命）
  6. 寫 broadcast_games timeline_* 欄位

trigger：naming_finalizer.try_finalize_naming 成功 derive_series 後 hook 進來。
"""

from __future__ import annotations

import logging
from pathlib import Path

from automation.db.connection import mysql_conn
from automation.sources.timeline import (
    LEAGUEPEDIA_LEAGUES,
    BILIBILI_LEAGUES,
    GameTimeline,
    cargo_query_games,
    fetch_leaguepedia_timeline,
    fetch_bilibili_view_points,
    fetch_bilibili_view,
)

logger = logging.getLogger(__name__)

# diff > 此值 算 YOLO 切點跟 timeline 不符
DIFF_THRESHOLD_SEC = 30.0
OUTRO_BUFFER_SEC = 15.0


def _select_game_meta(game_id: int) -> dict | None:
    """撈 broadcast_games + broadcasts + series + teams 一次給齊。

    5/21 Race fix：team_a/b_name 改成優先從 series.team_a_id/b_id JOIN teams 拿
    （series_id 有設時最準），fallback 才用 broadcast_games.team_a/b_code。
    避免 live_split insert 時 broadcast 預設 team_codes 跟實際 series 不符 →
    race window 內 fetch_timeline_metadata 拓錯場（g114 5/20 踩到）。
    """
    with mysql_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT bg.game_id, bg.broadcast_id, bg.game_index, bg.series_id, bg.series_order,
                   bg.start_offset_sec, bg.end_offset_sec, bg.game_path,
                   bg.team_a_code, bg.team_b_code,
                   bg.timeline_external_id, bg.timeline_source,
                   b.league_code, b.broadcast_date, b.vod_url, b.external_id AS broadcast_ext,
                   COALESCE(ts_a.name, ta.name) AS team_a_name,
                   COALESCE(ts_b.name, tb.name) AS team_b_name,
                   COALESCE(ts_a.code, bg.team_a_code) AS series_team_a_code,
                   COALESCE(ts_b.code, bg.team_b_code) AS series_team_b_code,
                   s.match_date, s.score_a, s.score_b, s.status AS series_status
            FROM broadcast_games bg
            JOIN broadcasts b ON b.broadcast_id = bg.broadcast_id
            LEFT JOIN series s ON s.series_id = bg.series_id
            LEFT JOIN teams ts_a ON ts_a.team_id = s.team_a_id
            LEFT JOIN teams ts_b ON ts_b.team_id = s.team_b_id
            LEFT JOIN teams ta ON ta.code = bg.team_a_code
            LEFT JOIN teams tb ON tb.code = bg.team_b_code
            WHERE bg.game_id = %s
        """, (game_id,))
        return cur.fetchone()


def _find_rpgi_for_lck_lcp(meta: dict) -> str | None:
    """LCK/LCP: 用 (match_date, team1 full name, team2 full name, N_GameInMatch) 找 RPGId。

    series.team_a/b code 對映到 Leaguepedia full name 透過 teams.name。
    若找不到 → log warning + None。
    """
    match_date = meta.get("match_date") or meta.get("broadcast_date")
    team_a_name = (meta.get("team_a_name") or "").strip()
    team_b_name = (meta.get("team_b_name") or "").strip()
    n_game = meta.get("series_order")
    league = meta.get("league_code")

    if not all([match_date, team_a_name, team_b_name, n_game, league]):
        logger.warning(
            "[timeline_anchor] g%s 缺欄位: date=%s teams=%s/%s n=%s league=%s",
            meta.get("game_id"), match_date, team_a_name, team_b_name, n_game, league,
        )
        return None

    games = cargo_query_games(match_date, league)
    if not games:
        logger.warning(
            "[timeline_anchor] g%s Cargo %s %s 0 hits (cache 可能 indexing)",
            meta.get("game_id"), match_date, league,
        )
        return None

    # 5/25 補強：除了 full name 也比對 short_name (code) — Leaguepedia wiki team name
    # 在比賽剛結束時 lolesports 原始名稱（如 'DN SOOPers'）vs wiki 編輯後版本
    # （如 'Dplus Kia'）可能短暫不一致；用 short code 比對更穩定。
    team_a_code = (meta.get("series_team_a_code") or meta.get("team_a_code") or "").strip()
    team_b_code = (meta.get("series_team_b_code") or meta.get("team_b_code") or "").strip()

    def _match_pair(t1: str, t2: str) -> bool:
        """t1/t2 對 (team_a, team_b) 雙向 + full name + code 三層比對。"""
        # Layer 1: full name 雙向 substring
        n1 = (_team_name_match(t1, team_a_name) and _team_name_match(t2, team_b_name)) or \
             (_team_name_match(t1, team_b_name) and _team_name_match(t2, team_a_name))
        if n1:
            return True
        # Layer 2: code 雙向 substring（"DNS" / "GEN" 對 wiki short name）
        if team_a_code and team_b_code:
            n2 = (_team_name_match(t1, team_a_code) and _team_name_match(t2, team_b_code)) or \
                 (_team_name_match(t1, team_b_code) and _team_name_match(t2, team_a_code))
            if n2:
                return True
        # Layer 3: full vs code 交叉（少見但保險）
        if team_a_code and team_b_code:
            return (_team_name_match(t1, team_a_code) and _team_name_match(t2, team_b_name)) or \
                   (_team_name_match(t1, team_b_code) and _team_name_match(t2, team_a_name)) or \
                   (_team_name_match(t1, team_a_name) and _team_name_match(t2, team_b_code)) or \
                   (_team_name_match(t1, team_b_name) and _team_name_match(t2, team_a_code))
        return False

    for g in games:
        gn = str(g.get("N GameInMatch") or "0")
        if gn != str(n_game):
            continue
        t1 = (g.get("Team1") or "").strip()
        t2 = (g.get("Team2") or "").strip()
        if _match_pair(t1, t2):
            rpgi = g.get("RiotPlatformGameId")
            if rpgi:
                return rpgi
    logger.warning(
        "[timeline_anchor] g%s 沒對到 Cargo (我們 %s/%s vs %s/%s g%s, Cargo 有 %d games)",
        meta.get("game_id"), team_a_name, team_a_code, team_b_name, team_b_code, n_game, len(games),
    )
    return None


def _team_name_match(leaguepedia_name: str, our_name: str) -> bool:
    """寬鬆 team 名比對：substring / case-insensitive。"""
    if not leaguepedia_name or not our_name:
        return False
    a = leaguepedia_name.strip().lower()
    b = our_name.strip().lower()
    return a == b or a in b or b in a


def _fetch_timeline_for_meta(meta: dict) -> GameTimeline | None:
    """依 league 選 source 拓 timeline。

    除了回 GameTimeline 物件，也呼叫 _save_raw 寫 JSON 到 disk
    （否則 clip.py 的 _compute_kill_correlation_anchor_for_game / _filter_segments
     會找不到檔 → 跳過 timeline 處理）。

    normalize match_date — str → date 物件（_save_raw strftime 用）
    """
    from automation.sources.timeline import _save_raw
    from datetime import datetime as _dt, date as _date_cls
    # match_date 可能是 datetime.date() / datetime.datetime / str / None
    raw_md = meta.get("match_date") or meta.get("broadcast_date")
    if raw_md is None:
        logger.warning("[timeline_anchor] g%s match_date is None, skip timeline fetch",
                       meta.get("game_id"))
        return None
    if isinstance(raw_md, str):
        try:
            raw_md = _dt.strptime(raw_md[:10], "%Y-%m-%d").date()
        except ValueError:
            logger.warning("[timeline_anchor] g%s match_date format 錯: %s",
                           meta.get("game_id"), raw_md)
            return None
    elif hasattr(raw_md, "date") and not isinstance(raw_md, _date_cls):
        raw_md = raw_md.date()    # datetime → date
    meta_match_date = raw_md
    league = (meta.get("league_code") or "").upper()

    # disk 先讀避免重複 fetch（idempotency）
    def _try_load_from_disk(source: str, ext_id: str) -> dict | None:
        try:
            from automation.sources.timeline import _timelines_dir
            import re as _re, json as _json
            safe_id = _re.sub(r"[^A-Za-z0-9._-]", "_", ext_id)
            p = _timelines_dir() / meta_match_date.strftime("%Y-%m-%d") / f"{source}_{safe_id}.json"
            if p.is_file():
                with p.open(encoding="utf-8") as f:
                    logger.info("[timeline_anchor] g%s disk hit %s (skip API fetch)",
                                meta.get("game_id"), p.name)
                    return _json.load(f)
        except Exception:
            logger.exception("[timeline_anchor] g%s disk read 失敗",
                             meta.get("game_id"))
        return None

    if league in LEAGUEPEDIA_LEAGUES:
        # 若 DB 已存 RPGI → 直接用，省 Cargo lookup（避免 naming_finalizer cron 一直 re-fetch）
        cached_rpgi = (meta.get("timeline_external_id") or "").strip()
        if cached_rpgi and meta.get("timeline_source") == "leaguepedia":
            rpgi = cached_rpgi
            logger.info("[timeline_anchor] g%s 已知 RPGI=%s，skip Cargo lookup",
                        meta.get("game_id"), rpgi)
        else:
            rpgi = _find_rpgi_for_lck_lcp(meta)
            if not rpgi:
                return None
        # 先嘗試 disk hit
        raw = _try_load_from_disk("leaguepedia", rpgi)
        if raw is None:
            raw = fetch_leaguepedia_timeline(rpgi)
        if raw is None:
            return None
        from automation.sources.timeline import _extract_game_end_ms, _count_events
        # 寫 disk 只在 fetch 來的（disk hit 已存在不重寫）
        if raw is not None:
            try:
                _save_raw(raw, "leaguepedia", rpgi, meta_match_date)
            except Exception:
                logger.exception(
                    "[timeline_anchor] g%s _save_raw 失敗（不影響 fetch return）",
                    meta.get("game_id"),
                )
        return GameTimeline(
            source="leaguepedia",
            external_id=rpgi,
            match_date=meta_match_date,
            game_duration_ms=_extract_game_end_ms(raw),
            events_count=_count_events(raw),
            raw=raw,
        )
    if league in BILIBILI_LEAGUES:
        # 從 broadcast.vod_url 或 external_id 取 BV id
        bvid = _extract_bvid(meta)
        if not bvid:
            logger.warning("[timeline_anchor] g%s LPL 但拿不到 BV id（vod_url=%s）",
                           meta.get("game_id"), meta.get("vod_url"))
            return None
        view = fetch_bilibili_view(bvid)
        if not view:
            return None
        # 找對應 page（game_index 對應 part index）
        pages = view.get("pages") or []
        # 5：series_order 可能跟 BV part index 不對齊
        # 嘗試順序：
        #   1. vod_url 內 ?p=N（最權威）
        #   2. series_order - 1
        #   3. game_index - 1
        idx = None
        vod_url = meta.get("vod_url") or ""
        import re as _re
        m = _re.search(r"[?&]p=(\d+)", vod_url)
        if m:
            idx = int(m.group(1)) - 1   # ?p=N 是 1-based
            logger.info("[timeline_anchor] g%s 用 vod_url ?p=%d 當 BV part",
                        meta.get("game_id"), idx + 1)
        elif meta.get("series_order"):
            idx = meta["series_order"] - 1
        elif meta.get("game_index"):
            idx = meta["game_index"] - 1
        else:
            idx = 0
        if idx < 0 or idx >= len(pages):
            logger.warning("[timeline_anchor] g%s BV part idx=%d 越界（共 %d parts）",
                           meta.get("game_id"), idx, len(pages))
            return None
        cid = pages[idx]["cid"]
        external_id = f"{bvid}_{cid}"
        # 先 disk hit
        raw_disk = _try_load_from_disk("bilibili", external_id)
        if raw_disk is not None:
            vps = raw_disk.get("view_points") or []
            last_t = max((vp.get("to", vp.get("from", 0)) for vp in vps), default=0)
            return GameTimeline(
                source="bilibili",
                external_id=external_id,
                match_date=meta_match_date,
                game_duration_ms=int(last_t) * 1000,
                events_count=len(vps),
                raw=raw_disk,
            )
        vps = fetch_bilibili_view_points(view["aid"], cid)
        if not vps:
            return None
        last_t = max((vp.get("to", vp.get("from", 0)) for vp in vps), default=0)
        raw_packed = {"bvid": bvid, "aid": view["aid"], "cid": cid, "view_points": vps}
        try:
            _save_raw(raw_packed, "bilibili", external_id, meta_match_date)
        except Exception:
            logger.exception("[timeline_anchor] g%s _save_raw 失敗（不影響 fetch return）",
                             meta.get("game_id"))
        return GameTimeline(
            source="bilibili",
            external_id=external_id,
            match_date=meta_match_date,
            game_duration_ms=int(last_t) * 1000,
            events_count=len(vps),
            raw=raw_packed,
        )
    return None


def _extract_bvid(meta: dict) -> str | None:
    """從 broadcast.vod_url 或 external_id 取 BV id。"""
    import re
    url = meta.get("vod_url") or ""
    m = re.search(r"BV[A-Za-z0-9]{10}", url)
    if m:
        return m.group(0)
    ext = meta.get("broadcast_ext") or ""
    m = re.search(r"BV[A-Za-z0-9]{10}", ext)
    if m:
        return m.group(0)
    return None


def _detect_anchor(game_path: Path, max_search_sec: float = 360.0) -> float | None:
    """跑 flash icon anchor 偵測。"""
    if not game_path.is_file():
        logger.warning("[timeline_anchor] game.mp4 不存在: %s", game_path)
        return None
    try:
        # 延遲 import，避免 torch 載入成本
        from highlight import create_yolo_detector
        det = create_yolo_detector(str(game_path))
        anchor = det.get_game_start_anchor(
            search_start_sec=0.0,
            max_search_sec=max_search_sec,
            min_icons=8,
            stride_sec=1.0,
        )
        return anchor
    except Exception:
        logger.exception("[timeline_anchor] anchor 偵測失敗: %s", game_path)
        return None


def fetch_timeline_metadata(game_id: int) -> dict:
    """輕量版 — 只 fetch timeline JSON + 寫 source/external_id/duration 到 DB。
    不跑 flash anchor 偵測（flash anchor 偏移 30-40s 不準，改用 clip.py
    內 SCAN 後 kill_feed × CHAMPION_KILL cross-correlation 算 anchor）。

    這個函式給 clip_worker pre-fetch 用 — 確保 clip.py 進來時 timeline JSON 已 ready。

    回 {'game_id', 'action', 'source', 'external_id', 'game_duration_sec'}
    """
    meta = _select_game_meta(game_id)
    if not meta:
        return {"game_id": game_id, "action": "skip", "reason": "game_not_found"}

    # 拓 timeline
    tl = _fetch_timeline_for_meta(meta)
    if not tl:
        return {"game_id": game_id, "action": "no_timeline",
                "league": meta.get("league_code"),
                "reason": "timeline 拿不到 (Cargo 0 / RPGId empty / 不支援聯賽)"}

    # 寫 DB 前先驗證 disk JSON 真的存在（防 DB / disk 不同步）
    from automation.sources.timeline import _save_raw
    import re as _re
    from highlight.utils import paths as _paths
    timelines_base = _paths.timelines_dir()
    safe_id = _re.sub(r"[^A-Za-z0-9._-]", "_", tl.external_id)
    expected_path = (timelines_base / tl.match_date.strftime("%Y-%m-%d")
                     / f"{tl.source}_{safe_id}.json")
    if not expected_path.is_file():
        # _fetch_timeline_for_meta 內 _save_raw 應該有寫；漏寫的話這裡補救
        logger.warning(
            "[fetch_timeline_metadata] g%s disk JSON 不存在（_save_raw 應該有寫），補救...",
            game_id,
        )
        try:
            _save_raw(tl.raw, tl.source, tl.external_id, tl.match_date)
        except Exception:
            logger.exception("[fetch_timeline_metadata] g%s 補救 _save_raw 失敗", game_id)
        if not expected_path.is_file():
            logger.error(
                "[fetch_timeline_metadata] g%s disk JSON 補救後仍不存在: %s "
                "-> 不寫 DB 避免 clip.py 找不到檔",
                game_id, expected_path,
            )
            return {"game_id": game_id, "action": "no_disk_save",
                    "reason": f"_save_raw 失敗 path={expected_path}"}

    # 寫 DB（source / external_id / game_duration_sec；anchor 留給 clip.py kill_correlation）
    game_duration_sec = tl.game_duration_ms / 1000.0
    with mysql_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE broadcast_games SET timeline_source=%s, timeline_external_id=%s, "
            "  timeline_game_duration_sec=%s WHERE game_id=%s",
            (tl.source, tl.external_id, game_duration_sec, game_id),
        )
        conn.commit()
    logger.info(
        "[fetch_timeline_metadata] g%s [OK] source=%s ext_id=%s duration=%.0fs disk=%s",
        game_id, tl.source, tl.external_id, game_duration_sec, expected_path.name,
    )
    return {
        "game_id": game_id, "action": "ok",
        "source": tl.source,
        "external_id": tl.external_id,
        "game_duration_sec": game_duration_sec,
    }


def adjust_end_offset_for_game(game_id: int, force: bool = False) -> dict:
    """主入口：對單一 game 跑 timeline 對齊 + 寫 DB。

    Args:
        force: True 強制重跑（即使已 verified）；False (預設) 跳過已 verified
    Returns:
        {'game_id', 'action', 'source', 'anchor_sec', 'game_duration_sec',
         'expected_end_offset', 'actual_end_offset', 'diff_sec', 'needs_recut'}
    """
    meta = _select_game_meta(game_id)
    if not meta:
        return {"game_id": game_id, "action": "skip", "reason": "game_not_found"}

    # 已 verified 就 skip，避免 cargo cache miss 時 overwrite 掉 OK 資料
    if not force:
        with mysql_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT timeline_verified, timeline_source, timeline_anchor_sec "
                "FROM broadcast_games WHERE game_id=%s",
                (game_id,),
            )
            prev = cur.fetchone()
        if prev and prev.get("timeline_verified") == 1 and prev.get("timeline_source") not in (None, "none"):
            return {
                "game_id": game_id, "action": "skip_already_verified",
                "source": prev["timeline_source"],
                "anchor_sec": prev.get("timeline_anchor_sec"),
            }

    # 1. 拓 timeline
    tl = _fetch_timeline_for_meta(meta)
    if not tl:
        # 拓不到 → 不 overwrite DB（保留之前可能存在的 verified 資料）
        return {
            "game_id": game_id, "action": "no_timeline",
            "league": meta.get("league_code"),
            "reason": "timeline 拿不到 (Cargo 0 / RPGId empty / 不支援聯賽)",
        }

    # 2. 跑 anchor 偵測
    game_path = Path(meta["game_path"]) if meta.get("game_path") else None
    if not game_path or not game_path.is_file():
        # 檔案被 cleanup 刪了，只記 timeline metadata
        # 傳 external_id 避免清掉 D 策略剛 fetch 的 ext_id
        _save_anchor_result(
            game_id, source=tl.source, external_id=tl.external_id,
            game_duration_sec=tl.game_duration_ms / 1000.0,
            verified=0,
        )
        return {
            "game_id": game_id, "action": "no_file",
            "reason": "game.mp4 已被 cleanup 刪",
            "source": tl.source, "game_duration_sec": tl.game_duration_ms / 1000.0,
        }

    anchor_sec = _detect_anchor(game_path)
    if anchor_sec is None:
        # 傳 external_id 避免清掉 D 策略剛 fetch 的 ext_id
        _save_anchor_result(
            game_id, source=tl.source, external_id=tl.external_id,
            game_duration_sec=tl.game_duration_ms / 1000.0,
            verified=0,
        )
        return {
            "game_id": game_id, "action": "no_anchor",
            "reason": "flash icon anchor 找不到",
            "source": tl.source, "game_duration_sec": tl.game_duration_ms / 1000.0,
        }

    # 3. 算 expected end vs actual
    game_duration_sec = tl.game_duration_ms / 1000.0
    expected_game_end_in_mp4 = anchor_sec + game_duration_sec + OUTRO_BUFFER_SEC
    actual_duration = meta["end_offset_sec"] - meta["start_offset_sec"]
    diff_sec = actual_duration - expected_game_end_in_mp4
    needs_recut = 0
    action = "ok"
    if abs(diff_sec) > DIFF_THRESHOLD_SEC:
        if diff_sec < 0:
            # YOLO 切太早，game.mp4 比 timeline 預測短
            needs_recut = 1
            action = "flag_recut_too_short"
        else:
            # YOLO 切太晚（多錄 outro），可接受
            action = "accepted_too_long"

    # 4. 寫 DB
    _save_anchor_result(
        game_id,
        anchor_sec=anchor_sec,
        game_duration_sec=game_duration_sec,
        source=tl.source,
        external_id=tl.external_id,
        verified=1,
        needs_recut=needs_recut,
    )

    logger.info(
        "[timeline_anchor] g%s %s anchor=%.1fs duration=%.0fs expected_end=%.0fs actual=%.0fs diff=%+.0fs %s",
        game_id, tl.source, anchor_sec, game_duration_sec,
        expected_game_end_in_mp4, actual_duration, diff_sec,
        action,
    )

    return {
        "game_id": game_id, "action": action,
        "source": tl.source,
        "anchor_sec": anchor_sec,
        "game_duration_sec": game_duration_sec,
        "expected_end_offset": meta["start_offset_sec"] + expected_game_end_in_mp4,
        "actual_end_offset": meta["end_offset_sec"],
        "diff_sec": diff_sec,
        "needs_recut": needs_recut,
    }


def _save_anchor_result(
    game_id: int,
    *,
    anchor_sec: float | None = None,
    game_duration_sec: float | None = None,
    source: str | None = None,
    external_id: str | None = None,
    verified: int = 0,
    needs_recut: int = 0,
) -> None:
    # COALESCE 防呼叫者沒傳值時清掉既有資料
    # 譬如 _save_anchor_result(game_id, source=...) 沒給 external_id → COALESCE
    # 會保留 DB 內既有 ext_id 而不會被 NULL 覆寫
    with mysql_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE broadcast_games SET "
            "  timeline_anchor_sec=COALESCE(%s, timeline_anchor_sec), "
            "  timeline_game_duration_sec=COALESCE(%s, timeline_game_duration_sec), "
            "  timeline_source=COALESCE(%s, timeline_source), "
            "  timeline_external_id=COALESCE(%s, timeline_external_id), "
            "  timeline_verified=%s, needs_recut=%s "
            "WHERE game_id=%s",
            (anchor_sec, game_duration_sec, source, external_id,
             verified, needs_recut, game_id),
        )
        conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    import argparse, sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="對單一 / 多 game 跑 timeline 對齊")
    parser.add_argument("--game-id", type=int, action="append",
                        help="可重複指定 game_id")
    parser.add_argument("--date", type=str,
                        help="對某日所有 broadcast_games 跑（YYYY-MM-DD）")
    parser.add_argument("--league", type=str,
                        help="搭配 --date 篩聯賽")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    game_ids: list[int] = list(args.game_id or [])
    if args.date:
        with mysql_conn() as conn:
            cur = conn.cursor()
            q = (
                "SELECT bg.game_id FROM broadcast_games bg "
                "JOIN broadcasts b ON b.broadcast_id = bg.broadcast_id "
                "WHERE b.broadcast_date=%s"
            )
            params = [args.date]
            if args.league:
                q += " AND b.league_code=%s"
                params.append(args.league.upper())
            cur.execute(q, params)
            for r in cur.fetchall():
                game_ids.append(r["game_id"])

    if not game_ids:
        print("無 game_id 指定（--game-id 或 --date 都沒）")
        return 1

    print(f"=== 對 {len(game_ids)} game 跑 timeline anchor 對齊 ===")
    for gid in game_ids:
        r = adjust_end_offset_for_game(gid)
        print(f"  g{gid:3} -> {r}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

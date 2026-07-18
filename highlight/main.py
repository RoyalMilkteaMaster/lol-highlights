"""
LoL 精華自動剪輯 — 主程式

執行方式：
    python main.py <VOD 路徑>                # 完整流程：分割 → 掃描 → 剪輯
    python main.py <VOD 路徑> --skip-split   # 跳過分割（VOD 已是單場 MP4）
    python main.py <VOD 路徑> --force-rescan # 強制重跑視覺偵測（忽略 cache）
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

import argparse
import yaml

# sys.path 必須含 root（lol-highlights/），讓 `from highlight.xxx` 解析得到。
_HIGHLIGHT_ROOT = Path(__file__).parent           # lol-highlights/highlight/
_PROJECT_ROOT   = _HIGHLIGHT_ROOT.parent          # lol-highlights/
sys.path.insert(0, str(_PROJECT_ROOT))
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

from highlight.pipeline.split_vod import split_vod
from highlight.pipeline.scan_video import run_yolo_scan
from highlight.utils.utils import ensure_ffmpeg_path, get_video_duration
from highlight.utils import paths
from highlight.utils.vod_metadata import (
    lookup_broadcast_for_highlight,
    lookup_game_for_highlight,
)

CONFIG_PATH = _HIGHLIGHT_ROOT / "config.yaml"

# 統一進度 log，watch_progress.py 會讀此檔。
# 開頭清空，確保每次 run 從頭開始；clip.py 會以 append 模式繼續寫入。
_PROGRESS_LOG = paths.cut_progress_log()
logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    _PROGRESS_LOG.parent.mkdir(parents=True, exist_ok=True)
    _PROGRESS_LOG.write_text("", encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    handler = logging.FileHandler(str(_PROGRESS_LOG), mode="a", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s  %(levelname)-7s  %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logging.getLogger().addHandler(handler)


# ─────────────────────────────────────────────────────────────────────────────
# 設定載入
# ─────────────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ─────────────────────────────────────────────────────────────────────────────
# 各步驟函式（一個函式只負責一件事）
# ─────────────────────────────────────────────────────────────────────────────

def step_split(vod: Path, split_dir: Path) -> list[Path]:
    """步驟 1：把多場 VOD 分割成個別場次 MP4，回傳輸出檔案列表。"""
    logger.info(f"[1/3 分割] {vod.name}")
    split_dir.mkdir(parents=True, exist_ok=True)
    game_files = split_vod(vod, split_dir)
    logger.info(f"  -> 分割完成：{len(game_files)} 場")
    return game_files


def step_scan(
    game_mp4: Path,
    scan_dir: Path,
) -> Path:
    """步驟 2：對單場 MP4 執行 YOLO 掃描，輸出 scene JSON。"""
    scan_dir.mkdir(parents=True, exist_ok=True)
    scene_path = scan_dir / f"{game_mp4.stem}_scene.json"

    logger.info(f"[2/3 掃描] {game_mp4.name}")

    duration = get_video_duration(game_mp4)
    run_yolo_scan(game_mp4, scene_path, duration)

    return scene_path


def step_cut(
    game_mp4: Path,
    scene_json: Path,
    output_path: Path,
    force_rescan: bool = False,
    kill_model: str | None = None,
    end_graph_model: str | None = None,
) -> int:
    """步驟 3：呼叫 clip.py 進行精華剪輯，輸出最終 MP4。

    Phase 49-3e：回傳 clip.py 的 returncode（透傳給 main()），讓 clip_worker 能讀到：
      0 → 成功
      2 → FATAL_NO_BP（BPNotFoundError）
      3 → FATAL_NO_END（EndNotFoundError）
      其他 → 一般失敗
    """
    logger.info(f"[3/3 剪輯] {game_mp4.name} -> {output_path.name}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 5/8：強制用 python.exe（不用 sys.executable 因為可能 = pythonw.exe）。
    # 觀察：clip_worker 用 pythonw 啟 main.py，main.py 用 sys.executable=pythonw spawn clip.py
    # → clip.py 啟動 1 秒掛 rc=120 + stderr 完全沒輸出（pythonw + 子 pythonw 的 stdio 繼承
    # 在 Windows 不穩）。改用 python.exe + CREATE_NO_WINDOW + 顯式 stdout/stderr 解決：
    #   - python.exe：console subsystem，stdio 行為可靠
    #   - CREATE_NO_WINDOW (0x08000000)：藏住 console 視窗（user 跑 pythonw 不想看視窗）
    #   - stdout/stderr 顯式傳：不靠 Windows handle inheritance（pythonw 下會壞）
    py_exe = Path(sys.executable).parent / "python.exe"
    if not py_exe.is_file():
        py_exe = sys.executable  # fallback（理論上 conda env 一定有 python.exe）
    cmd = [
        str(py_exe),
        str(_HIGHLIGHT_ROOT / "pipeline" / "clip.py"),
        "--video",  str(game_mp4),
        "--scene",  str(scene_json),
        "--output", str(output_path),
    ]
    if force_rescan:
        cmd.append("--force-rescan")
    if kill_model:
        cmd.extend(["--kill-model", kill_model])
    if end_graph_model:
        cmd.extend(["--end-graph-model", end_graph_model])

    creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    try:
        sys.stdout.flush(); sys.stderr.flush()
    except Exception:
        pass
    result = subprocess.run(cmd, stdout=sys.stdout, stderr=sys.stderr,
                            creationflags=creationflags)
    if result.returncode == 2:
        logger.error("  [X] FATAL_NO_BP（鐵則 R1：BP_UI 未偵測到，本片放棄剪輯）")
    elif result.returncode == 3:
        logger.error("  [X] FATAL_NO_END（鐵則 R7：游戲結尾三層訊號皆失，本片放棄剪輯）")
    elif result.returncode != 0:
        logger.error(f"  剪輯失敗（returncode={result.returncode}）")
    return result.returncode


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────

def filter_by_teams(game_files: list[Path], teams: list[str]) -> list[Path]:
    """篩選檔名中包含所有指定隊名的場次（大小寫不分）。"""
    teams_lower = [t.lower() for t in teams]
    matched = [f for f in game_files if all(t in f.stem.lower() for t in teams_lower)]
    return matched


def run_pipeline(
    vod: Path,
    skip_split: bool,
    force_rescan: bool,
    teams: list[str] | None = None,
    kill_model: str | None = None,
    end_graph_model: str | None = None,
) -> None:
    """主流程：依序執行分割 → YOLO 掃描 → 剪輯，支援多場批次處理。"""
    ensure_ffmpeg_path()
    load_config()

    # 固定目錄結構（讀 highlight.utils.paths，支援跨機器 env var override）
    split_dir = paths.split_dir()        # 分割後的單場 MP4
    scan_dir  = paths.scan_dir()         # 掃描產生的 JSON
    final_dir = paths.finals_dir()       # 最終精華影片輸出

    # 步驟 1：分割（超過 2 小時自動分割，否則直接處理）
    SPLIT_THRESHOLD_SEC = 2 * 3600  # 2 小時
    duration = get_video_duration(vod)

    if skip_split:
        game_files = [vod]
        logger.info(f"[跳過分割] 直接處理：{vod.name}")
    elif duration > SPLIT_THRESHOLD_SEC:
        logger.info(f"  影片長度 {duration/3600:.1f} 小時（> 2 小時），自動分割")
        game_files = step_split(vod, split_dir)
        if not game_files:
            logger.error("分割結果為空，請確認影片內容或改用 --skip-split")
            sys.exit(1)
    else:
        logger.info(f"  影片長度 {duration/60:.0f} 分鐘（≤ 2 小時），跳過分割直接處理")
        game_files = [vod]

    # 隊名篩選（分割後才能用檔名過濾）
    if teams:
        original_count = len(game_files)
        game_files = filter_by_teams(game_files, teams)
        logger.info(f"  隊名篩選 {teams}：{original_count} 場 -> {len(game_files)} 場")
        if not game_files:
            logger.error(f"找不到包含隊名 {teams} 的場次，請確認隊名縮寫是否正確")
            sys.exit(1)

    # 步驟 2 & 3：逐場掃描 + 剪輯
    # Phase 49-3e：紀錄每場 returncode；最後回傳 max(returncodes)，
    # 讓 main() 透傳給上層（clip_worker spawn main.py 時看 exit code 判定 fatal）。
    total = len(game_files)
    returncodes: list[int] = []
    for i, game_mp4 in enumerate(game_files, 1):
        logger.info(f"\n{'='*55}")
        logger.info(f"第 {i}/{total} 場：{game_mp4.name}")

        scene_path = step_scan(game_mp4, scan_dir)

        output_path = final_dir / f"{game_mp4.stem}_highlights.mp4"
        rc = step_cut(
            game_mp4, scene_path, output_path, force_rescan,
            kill_model=kill_model,
            end_graph_model=end_graph_model,
        )
        returncodes.append(rc)
        if rc == 0:
            logger.info(f"  [OK] 完成 -> {output_path}")

    logger.info(f"\n全部處理完畢，共 {total} 場（returncodes={returncodes}）")
    return max(returncodes) if returncodes else 0


# ─────────────────────────────────────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="LoL 精華自動剪輯")
    parser.add_argument(
        "vod",
        nargs="?",                                 # Phase 49-2：可選（用 --from-broadcast 時不傳）
        default=None,
        help="VOD 影片路徑（推薦已切好的 $VIDEO_DIR/split/<game>.mp4 配 --skip-split）",
    )
    parser.add_argument(
        "--from-broadcast",
        type=int,
        default=None,
        metavar="BROADCAST_ID",
        help="（Phase 49-2）從 DB 讀取 broadcast_id 對應的 recording_path 作為 VOD 路徑",
    )
    parser.add_argument(
        "--from-game",
        type=int,
        default=None,
        metavar="GAME_ID",
        help="（Phase 49-3a）從 DB 讀取 broadcast_games.game_path 作為已切好的單場 VOD"
             "（自動 --skip-split）",
    )
    parser.add_argument(
        "--skip-split",
        action="store_true",
        help="跳過分割（VOD 已是單場 MP4）",
    )
    parser.add_argument(
        "--force-rescan",
        action="store_true",
        help="強制重跑視覺偵測，忽略已有的 cache",
    )
    parser.add_argument(
        "--teams",
        type=str,
        default=None,
        help="只處理指定隊伍的場次，用逗號分隔（例如：CFO,SHF）",
    )
    parser.add_argument(
        "--kill-model",
        type=str,
        default=None,
        help="覆寫 kill_feed YOLO 模型路徑（預設讀 config.yaml）",
    )
    parser.add_argument(
        "--end-graph-model",
        type=str,
        default=None,
        help="覆寫 end_graph YOLO 模型路徑（預設讀 config.yaml）",
    )
    args = parser.parse_args()
    _setup_logging()

    # ── Phase 49-2 Step 2：解析 vod 來源 ────────────────────────────────
    sources_set = sum(x is not None for x in (args.vod, args.from_broadcast, args.from_game))
    if sources_set > 1:
        logger.error("vod / --from-broadcast / --from-game 三者只能擇一")
        sys.exit(1)

    if args.from_game is not None:
        # Phase 49-3a：from-game 表示影片已是切好的單場，自動 skip_split
        vod = _resolve_vod_from_game(args.from_game)
        args.skip_split = True
    elif args.from_broadcast is not None:
        vod = _resolve_vod_from_broadcast(args.from_broadcast)
    elif args.vod is not None:
        vod = Path(args.vod)
    else:
        logger.error("必須指定 vod 路徑 / --from-broadcast / --from-game")
        parser.print_help()
        sys.exit(1)

    if not vod.exists():
        logger.error(f"找不到影片：{vod}")
        sys.exit(1)

    rc = run_pipeline(
        vod=vod,
        skip_split=args.skip_split,
        force_rescan=args.force_rescan,
        teams=args.teams.split(",") if args.teams else None,
        kill_model=args.kill_model,
        end_graph_model=args.end_graph_model,
    )
    # Phase 49-3e：透傳 clip.py 的 exit code 給上層 clip_worker
    # 5/8：用 os._exit 而非 sys.exit。pythonw 跑時 atexit 清理會把非零 exit code 改成 120
    # → clip_worker 看不到原本的 2/3 fatal code → 誤判一般失敗。手動 flush 後 _exit 確保透傳。
    try:
        sys.stdout.flush(); sys.stderr.flush()
    except Exception:
        pass
    os._exit(rc or 0)


def _resolve_vod_from_broadcast(broadcast_id: int) -> Path:
    """從 DB 撈 broadcasts.recording_path 作為 vod 路徑（Phase 49-2 Step 2）。

    【明文規定】此函式只「讀」broadcasts；
    完全不操作 clip_jobs（clip_worker 是 clip_jobs 唯一管理者）。
    """
    row = lookup_broadcast_for_highlight(broadcast_id)

    if not row:
        logger.error(f"找不到 broadcast_id={broadcast_id}")
        sys.exit(1)
    if not row.get("recording_path"):
        logger.error(
            f"broadcast_id={broadcast_id} 沒有 recording_path "
            f"（status={row.get('recording_status_v2')}）— 還沒錄完？"
        )
        sys.exit(1)

    rp = Path(row["recording_path"])
    logger.info(
        f"[--from-broadcast] broadcast_id={broadcast_id} "
        f"league={row['league_code']} date={row['broadcast_date']} -> {rp}"
    )
    return rp


def _resolve_vod_from_game(game_id: int) -> Path:
    """從 DB 撈 broadcast_games.game_path（Phase 49-3a：單場已切好的 VOD）。"""
    row = lookup_game_for_highlight(game_id)

    if not row:
        logger.error(f"找不到 game_id={game_id}")
        sys.exit(1)
    if not row.get("game_path"):
        logger.error(
            f"game_id={game_id} 沒有 game_path（status={row.get('status')}）— 還沒切？"
        )
        sys.exit(1)

    gp = Path(row["game_path"])
    logger.info(
        f"[--from-game] game_id={game_id} g{row['game_index']} "
        f"league={row['league_code']} date={row['broadcast_date']} -> {gp}"
    )
    return gp


if __name__ == "__main__":
    main()

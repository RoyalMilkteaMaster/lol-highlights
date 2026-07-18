"""（Blocker 3）：驗證 _detect_multi_classes 時間語義。

核心問題：
  cache 設計用 incremental 累加 events，需要確認 _detect_multi_classes(start, end) 回傳的
  timestamps 是【絕對時間】還是【相對於 start 的 offset】。

  - 若絕對：cache merge 時直接 extend
  - 若相對：cache merge 時要 +start_sec offset

驗證方式：
  a = scan [0, 600]
  b = scan [0, 300]
  c = scan [300, 600]

  預期：sorted(set(b ∪ c)) == sorted(a)（絕對時間）
  失敗：c 回傳 [0, 30, 60, ...] 而非 [300, 330, ...] = 相對時間

跑法（不是 unittest，是手動實跑因為要燒 GPU 幾分鐘）：
  $env:KMP_DUPLICATE_LIB_OK="TRUE"
  & "C:\\Users\\lesli\\anaconda3\\envs\\lol-env\\python.exe" `
      automation\\tests\\test_yolo_detector_range.py
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    # 用既有的 LCK 4hr VOD（已知有多個 bp_ui events）
    test_vod = Path("E:/videos/lol_vods/LCK_Carry_1sej4xxJA64.mp4")
    if not test_vod.is_file():
        print(f"[ERROR] 找不到測試 VOD：{test_vod}")
        return 2

    # 讓 detectors/ 可被 import：把 project root 加入 sys.path
    project_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(project_root))

    from highlight import create_yolo_detector

    print("=" * 60)
    print(f"Step 0 驗證：_detect_multi_classes 時間語義")
    print(f"VOD：{test_vod.name}")
    print("=" * 60)

    det = create_yolo_detector(test_vod)

    # 掃 30 分鐘（涵蓋第一場 BP，前 10 分鐘通常是賽前介紹）
    A_END, MID, FULL = 0, 900, 1800

    print(f"\n>>> 掃 [{A_END}, {FULL}]，stride=30 ...")
    a = det._detect_multi_classes(["bp_ui"], A_END, FULL, 30)
    a_times = sorted(set(a["bp_ui"]))
    print(f"   bp_ui hits: {a_times}")

    print(f"\n>>> 掃 [{A_END}, {MID}]，stride=30 ...")
    b = det._detect_multi_classes(["bp_ui"], A_END, MID, 30)
    b_times = sorted(set(b["bp_ui"]))
    print(f"   bp_ui hits: {b_times}")

    print(f"\n>>> 掃 [{MID}, {FULL}]，stride=30 ...")
    c = det._detect_multi_classes(["bp_ui"], MID, FULL, 30)
    c_times = sorted(set(c["bp_ui"]))
    print(f"   bp_ui hits: {c_times}")

    bc_times = sorted(set(b_times) | set(c_times))

    print("\n" + "=" * 60)
    print("比對結果")
    print("=" * 60)
    print(f"a (整段 0-600)：    {a_times}")
    print(f"b ∪ c (合併):       {bc_times}")

    if a_times == bc_times:
        print("\n[OK] 完全一致 — _detect_multi_classes 回傳【絕對時間】")
        print("     cache 設計：直接 extend 不用加 offset")
        return 0

    if c_times and all(t < 300 for t in c_times):
        print("\n[FAIL] c 的 timestamps 都 < 300 — 疑似回傳【相對時間】")
        print("       cache merge 時必須手動 +start_sec offset")
        return 1

    print("\n[FAIL] 不一致但也不是純相對時間，要查清楚")
    print(f"       a 多出: {sorted(set(a_times) - set(bc_times))}")
    print(f"       bc 多出: {sorted(set(bc_times) - set(a_times))}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

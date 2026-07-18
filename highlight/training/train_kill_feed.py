"""
train_kill_feed.py — 訓練 Kill Feed (擊殺廣播) YOLO 偵測模型

資料集：training/My First Project.v1i.yolov8（v5；1855 train + 134 valid）
  v4 → v5 改進：
    - 移除 v4 的「同擊殺畫面標兩次」造成的虛高重複資料
    - 補充更多新樣本（總量 467 v2 → 1098 v4 → 1855 v5）
類別：kill_feed, tower（共 2 類）
輸出：assets/yolo_models/kill_feed_yolo11s.pt

執行方式：
  conda activate lol-env
  python training/train_kill_feed.py

Phase 45+：YOLOv8s → YOLOv11s
  - v2 dataset 467 張時 v11s overfit / class 搞反，所以退回 v8s
  - v4 dataset 1098 train + 131 valid，資料量足夠 v11s（C2PSA attention 需要更多樣本）
  - v8n (ultralytics 預設 production) → 80% 過度偵測 → 改用更大的 v11s
  - 資料集 .txt 格式 YOLOv8/v11 完全相容，base model 換成 yolo11s.pt 即可

加強訓練參數：
  - imgsz=640
  - epochs=300（上限，配 patience 自動停）
  - patience=80
  - batch=16
  - dropout=0.1（regularization）
  - lr0=0.005（預設 0.01 降半）
"""

import shutil
from pathlib import Path
from ultralytics import YOLO

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
DATA_YAML    = PROJECT_ROOT / "training" / "My First Project.v1i.yolov8" / "data.yaml"
OUTPUT_DIR   = PROJECT_ROOT / "runs"
DEST_PT      = PROJECT_ROOT / "assets" / "yolo_models" / "kill_feed_yolo11s.pt"


def fix_data_yaml_paths():
    """
    Roboflow 匯出的 data.yaml 用相對路徑（../train/images），ultralytics 解析時
    會以 datasets root 為基準，容易找不到。改寫成絕對路徑確保萬無一失。
    """
    import yaml
    with open(DATA_YAML, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    base = DATA_YAML.parent
    cfg["train"] = str((base / "train" / "images").resolve())
    cfg["val"]   = str((base / "valid" / "images").resolve())
    cfg["test"]  = str((base / "test"  / "images").resolve())

    with open(DATA_YAML, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    print(f"[yaml] 路徑已改成絕對路徑：\n  train={cfg['train']}\n  val={cfg['val']}")


def train():
    fix_data_yaml_paths()

    # Phase 45+：改用 YOLOv11s（v4 dataset 1098 張足夠喂飽 v11s attention head）
    base_pt = PROJECT_ROOT / "assets" / "yolo_models" / "yolo11s.pt"
    if not base_pt.exists():
        print(f"[train] 找不到 {base_pt}，改用線上 yolo11s.pt（自動下載 ~19MB）")
        base_pt = "yolo11s.pt"

    model = YOLO(str(base_pt))

    results = model.train(
        data=str(DATA_YAML),
        epochs=300,          # 上限，配大 patience 自動停
        imgsz=640,
        batch=16,
        patience=80,
        workers=4,
        device=0,
        project=str(OUTPUT_DIR),
        name="kill_feed_yolo11s",
        exist_ok=True,
        # 防 overfit 的 regularization（Phase 41-v3 新增）
        dropout=0.1,
        lr0=0.005,
        # 資料增強：UI 元素位置固定，禁用幾何翻轉
        degrees=0,
        fliplr=0,
        flipud=0,
        scale=0.2,
        translate=0.1,
        mosaic=0.3,
        # 顏色增強（特效干擾期顏色變化大）
        hsv_h=0.015,
        hsv_s=0.5,
        hsv_v=0.3,
    )

    # 同時複製 best.pt 與 last.pt → models/（Phase 45+：production 對比兩者誤判率）
    DEST_PT.parent.mkdir(parents=True, exist_ok=True)
    weights_dir = Path(results.save_dir) / "weights"
    best_src = weights_dir / "best.pt"
    last_src = weights_dir / "last.pt"
    last_dst = DEST_PT.with_name(DEST_PT.stem + "_last.pt")   # e.g. kill_feed_yolo11s_last.pt
    shutil.copy(best_src, DEST_PT)
    if last_src.exists():
        shutil.copy(last_src, last_dst)
    print(f"\n[train] 訓練完成！")
    print(f"  best.pt → {DEST_PT}")
    print(f"  last.pt → {last_dst}")

    # Phase 41-v3：per-class 驗證，確保 kill_feed 和 tower 都有學好（避免上次 class 搞反）
    print("\n[train] 最終 per-class 驗證...")
    val_results = model.val(data=str(DATA_YAML))
    try:
        names = val_results.names if hasattr(val_results, "names") else {0: "kill_feed", 1: "tower"}
        maps = val_results.box.maps   # per-class mAP50-95
        print(f"\n  {'Class':<12} {'mAP50-95':>10}")
        print("  " + "-" * 25)
        items = names.items() if isinstance(names, dict) else enumerate(names)
        for idx, name in items:
            print(f"  {name:<12} {float(maps[idx]):>10.4f}")
    except Exception as e:
        print(f"  per-class 驗證印出失敗: {e}")


if __name__ == "__main__":
    train()

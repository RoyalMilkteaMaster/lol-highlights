"""
train_lol_v5.py — 用 v5 yolov11 資料集訓練 lol_detector v11s，並替換舊模型

Phase 41：YOLOv8s → YOLOv11s + 新 dataset（4062 張含 3x augment）
  - 舊 (5) yolov8: 1669 張無 augment，baron/game_end 等極少
  - 新 v5 yolov11: 4062 張，所有 class 標註數量充足（baron 291, bp_ui 700, etc.）

資料集：training/LoL-Auto-Clipper.v5i.yolov11/
  - 11 類別（跟舊 v5 同 class 定義）
  - train/valid/test 都有資料（之前只有 train）

執行方式：
  conda activate lol-env
  python training/train_lol_v5.py

訓練完成後：
  - 舊模型備份：assets/yolo_models/lol_detector.pt → lol_detector.pt.bak.YYYYmmdd_HHMMSS
  - 新模型替換：runs/lol_detector_v11s/weights/best.pt → assets/yolo_models/lol_detector.pt
"""

import shutil
from datetime import datetime
from pathlib import Path

import yaml
from ultralytics import YOLO

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
PROJECT_ROOT  = Path(__file__).parent.parent
DATASET_DIR   = PROJECT_ROOT / "training" / "LoL-Auto-Clipper.v5i.yolov11"
DATA_YAML     = DATASET_DIR / "data.yaml"
BASE_PT       = PROJECT_ROOT / "assets" / "yolo_models" / "yolo11s.pt"
OUTPUT_DIR    = PROJECT_ROOT / "runs"
RUN_NAME      = "lol_detector_v11s"
DEST_PT       = PROJECT_ROOT / "assets" / "yolo_models" / "lol_detector.pt"


def fix_data_yaml_paths():
    """改寫 data.yaml 為絕對路徑；valid/test 不存在時，用 train 當 val。"""
    with open(DATA_YAML, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    base = DATASET_DIR
    train_imgs = (base / "train" / "images").resolve()
    valid_imgs = (base / "valid" / "images").resolve()
    test_imgs  = (base / "test"  / "images").resolve()

    cfg["train"] = str(train_imgs)
    cfg["val"]   = str(valid_imgs if valid_imgs.exists() else train_imgs)
    if test_imgs.exists():
        cfg["test"] = str(test_imgs)
    elif "test" in cfg:
        cfg.pop("test")

    with open(DATA_YAML, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    print(f"[yaml] train={cfg['train']}")
    print(f"[yaml] val  ={cfg['val']}")
    print(f"[yaml] nc={cfg['nc']}, names={cfg['names']}")


def backup_old_model():
    """把現有 lol_detector.pt 備份成 .bak.YYYYmmdd_HHMMSS（保留歷史）。"""
    if not DEST_PT.exists():
        print("[backup] 無舊模型可備份")
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = DEST_PT.with_suffix(f".pt.bak.{ts}")
    shutil.copy2(DEST_PT, bak)
    print(f"[backup] 舊模型已備份：{bak.name}")


def train():
    fix_data_yaml_paths()
    backup_old_model()

    base_pt = BASE_PT if BASE_PT.exists() else "yolo11s.pt"
    print(f"[train] 起始權重：{base_pt}")
    model = YOLO(str(base_pt))

    results = model.train(
        data=str(DATA_YAML),
        epochs=150,          # Phase 41：100 → 150（v11s + 更多資料要更多時間收斂）
        imgsz=1280,
        batch=8,
        patience=60,         # Phase 41：20 → 60（避免 early stop 太早）
        workers=4,
        device=0,
        project=str(OUTPUT_DIR),
        name=RUN_NAME,
        exist_ok=True,
        # Phase 41 regularization
        dropout=0.1,
        lr0=0.005,
        # UI 元素位置固定 → 關閉幾何翻轉
        degrees=0,
        fliplr=0,
        flipud=0,
        scale=0.3,
        translate=0.1,
        mosaic=0.5,
    )

    best = Path(results.save_dir) / "weights" / "best.pt"
    DEST_PT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(best, DEST_PT)
    print(f"\n[train] 訓練完成！")
    print(f"[train] best.pt: {best}")
    print(f"[train] 已替換： {DEST_PT}")

    # Phase 41：per-class 驗證，確認各類別都有學好
    print("\n[train] per-class 驗證...")
    val_results = model.val(data=str(DATA_YAML))
    try:
        names = val_results.names if hasattr(val_results, "names") else {}
        maps = val_results.box.maps
        print(f"\n  {'Class':<25} {'mAP50-95':>10}")
        print("  " + "-" * 40)
        items = names.items() if isinstance(names, dict) else enumerate(names)
        for idx, name in items:
            print(f"  {name:<25} {float(maps[idx]):>10.4f}")
    except Exception as e:
        print(f"  per-class 驗證失敗: {e}")


if __name__ == "__main__":
    train()

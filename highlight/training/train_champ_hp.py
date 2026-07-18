"""
train_champ_hp.py — 訓練 champion HP bar 偵測模型

資料集：LoL-HP-Merge.yolov8（374 張，藍/紅方英雄血條 + 忽略血條）
輸出：assets/yolo_models/champ_hp_detector.pt

執行方式：
  python training/train_champ_hp.py
"""

import shutil
import random
from pathlib import Path
from ultralytics import YOLO

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
SRC_DIR      = PROJECT_ROOT / "LoL-HP-Merge.yolov8" / "train"
DATA_DIR     = PROJECT_ROOT / "training" / "champ_hp_data"
YAML_PATH    = PROJECT_ROOT / "training" / "champ_hp.yaml"
OUTPUT_DIR   = PROJECT_ROOT / "runs"
DEST_PT      = PROJECT_ROOT / "assets" / "yolo_models" / "champ_hp_detector.pt"

VAL_RATIO = 0.2   # 20% 作驗證集


def split_dataset():
    """將 LoL-HP-Merge.yolov8/train/ 做 80/20 split，複製到 training/champ_hp_data/。"""
    images_src = SRC_DIR / "images"
    labels_src = SRC_DIR / "labels"

    for split in ["train", "val"]:
        (DATA_DIR / "images" / split).mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "labels" / split).mkdir(parents=True, exist_ok=True)

    # 已有資料就跳過（idempotent）
    if list((DATA_DIR / "images" / "train").glob("*.jpg")):
        print(f"[split] 已存在資料，跳過 split（train: "
              f"{len(list((DATA_DIR / 'images' / 'train').glob('*.jpg')))} 張）")
        return

    all_images = sorted(images_src.glob("*.jpg")) + sorted(images_src.glob("*.png"))
    random.seed(42)
    random.shuffle(all_images)

    n_val   = max(1, int(len(all_images) * VAL_RATIO))
    val_set = set(img.name for img in all_images[:n_val])

    for img_path in all_images:
        split = "val" if img_path.name in val_set else "train"
        shutil.copy(img_path, DATA_DIR / "images" / split / img_path.name)

        lbl_path = labels_src / img_path.with_suffix(".txt").name
        if lbl_path.exists():
            shutil.copy(lbl_path, DATA_DIR / "labels" / split / lbl_path.name)

    n_train = len(all_images) - n_val
    print(f"[split] 完成：train={n_train} 張，val={n_val} 張")


def train():
    split_dataset()

    # 從 YOLOv8s 開始 fine-tune（不從 lol_detector 開始，避免類別衝突）
    base_pt = PROJECT_ROOT / "assets" / "yolo_models" / "yolov8s.pt"
    if not base_pt.exists():
        print(f"[train] 找不到 {base_pt}，改用 yolov8s（自動下載）")
        base_pt = "yolov8s.pt"

    model = YOLO(str(base_pt))

    results = model.train(
        data=str(YAML_PATH),
        epochs=100,
        imgsz=1280,          # LOL 1080p 畫面，解析度要夠高才能抓到血條
        batch=8,             # 10GB VRAM 已驗證 batch=8
        patience=20,         # 20 epoch 無改善提早停止
        workers=4,
        device=0,            # GPU 0
        project=str(OUTPUT_DIR),
        name="champ_hp_detector",
        exist_ok=True,
        # 資料增強：HUD 位置固定，不翻轉（左藍右紅）
        degrees=0,
        fliplr=0,
        flipud=0,
        scale=0.3,
        translate=0.1,
        mosaic=0.5,
    )

    # 複製 best.pt → assets/yolo_models/
    best = Path(results.save_dir) / "weights" / "best.pt"
    DEST_PT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(best, DEST_PT)
    print(f"\n[train] 訓練完成！模型已複製到：{DEST_PT}")


if __name__ == "__main__":
    train()

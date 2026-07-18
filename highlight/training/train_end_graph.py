"""
train_end_graph.py — 訓練 End Graph (賽後總表) YOLO 偵測模型

資料集：training/end_graph.v1i.yolov8（Roboflow 匯出，全螢幕無 Crop）
類別：end_graph（共 1 類）
輸出：assets/yolo_models/end_graph_yolov8n.pt

執行方式：
  conda activate lol-env
  python training/train_end_graph.py

備註：
  - 採 YOLOv8n（nano），imgsz=640，符合 Phase 34 規格
  - End Graph 是全螢幕大物件，**不要 Static Crop**
  - device=0 強制使用 GPU 0
  - 直接用 .pt 推論；若要更快可用 ultralytics export 為 .onnx
  - 訓練資料涵蓋 LCK + LPL + LCP 三賽區（單一 class 策略）
"""

import shutil
from pathlib import Path
from ultralytics import YOLO

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent.parent
DATA_YAML    = PROJECT_ROOT / "training" / "end_graph.v1i.yolov8" / "data.yaml"
OUTPUT_DIR   = PROJECT_ROOT / "runs"
DEST_PT      = PROJECT_ROOT / "assets" / "yolo_models" / "end_graph_yolov8n.pt"


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

    # 從 YOLOv8n（nano，最輕量最快）開始 fine-tune
    base_pt = PROJECT_ROOT / "assets" / "yolo_models" / "yolov8n.pt"
    if not base_pt.exists():
        print(f"[train] 找不到 {base_pt}，改用線上 yolov8n.pt（自動下載）")
        base_pt = "yolov8n.pt"

    model = YOLO(str(base_pt))

    results = model.train(
        data=str(DATA_YAML),
        epochs=100,
        imgsz=640,           # Phase 34 規格：全螢幕推論 imgsz=640
        batch=32,            # 10GB VRAM 已驗證 batch=32
        patience=20,         # 20 epoch 無改善提早停止
        workers=4,
        device=0,            # 強制 GPU 0
        project=str(OUTPUT_DIR),
        name="end_graph_yolov8n",
        exist_ok=True,
        # 資料增強：end_graph 位置與排版固定，禁用所有幾何變形
        degrees=0,
        fliplr=0,
        flipud=0,
        scale=0.0,           # 不縮放（end_graph 大小恆定）
        translate=0.0,       # 不位移（位置恆定）
        mosaic=0.0,          # 不 mosaic（會破壞「整片大圖表」的視覺特徵）
        # 顏色增強只開亮度與輕微 HSV（賽區顏色細微差異）
        hsv_h=0.01,
        hsv_s=0.3,
        hsv_v=0.3,
    )

    # 複製 best.pt → models/
    best = Path(results.save_dir) / "weights" / "best.pt"
    DEST_PT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(best, DEST_PT)
    print(f"\n[train] 訓練完成！模型已複製到：{DEST_PT}")


if __name__ == "__main__":
    train()

# YOLO model files

The four model weights are required at runtime but intentionally excluded from Git history.
Publish them as release assets only after confirming redistribution rights, then place them in this directory with these exact names.

| File | Bytes | SHA-256 |
|---|---:|---|
| `champ_hp_detector.pt` | 89,577,123 | `3e831da92204de888d67a821e8a70ffeef91d4f2a9a0c8a7019f5fcae81e9f09` |
| `end_graph_yolov8n.pt` | 6,228,906 | `4bf1d8a31caab94d4d795466fd694ab511c73d6d90fab588c04371efc07e8733` |
| `kill_feed_yolo11s.pt` | 19,163,674 | `2a89dbc5da9aa031a3d9b871fc7f69023594be2dc1f126589b26df1d83b9361a` |
| `lol_detector.pt` | 19,251,162 | `564c52b9370c7c1b16f40764a1b6542955435fe8e81129b00bcf8fd67a9df11e` |

Verify a downloaded file in PowerShell:

```powershell
Get-FileHash .\highlight\assets\yolo_models\lol_detector.pt -Algorithm SHA256
```

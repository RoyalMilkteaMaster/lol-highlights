# YOLO model files

四個模型權重在執行時必要，但刻意不進 Git history；發布在 GitHub Release（tag `models-v1`）。
`setup.ps1` 會自動下載到這個資料夾並用 [`SHA256SUMS`](SHA256SUMS) 校驗。

手動下載後驗證：

```powershell
Get-FileHash .\highlightssets\yolo_models\*.pt -Algorithm SHA256
```

重新訓練模型後：更新 `SHA256SUMS`（`sha256sum *.pt > SHA256SUMS`）、上傳新 Release、把 `setup.ps1` 的 `-ModelsReleaseTag` 預設值改成新 tag。

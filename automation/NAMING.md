# 影片命名規則

## 各目錄的命名

| 目錄 | 來源 | 命名規則 | 範例 |
|---|---|---|---|
| `E:\videos\live_recordings\` | 自動錄影（streamlink） | 強制 `<LEAGUE>_<YYYYMMDD>_<PLATFORM>[_<shortid>].mp4` | `LCK_20260518_youtube_dQw4w9.mp4` |
| `E:\videos\lol_games_vods\` | 邊錄邊切 / Bilibili VOD 下載 | `<LEAGUE>_<YYYYMMDD>_<TA>vs<TB>/g<N>.mp4` | `LCK_20260518_T1vsGEN/g1.mp4` |
| `E:\videos\split\` | split_vod 切出單場 | 強制 `<LEAGUE>_<YYYYMMDD>_g<n>_<A>vs<B>.mp4` | `LCK_20260518_g1_T1vsGEN.mp4` |
| `E:\videos\scan\` | YOLO 掃描結果 | 沿用 split + `_scene.json` | `LCK_20260518_g1_T1vsGEN_scene.json` |
| `E:\videos\finals\` 或 `F:\lol-highlights\output\final\` | 剪輯輸出 | 沿用 split + `_highlights.mp4` | `LCK_20260518_g1_T1vsGEN_highlights.mp4` |

## `live_recordings/` short_id 規則

- `LCK_20260518_youtube_dQw4w9.mp4` — short_id = video_id 前 6 字元
- `LPL_20260518_bilibili_6.mp4` — short_id = room_id
- 同一天若同平台多直播（中斷重開等）→ short_id 自動加後綴 `_2`、`_3`

## `broadcast_date` 用 league timezone 推

- LCK 用 `Asia/Seoul`、LPL 用 `Asia/Shanghai`、LCP 用 `Asia/Taipei`
- DB 內部時間一律存 UTC（`scheduled_start_utc` / `actual_start_utc` / `actual_end_utc`）
- 命名 / 查詢用 league local date（`broadcast_date`）

## 縮寫規範

| 項目 | 規則 |
|---|---|
| LEAGUE | 3 字大寫（LCK / LPL / LCP / LEC / LCS / WCS / MSI） |
| PLATFORM | 小寫（`youtube` / `bilibili`） |
| YYYYMMDD | league local date |
| TEAM | ≤4 字大寫，跟 DB `teams.code` 對齊 |
| 連接符 | `vs`（不加底線） |

## `vod_metadata` 解析鏈

[`highlight/utils/vod_metadata.py`](../highlight/utils/vod_metadata.py) 的 `extract_metadata(vod_path)` 鏈式判斷：

```
1. 解析輸入檔名（regex 試 LEAGUE_DATE 嚴格模式 / LEAGUE 寬鬆模式）
   ↓ 解析得出 league + date → 用標準格式
   ↓ 解析不出
2. 看同目錄有沒有 <stem>.info.json（yt-dlp sidecar）
   ↓ 有 → 從 metadata 取 upload_date
   ↓ 沒有
3. 用檔案 mtime 當日期（fallback）
   ↓
4. 用 DB teams.code 白名單從檔名 token 抓隊伍縮寫
   ↓
5. 加入 caller 提供的 bp_teams_hint（從 BP 偵測）
   ↓
6. DB 反查 series_id（lazy import 防循環依賴）
   ↓
7. 評定 confidence：
   - high   : league + date + 2 隊 + series_id 全到
   - medium : league + date + ≥1 隊
   - low    : 只有 date
   - fallback: 全空
```

## 同日加賽防呆（未來建議）

極端情境：「Tiebreaker」或「上下半區同日交手」時，A vs B 同日打兩次，g1 檔名會撞。當前實作不處理，未來建議擴充：

```
<LEAGUE>_<YYYYMMDD>_s<series_id>_g<n>_<A>vs<B>.mp4
```

例如 `LCK_20260518_s20260518012001_g1_T1vsGEN.mp4`，加上 series_id 確保物理檔案絕對唯一。

## 架構邊界

[`highlight/utils/vod_metadata.py`](../highlight/utils/vod_metadata.py) 允許 `import automation.db`（單向例外，給 `main.py --from-game` 用）。`detectors/` / `selection/` / `rendering/` / `dashboard/` / `training/` 永遠不准 import `automation`。

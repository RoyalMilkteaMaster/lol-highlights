"""加 'post_recording' 狀態到 broadcasts.recording_status_v2 ENUM。

背景：
LCK broadcast 42（T1/DNS + KRX/GEN）watchdog 5/5 重啟失敗 mark 'failed'
→ live_split 從 active list 排除 → cumulative.mp4 剩 41 min 沒掃 → KRX/GEN g2 丟失。

設計：
- 'recording'      錄影進行中
- 'post_recording' 錄影結束（自然 / watchdog give-up），但有 .ts → live_split 必須繼續掃完
- 'recorded'       live_split 掃完 cumulative + 無新 .ts → 真正結束
- 'failed'         **完全沒產生有效 .ts** 的純災難

streamlink_recorder 決策：
  raw_segments_dir 0 個 .ts → 'failed'
  ≥ 1 個 .ts            → 'post_recording'

live_split_worker 決策：
  status='post_recording' AND last_scan_until_sec >= cumulative_duration
  AND no new .ts in N min → 自動 mark 'recorded'
"""

from __future__ import annotations


def run(conn) -> None:
    """ALTER ENUM 加 'post_recording' 值。冪等：已存在不重複加。"""
    with conn.cursor() as cur:
        # 檢查當前 column 定義
        cur.execute(
            "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() "
            "  AND TABLE_NAME = 'broadcasts' "
            "  AND COLUMN_NAME = 'recording_status_v2'"
        )
        row = cur.fetchone()
        if not row:
            print("[migration 008] broadcasts.recording_status_v2 column 不存在，跳過")
            return
        col_type = row.get("COLUMN_TYPE") if isinstance(row, dict) else row[0]
        if "post_recording" in col_type:
            print("[migration 008] 'post_recording' 已存在，跳過")
            return

        cur.execute(
            "ALTER TABLE broadcasts MODIFY COLUMN recording_status_v2 "
            "ENUM('scheduled','waiting_stream','recording','merging',"
            "'recorded','failed','post_recording') NULL"
        )
    conn.commit()
    print("[migration 008] 已加 'post_recording' 到 recording_status_v2 ENUM")

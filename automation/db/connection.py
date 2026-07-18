"""MySQL 連線管理（PyMySQL + context manager）。

設計原則：
- 從 `.env` 讀帳密（不寫死在程式裡）
- autocommit=False，由 Repository 層決定何時 commit
- 統一以絕對路徑載入 .env，避免依賴 CWD
- 提供 with-statement 介面，自動處理連線釋放與 rollback
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pymysql
from dotenv import load_dotenv
from pymysql.connections import Connection

# ── 路徑：讀 root 專案 /.env ──────────────────────────────────────────────
# parents[2] = automation/db/connection.py 往上 3 層 = root 專案目錄
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ENV_PATH = _PROJECT_ROOT / ".env"

# 載入 .env（若不存在會靜默跳過，由下方檢查報錯）
load_dotenv(_ENV_PATH)

logger = logging.getLogger(__name__)


def get_db_config(include_database: bool = True) -> dict:
    """從環境變數組裝 PyMySQL 連線參數。

    Args:
        include_database: True 時連到指定 DB；False 時不指定（用於 CREATE DATABASE()）。

    缺少必要欄位時直接拋錯，避免後續隱性失敗。
    """
    required = ["MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DB"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            f"缺少必要環境變數：{missing}；"
            f"請編輯專案 root 的 .env，加入 MYSQL_* 設定"
        )

    config = {
        "host":       os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port":       int(os.getenv("MYSQL_PORT", "3306")),
        "user":       os.getenv("MYSQL_USER"),
        "password":   os.getenv("MYSQL_PASSWORD"),
        "charset":    "utf8mb4",
        "autocommit": False,
        # DictCursor：fetch 結果是 dict，不是 tuple，方便 repo 取值
        "cursorclass": pymysql.cursors.DictCursor,
        # 強制 UTC session timezone — 確保 UTC_TIMESTAMP() 跟 naive datetime
        # 互相對應且 Python 端 datetime.utcnow() 比對不會有 timezone drift
        "init_command": "SET time_zone='+00:00'",
    }
    if include_database:
        config["database"] = os.getenv("MYSQL_DB")
    return config


@contextmanager
def mysql_conn() -> Iterator[Connection]:
    """連線 context manager；異常時自動 rollback，正常結束自動 close。

    用法：
        with mysql_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
            conn.commit()
    """
    conn: Connection | None = None
    try:
        conn = pymysql.connect(**get_db_config())
        yield conn
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()

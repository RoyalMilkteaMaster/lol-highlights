"""一次性建表 + Migration 腳本。

兩種模式：

1. **--init-db**：CREATE DATABASE IF NOT EXISTS + 跑 schema.sql
2. **--migrate**：跑 db/migrations/*.sql 中尚未執行過的檔案

執行：
    python -m automation.db.init_db                # 建表（初次）
    python -m automation.db.init_db --migrate      # 跑未執行的 migration

或從 run.py：
    python -m automation.run --init-db
    python -m automation.run --migrate

設計重點：
- migration 跑前先檢查 leagues/series/teams 表存在（safety check）
- 每個 migration 包 transaction，失敗 rollback
- 用 SHA-256 checksum 記錄已跑過的版本，重複跑同名檔不會誤判
- migrations 表第一次執行 --migrate 時會自動建立（idempotent）
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
from pathlib import Path

import pymysql

from automation.db.connection import get_db_config, mysql_conn

logger = logging.getLogger(__name__)

_SCHEMA_PATH      = Path(__file__).resolve().parent / "schema.sql"
_MIGRATIONS_DIR   = Path(__file__).resolve().parent / "migrations"
_REQUIRED_TABLES  = ("leagues", "teams", "series")


def _split_statements(sql_text: str) -> list[str]:
    """將 .sql 拆成多個 statement。

    PyMySQL execute 預設一次只能跑一個 statement，
    必須用 `;` 切分；同時略過 SQL 註解與空行。
    """
    cleaned: list[str] = []
    for raw in sql_text.split(";"):
        # 移除註解行（-- 開頭）與空白
        lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("--")]
        stmt = "\n".join(lines).strip()
        if stmt:
            cleaned.append(stmt)
    return cleaned


def _ensure_database_exists() -> None:
    """連線時不指定 DB，執行 CREATE DATABASE IF NOT EXISTS。"""
    db_name = os.getenv("MYSQL_DB")
    if not db_name:
        raise RuntimeError("環境變數 MYSQL_DB 未設定")
    if not re.fullmatch(r"[A-Za-z0-9_]+", db_name):
        raise ValueError("MYSQL_DB 只能包含英文字母、數字與底線")

    conn = pymysql.connect(**get_db_config(include_database=False))
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
        logger.info("[OK] DATABASE() `%s` 已就緒", db_name)
    finally:
        conn.close()


def init_database() -> None:
    """建立 DB（如不存在）+ 所有表（idempotent — 用 CREATE TABLE IF NOT EXISTS）。"""
    if not _SCHEMA_PATH.is_file():
        raise FileNotFoundError(f"找不到 schema.sql：{_SCHEMA_PATH}")

    _ensure_database_exists()

    sql_text = _SCHEMA_PATH.read_text(encoding="utf-8")
    statements = _split_statements(sql_text)
    logger.info("讀取 schema.sql：%d 個 statement", len(statements))

    with mysql_conn() as conn:
        with conn.cursor() as cur:
            for idx, stmt in enumerate(statements, start=1):
                logger.info("執行 statement %d/%d", idx, len(statements))
                cur.execute(stmt)
        conn.commit()

    logger.info("[OK] 建表完成")


# ============================================================================
#  Migration
# ============================================================================

def _check_required_tables_exist(cursor) -> None:
    """跑 migration 前確認必要表已存在（避免在空 DB 上亂跑 migration）。"""
    cursor.execute(
        "SELECT TABLE_NAME FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE()"
    )
    existing = {row["TABLE_NAME"].lower() for row in cursor.fetchall()}
    missing = [t for t in _REQUIRED_TABLES if t not in existing]
    if missing:
        raise RuntimeError(
            f"跑 --migrate 前必須先 --init-db 建好基底表。缺少：{missing}"
        )


def _ensure_migrations_table(cursor) -> None:
    """建立 migrations 紀錄表（如不存在）。"""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS migrations (
            migration_name VARCHAR(255) PRIMARY KEY,
            applied_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP(),
            checksum       VARCHAR(64)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def _get_applied_migrations(cursor) -> dict[str, str]:
    """讀已跑過的 migration → {name: checksum}。"""
    cursor.execute("SELECT migration_name, checksum FROM migrations")
    return {row["migration_name"]: row["checksum"] for row in cursor.fetchall()}


def _calculate_checksum(content: str) -> str:
    """SHA-256 hex digest（記錄 migration 內容指紋）。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def run_migrations() -> None:
    """跑 db/migrations/ 中尚未執行過的 *.sql 與 *.py。

    .sql：split 成多個 statement 依序執行
    .py ：import 後呼叫 run(conn) — 支援 information_schema 條件式 ALTER
    依檔名字典序執行（001_xxx → 002_xxx）。
    """
    if not _MIGRATIONS_DIR.is_dir():
        logger.warning("找不到 migrations 目錄：%s", _MIGRATIONS_DIR)
        return

    # 同時掃 .sql + .py，依檔名排序
    files = sorted(
        list(_MIGRATIONS_DIR.glob("*.sql")) + list(_MIGRATIONS_DIR.glob("*.py")),
        key=lambda p: p.name,
    )
    # 排除 __init__.py
    files = [f for f in files if f.name != "__init__.py"]
    if not files:
        logger.info("migrations 目錄為空，無事可做")
        return

    logger.info("發現 %d 個 migration 檔", len(files))

    with mysql_conn() as conn:
        with conn.cursor() as cur:
            _check_required_tables_exist(cur)
            _ensure_migrations_table(cur)
            conn.commit()
            applied = _get_applied_migrations(cur)

        for path in files:
            name = path.name
            content = path.read_text(encoding="utf-8")
            checksum = _calculate_checksum(content)

            if name in applied:
                if applied[name] == checksum:
                    logger.info("[OK] %s 已執行（checksum 一致）", name)
                else:
                    logger.warning(
                        "[WARN] %s 已執行但 checksum 不一致（DB=%s, file=%s）；不重跑",
                        name, applied[name][:8], checksum[:8],
                    )
                continue

            logger.info("→ 執行 %s ...", name)
            try:
                if path.suffix == ".sql":
                    _run_sql_migration(conn, content)
                elif path.suffix == ".py":
                    _run_py_migration(conn, path)
                else:
                    raise ValueError(f"不支援的 migration 副檔名：{path}")

                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO migrations (migration_name, checksum) "
                        "VALUES (%s, %s)",
                        (name, checksum),
                    )
                conn.commit()
                logger.info("  [OK] %s 執行成功", name)
            except Exception:
                conn.rollback()
                logger.exception("  [X] %s 執行失敗，已 rollback", name)
                raise

    logger.info("[OK] 所有 migration 完成")


def _run_sql_migration(conn, content: str) -> None:
    """執行 .sql migration（既有邏輯）。"""
    statements = _split_statements(content)
    with conn.cursor() as cur:
        for stmt in statements:
            cur.execute(stmt)


def _run_py_migration(conn, path) -> None:
    """執行 .py migration：dynamic import 後呼叫 run(conn)。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"_migration_{path.stem}", str(path),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "run"):
        raise RuntimeError(f"{path.name} 缺少 run(conn) 函式")
    module.run(conn)


# ============================================================================
#  CLI 進入點
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="DB 初始化 / Migration 工具")
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="跑 db/migrations/*.sql 中尚未執行過的 migration（不動既有資料）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.migrate:
        run_migrations()
    else:
        init_database()
if __name__ == "__main__":
    main()

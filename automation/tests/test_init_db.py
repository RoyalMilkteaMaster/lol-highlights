"""Database initialization safety tests."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from automation.db import init_db


class DatabaseCreationTests(unittest.TestCase):
    def test_create_database_uses_valid_mysql_syntax(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value

        with (
            patch.object(init_db.os, "getenv", return_value="lol_highlight"),
            patch.object(
                init_db,
                "get_db_config",
                return_value={"host": "localhost"},
            ) as get_db_config,
            patch.object(init_db.pymysql, "connect", return_value=connection),
        ):
            init_db._ensure_database_exists()

        get_db_config.assert_called_once_with(include_database=False)
        cursor.execute.assert_called_once_with(
            "CREATE DATABASE IF NOT EXISTS `lol_highlight` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        connection.commit.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_database_name_rejects_sql_metacharacters(self):
        with patch.object(init_db.os, "getenv", return_value="bad`name"):
            with self.assertRaisesRegex(ValueError, "MYSQL_DB"):
                init_db._ensure_database_exists()


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Shared automation configuration tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from automation.infra.config import load_config
from automation.pipeline import ScraperPipeline


class ConfigTests(unittest.TestCase):
    def test_missing_and_empty_files_return_empty_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            self.assertEqual({}, load_config(config_path))
            config_path.touch()
            self.assertEqual({}, load_config(config_path))

    def test_loads_yaml_mapping(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text(
                "scheduler:\n  poll_interval_sec: 30\n",
                encoding="utf-8",
            )
            self.assertEqual(
                {"scheduler": {"poll_interval_sec": 30}},
                load_config(config_path),
            )

    def test_rejects_non_mapping_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            config_path.write_text("- scheduler\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "config root must be a mapping"):
                load_config(config_path)

    def test_pipeline_respects_explicit_empty_config(self):
        with patch("automation.pipeline._load_config") as config_loader:
            pipeline = ScraperPipeline(config={})

        config_loader.assert_not_called()
        self.assertEqual({}, pipeline._config)


if __name__ == "__main__":
    unittest.main(verbosity=2)

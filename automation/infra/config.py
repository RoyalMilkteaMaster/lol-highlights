"""Shared automation configuration loader."""

from __future__ import annotations

from pathlib import Path

import yaml


CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


def load_config(path: Path | None = None) -> dict:
    config_path = path or CONFIG_PATH
    if not config_path.is_file():
        return {}
    with config_path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}
    if not isinstance(config, dict):
        raise ValueError(f"config root must be a mapping: {config_path}")
    return config

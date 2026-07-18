"""Import guard for the three subsystem boundaries."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_highlight_only_uses_the_allowed_db_adapter(self):
        violations: list[str] = []
        highlight_root = PROJECT_ROOT / "highlight"
        allowed_adapter = Path("utils/vod_metadata.py")

        for path in highlight_root.rglob("*.py"):
            relative = path.relative_to(highlight_root)
            for module in _imports(path):
                if module == "dashboard" or module.startswith("dashboard."):
                    violations.append(f"{relative}: {module}")
                if (
                    module == "automation" or module.startswith("automation.")
                ) and relative != allowed_adapter:
                    violations.append(f"{relative}: {module}")

        self.assertEqual([], violations)

    def test_automation_uses_highlight_public_boundaries_only(self):
        violations: list[str] = []
        automation_root = PROJECT_ROOT / "automation"

        for path in automation_root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            relative = path.relative_to(automation_root)
            for module in _imports(path):
                if module == "dashboard" or module.startswith("dashboard."):
                    violations.append(f"{relative}: {module}")
                if module.startswith("highlight.") and not (
                    module == "highlight.utils" or module.startswith("highlight.utils.")
                ):
                    violations.append(f"{relative}: {module}")

        self.assertEqual([], violations)


if __name__ == "__main__":
    unittest.main()

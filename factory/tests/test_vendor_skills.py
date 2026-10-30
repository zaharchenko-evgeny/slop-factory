#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for scripts/vendor-skills.py --check. Run: uv run factory/tests/test_vendor_skills.py"""
import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("vendor_skills", REPO / "scripts/vendor-skills.py")
vs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vs)


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        shutil.copy(REPO / "skills.lock", self.root)
        shutil.copytree(REPO / "skills/vendor", self.root / "skills/vendor")
        shutil.copytree(REPO / "roles", self.root / "roles", ignore=shutil.ignore_patterns("*.md", "*.sh", "*.json"))

    def test_repository_is_clean(self):
        self.assertEqual(vs.check(), [])

    def test_one_byte_edit_fails(self):
        with (self.root / "skills/vendor/ponytail/SKILL.md").open("a") as f:
            f.write("x")
        self.assertEqual(len(vs.check(self.root)), 1)

    def test_missing_directory_fails(self):
        (self.root / "roles/reviewer/skills.txt").write_text("ponytail-review\nnope\n")
        self.assertIn("reviewer: skill nope is not in skills.lock", vs.check(self.root))
        shutil.rmtree(self.root / "skills/vendor/ponytail-review")
        self.assertTrue(any("no vendored" in e for e in vs.check(self.root)))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Run with: uv run --quiet factory/tests/test_check_refs.py"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "factory/tests/fixtures/check_refs"
sys.path.insert(0, str(ROOT / "factory"))
import check_refs  # noqa: E402


class CheckRefsTest(unittest.TestCase):
    def setUp(self):
        check_refs.ISSUES = {"123"}

    def scan(self, fixture):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text((FIXTURES / fixture).read_text())
            return check_refs.check(root)

    def test_missing_path_is_reported(self):
        self.assertEqual(self.scan("missing.md"), ["README.md:1: missing.py"])

    def test_example_block_is_skipped(self):
        self.assertEqual(self.scan("example.md"), [])

    def test_closed_issue_ref_is_fine(self):
        self.assertEqual(self.scan("closed-issue.md"), [])

    def test_unknown_issue_ref_is_reported(self):
        self.assertEqual(self.scan("unknown-issue.md"), ["README.md:1: #9999"])

    def test_missing_link_path_is_reported(self):
        self.assertEqual(self.scan("missing-link.md"), ["README.md:1: missing-link-target.md"])

    def test_missing_extensionless_link_path_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("[guide](guide)\n")
            self.assertEqual(check_refs.check(root), ["README.md:1: guide"])

    def test_link_path_is_resolved_from_document_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            docs = root / "docs"
            docs.mkdir()
            (root / "guide.md").write_text("exists")
            (docs / "README.md").write_text("[guide](guide.md)\n")
            self.assertEqual(check_refs.check(root), ["docs/README.md:1: guide.md"])

    def test_ignored_run_directory_is_allowed(self):
        self.assertEqual(self.scan("ignored-path.md"), [])


if __name__ == "__main__":
    unittest.main()

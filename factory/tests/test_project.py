#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/project.py. Run: uv run factory/tests/test_project.py"""
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
import project  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/project"


def write_project(root, descriptor):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "factory.project.yaml").write_text(yaml.safe_dump(descriptor, sort_keys=False))
    return root


class ProjectTest(unittest.TestCase):
    def descriptor(self):
        return yaml.safe_load((FIXTURE / "factory.project.yaml").read_text())

    def test_fixture_validates(self):
        self.assertEqual(project.validate(FIXTURE), [])

    def test_store_must_be_exactly_one_kind(self):
        descriptor = self.descriptor()
        descriptor["states"]["ready"]["store"]["tag"] = "ready"
        with tempfile.TemporaryDirectory() as tmp:
            errors = project.validate(write_project(tmp, descriptor))
        self.assertTrue(any("store" in error and "exactly one" in error for error in errors), errors)

    def test_unknown_state_kind_rejected(self):
        descriptor = self.descriptor()
        descriptor["states"]["ready"]["kind"] = "paused"
        with tempfile.TemporaryDirectory() as tmp:
            errors = project.validate(write_project(tmp, descriptor))
        self.assertTrue(any("kind" in error and "paused" in error for error in errors), errors)

    def test_tracker_url_and_project_accepted(self):
        descriptor = self.descriptor()
        descriptor["tracker"] = {"adapter": "youtrack", "url": "https://example.youtrack.cloud", "project": "EX"}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(project.validate(write_project(tmp, descriptor)), [])
        del descriptor["tracker"]["project"]
        with tempfile.TemporaryDirectory() as tmp:
            errors = project.validate(write_project(tmp, descriptor))
        self.assertTrue(any(error.startswith("factory.project.yaml.tracker") for error in errors), errors)

    def test_defaults_merged(self):
        config.set_team(FIXTURE)
        try:
            legacy = config.load()
            self.assertEqual(legacy["branch_pattern"], "ez/{ticket}-{slug}")
            self.assertEqual(legacy["tracker"]["stages"]["ready"], "factory:ready")
            self.assertEqual(legacy["tracker"]["stage_kinds"]["ready"], "active")
            self.assertEqual(legacy["tracker"]["commands"]["review"], {"label": "factory:review"})
        finally:
            config.set_team(None)


if __name__ == "__main__":
    unittest.main()
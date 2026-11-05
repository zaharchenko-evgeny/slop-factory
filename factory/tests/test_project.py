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

    def errors(self, change):
        descriptor = self.descriptor()
        change(descriptor)
        with tempfile.TemporaryDirectory() as tmp:
            return project.validate(write_project(tmp, descriptor))

    def test_repository_role_is_primary_or_project(self):
        other = {"name": "other", "url": "https://github.com/example/other", "default_branch": "main"}
        self.assertEqual(self.errors(lambda d: d["repositories"].append({**other, "role": "project"})), [])
        errors = self.errors(lambda d: d["repositories"].append({**other, "role": "team"}))
        self.assertTrue(any("repositories[1].role" in e and "'team'" in e for e in errors), errors)

    def test_exactly_one_primary_repository(self):
        other = {"name": "other", "url": "https://github.com/example/other", "default_branch": "main", "role": "primary"}
        for change in (lambda d: d["repositories"].append(other), lambda d: d["repositories"][0].pop("role")):
            errors = self.errors(change)
            self.assertTrue(any("exactly one repository needs role primary" in e for e in errors), errors)

    def test_team_variables(self):
        def env(variables):
            return lambda d: d["environments"].setdefault("write", {}).update({"variables": variables})
        self.assertEqual(self.errors(env({"YOUTRACK_URL": "https://y.example"})), [])
        errors = self.errors(env({"FACTORY_X": "1"}))
        self.assertTrue(any("FACTORY_X" in e for e in errors), errors)

    def test_local_target(self):
        def local(value):
            return lambda d: d.setdefault("targets", {}).update({"local": value})
        self.assertEqual(self.errors(local({"harness": "pi", "poll_seconds": 60, "workspace": "~/factory"})), [])
        cases = {"harness": {"harness": "vim"}, "poll_seconds: must be at least 10": {"poll_seconds": 5},
                 "unknown-key": {"model": "x"}}
        for words, value in cases.items():
            errors = self.errors(local(value))
            self.assertTrue(any(words in e for e in errors), (words, errors))

    def test_unknown_mcp_connector_fails_validation_naming_it(self):
        import workflow
        descriptor = self.descriptor()
        wf = yaml.safe_load((FIXTURE / "workflows/delivery.yaml").read_text())
        self.assertEqual(workflow.validate(descriptor, wf), [])
        descriptor["targets"] = {"air": {"mcp_connectors": ["youtrack"]}}
        descriptor["roles"] = {"developer": {"mcp": ["youtrack"], "modes": {"x": {"mcp": ["slack"]}}}}
        wf["nodes"][next(iter(wf["nodes"]))]["mcp"] = ["github"]
        errors = workflow.validate(descriptor, wf)
        self.assertEqual(sorted(e.split(": ")[2] for e in errors), ["github", "slack"], errors)
        self.assertTrue(all(e.startswith("unknown-mcp-connector: ") for e in errors), errors)
        self.assertEqual(self.errors(lambda d: d.update(descriptor)), [])

    def test_defaults_merged(self):
        loaded = project.load(FIXTURE)
        self.assertEqual(loaded["branch_pattern"], "ez/{ticket}-{slug}")
        self.assertIn("states", loaded)
        with tempfile.TemporaryDirectory() as tmp:
            bad = self.descriptor()
            bad["states"]["ready"]["kind"] = "paused"
            with self.assertRaisesRegex(ValueError, "paused"):
                project.load(write_project(tmp, bad))
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
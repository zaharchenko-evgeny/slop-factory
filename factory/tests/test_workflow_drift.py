#!/usr/bin/env python3
"""Tests for drift detection between the engine fixture and the live Slop workflow."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import deploy  # noqa: E402


class WorkflowDriftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture, self.team = Path(self.tmp.name) / "fixture", Path(self.tmp.name) / "team"
        for root in (self.fixture, self.team):
            (root / "workflows").mkdir(parents=True)
            (root / "factory.project.yaml").write_text("project: Slop factory\n")
        self.write_workflow({"finish": {"merged": {"from": ["approved"]}}})

    def write_workflow(self, outcomes):
        data = {"format": 2, "nodes": {name: {"outcomes": value} for name, value in outcomes.items()}}
        for root in (self.fixture, self.team):
            (root / "workflows/delivery.yaml").write_text(yaml.safe_dump(data))

    def test_identical_pair_has_no_drift(self):
        self.assertEqual(deploy.workflow_drift(self.fixture, self.team), [])

    def test_changed_outcome_from_list_names_the_outcome(self):
        (self.team / "workflows/delivery.yaml").write_text(yaml.safe_dump({
            "format": 2, "nodes": {"finish": {"outcomes": {"merged": {"from": ["approved", "blocked"]}}}}
        }))
        self.assertEqual(deploy.workflow_drift(self.fixture, self.team), ["workflows/delivery.yaml: finish.merged"])

    def test_check_fails_and_reports_changed_outcome(self):
        (self.fixture / "factory.project.yaml").write_text(
            "team_repository: https://github.com/example/slop-eval-factory\n")
        (self.team / "factory.project.yaml").write_text(
            "team_repository: https://github.com/example/slop-eval-factory\n")
        (self.team / "workflows/delivery.yaml").write_text(yaml.safe_dump({
            "format": 2, "nodes": {"finish": {"outcomes": {"merged": {"from": ["blocked"]}}}}
        }))
        output = io.StringIO()
        with mock.patch.object(deploy, "SLOP_FIXTURE", self.fixture), contextlib.redirect_stderr(output):
            result = deploy.check_fixture_drift({"team_repository": "https://github.com/example/slop-eval-factory"}, self.team)
        self.assertEqual(result, 1)
        self.assertIn("fixture drift: workflows/delivery.yaml: finish.merged", output.getvalue())

    def test_check_skips_when_fixture_is_absent(self):
        absent_fixture = Path(self.tmp.name) / "absent"
        with mock.patch.object(deploy, "SLOP_FIXTURE", absent_fixture):
            self.assertEqual(deploy.check_fixture_drift({"team_repository": "https://github.com/example/other"}, self.team), 0)


if __name__ == "__main__":
    unittest.main()

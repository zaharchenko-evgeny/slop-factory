#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/workflow.py. Run: uv run factory/tests/test_workflow.py"""
import copy
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config  # noqa: E402
import workflow  # noqa: E402


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.nodes = yaml.safe_load((ROOT / "workflows/nodes.yaml").read_text())
        config.set_team(ROOT)
        cls.flow, cls.states, cls.commands = workflow.migrate(cls.nodes, config.load())

    def errors(self, mutate):
        flow = copy.deepcopy(self.flow)
        mutate(flow["nodes"])
        return workflow.validate(flow, self.states, self.commands)

    def test_migrated_workflow_is_valid(self):
        self.assertEqual(workflow.validate(self.flow, self.states, self.commands), [])

    def test_migrated_workflow_names_no_label_or_event(self):
        text = yaml.safe_dump(self.flow["nodes"])
        for word in ("factory:", "issues.labeled", "pull_request."):
            self.assertNotIn(word, text)

    def test_unknown_state_rejected(self):
        errors = self.errors(lambda n: n["start"]["outcomes"]["started"]["effects"].append({"state": "limbo"}))
        self.assertIn("unknown-state: start.started names limbo", errors)

    def test_unknown_command_rejected(self):
        errors = self.errors(lambda n: n["start"]["wakes_on"].append("command nope"))
        self.assertIn("unknown-command: start wakes_on names nope", errors)

    def test_unknown_effect_rejected(self):
        errors = self.errors(lambda n: n["start"]["outcomes"]["started"]["effects"].append({"explode": 1}))
        self.assertTrue(any(e.startswith("unknown-effect") for e in errors), errors)

    def test_uncovered_active_state_rejected(self):
        states = {**self.states, "review-pending": {"kind": "active", "store": {"label": "factory:review-pending"}}}
        errors = workflow.validate(self.flow, states, self.commands)
        self.assertEqual([e.split(":")[0] for e in errors], ["uncovered-state"])

    def test_migrate_round_trips_this_repository(self):
        self.assertEqual(workflow.render(self.flow, self.states, self.commands), {k: v for k, v in self.nodes.items()})

    def test_migrate_writes_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(workflow.main(["migrate", str(ROOT / "workflows/nodes.yaml"), str(ROOT / "factory.descriptor.yaml"), tmp]), 0)
            self.assertEqual(yaml.safe_load((Path(tmp) / "workflow.v2.yaml").read_text()), self.flow)
            self.assertEqual(yaml.safe_load((Path(tmp) / "states-commands.yaml").read_text())["commands"], self.commands)


if __name__ == "__main__":
    unittest.main()

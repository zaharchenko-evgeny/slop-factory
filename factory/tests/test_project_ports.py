#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for the descriptor-backed tracker port (factory/gate_port.py DescriptorPort) against the fixture project.
Run: uv run factory/tests/test_project_ports.py"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import project  # noqa: E402
from gate_port import MemoryTracker, UnsupportedStore  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/project"


def tracker(**ticket):
    return MemoryTracker({7: {"stage": "draft", **ticket}}, project=project.load(FIXTURE))


class ProjectPortsTest(unittest.TestCase):
    def test_label_state_round_trip(self):
        t = tracker()
        self.assertEqual(t.state(7), "draft")
        self.assertTrue(t.set_state(7, "ready", event="e1"))
        self.assertEqual(t.state(7), "ready")
        self.assertFalse(t.set_state(7, "ready"))
        self.assertEqual(t.facts(7)["seen"], ["e1"])

    def test_pr_state_from_pr_facts(self):
        self.assertEqual(tracker(pr=5, pr_state="open").pr_states(7), [])
        approved = tracker(pr=5, pr_state="open", round_complete=True, round_verdict="approve")
        self.assertEqual(approved.pr_states(7), ["pr-approved"])
        self.assertEqual(tracker(pr=5, pr_state="merged").pr_states(7), ["pr-merged"])
        with self.assertRaisesRegex(ValueError, "PR fact"):
            approved.set_state(7, "pr-approved")

    def test_command_round_trip(self):
        t = tracker()
        self.assertTrue(t.add_command(7, "review"))
        self.assertTrue(t.add_command(7, "pr-review"))
        self.assertEqual(sorted(t.commands(7)), ["pr-review", "review"])
        self.assertTrue(t.remove_command(7, "pr-review"))
        self.assertEqual(t.commands(7), ["review"])

    def test_unsupported_stores_raise(self):
        t = tracker()
        with self.assertRaisesRegex(UnsupportedStore, "needs-review: this tracker has no field store"):
            t.set_state(7, "needs-review")
        with self.assertRaisesRegex(UnsupportedStore, "close: this tracker has no tag store"):
            t.add_command(7, "close")
        with self.assertRaises(KeyError):
            t.add_command(7, "deploy")

    def test_command_templates_fill_their_placeholder(self):
        descriptor = project.load(FIXTURE)
        descriptor["commands"]["implement-<tier>"] = {"store": {"label": "factory:implement-<tier>"}}
        t = MemoryTracker({7: {"stage": "ready"}}, project=descriptor)
        self.assertTrue(t.add_command(7, "implement-mid"))
        self.assertEqual(t.commands(7), ["implement-mid"])
        self.assertTrue(t.remove_command(7, "implement-*"))
        self.assertEqual(t.commands(7), [])

    def test_no_project_is_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, "no project descriptor"):
            MemoryTracker({7: {"stage": "ready"}}).state(7)


if __name__ == "__main__":
    unittest.main()

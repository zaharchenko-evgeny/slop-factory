#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/loop.py. Run: uv run factory/tests/test_loop.py"""
import copy
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import loop  # noqa: E402
import nodes  # noqa: E402
from gate_port import MemoryTracker  # noqa: E402

CONFIG = yaml.safe_load((ROOT / "factory.yaml").read_text())
NODES = nodes.load()
HANDOFFS = {
    "intake": ("proceed", {"risk": "low", "complexity": "small", "tier": "cheap"}),
    "implement": ("done", {}),
}


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.t = MemoryTracker({1: {"stage": "ready", "head": "abc"}})
        facts = self.t.facts  # the memory fake has no reviews: add one approving review at the head
        self.t.facts = lambda n: {**facts(n), "reviews": [{"agent": True, "commit_id": "abc", "verdict": "approve"}]}

    def run_role(self, node, check):
        self.calls.append(node)
        return HANDOFFS[node]

    def tick(self, **kw):
        return loop.tick(self.t, [1], NODES, CONFIG, run_role=self.run_role, **kw)

    def test_ready_to_approved_over_ticks(self):
        self.tick()
        self.assertEqual(self.t.tickets[1]["stage"], "in-dev")
        self.tick()
        self.assertEqual(self.t.tickets[1]["stage"], "in-review")
        self.t.tickets[1].update(round_complete=True, round_head="abc")
        self.tick()
        self.assertEqual(self.t.tickets[1]["stage"], "approved")
        self.assertEqual(self.calls, ["intake", "implement"])

    def test_join_script_approves_without_run_role(self):
        self.t.tickets[1].update(stage="in-review", round_complete=True, round_head="abc")
        res = self.tick()
        self.assertEqual(res["ran"], [("join", 1, "approved")])
        self.assertEqual(self.t.tickets[1]["stage"], "approved")
        self.assertEqual(self.calls, [])

    def test_idempotent_and_quiet(self):
        self.t.tickets[1]["stage"] = "approved"
        before = copy.deepcopy(self.t.tickets)
        self.assertEqual(self.tick()["ran"], [])
        self.assertEqual(self.t.tickets, before)
        self.t.tickets[1]["stage"] = "in-dev"
        self.tick()
        self.assertEqual(self.tick()["ran"], [])  # in-review now: nothing matches
        self.assertEqual(self.calls, ["implement"])

    def test_concurrency_cap_and_node_filter(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["limits"]["concurrent_deliveries"] = 1
        t = MemoryTracker({1: {"stage": "ready", "head": "a"}, 2: {"stage": "ready", "head": "b"}})
        res = loop.tick(t, [1, 2], NODES, cfg, run_role=self.run_role)
        self.assertEqual(len(res["ran"]), 1)
        self.assertEqual(loop.tick(t, [2], NODES, CONFIG, run_role=self.run_role, only_node="implement")["ran"], [])

    def test_cron(self):
        last, now = datetime(2026, 1, 1, 5, 55), datetime(2026, 1, 1, 6, 1)
        self.assertTrue(loop.cron_due("0 6 * * *", last, now))
        self.assertTrue(loop.cron_due("*/10 * * * *", last, now))
        self.assertFalse(loop.cron_due("0 7 * * *", last, now))


if __name__ == "__main__":
    unittest.main()

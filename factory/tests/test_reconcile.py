#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/reconcile.py over MemoryTracker. Run: uv run factory/tests/test_reconcile.py"""
import copy
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
cfg.set_team(ROOT)
import nodes as nodes_mod  # noqa: E402
import reconcile  # noqa: E402
import gate  # noqa: E402
from gate_port import MemoryTracker  # noqa: E402

NODES, CONFIG = nodes_mod.load(), cfg.load()
STAGES, LIMITS = list(CONFIG["tracker"]["stages"]), CONFIG["limits"]
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
OLD, FRESH = "2026-11-03T11:00:00Z", "2026-11-03T11:50:00Z"


def run(ticket, nodes=NODES, **kw):
    tracker = MemoryTracker({1: ticket})
    results, failed = reconcile.reconcile(tracker, nodes, STAGES, LIMITS, now=NOW, **kw)
    return tracker, results, failed


def logged(tracker, kind):
    return [v for k, v in tracker.tickets[1].get("log", []) if k == kind]


class ReconcileTest(unittest.TestCase):
    def test_missing_review_focus_refired(self):
        t, results, _ = run({"stage": "in-review", "pr": 5, "ci": "green", "since": OLD, "focus_missing": ["behaviour"]})
        self.assertEqual(results[0]["node"], "review")
        self.assertIn('"factory:review-behaviour"', logged(t, "label"))

    def test_stale_review_label_without_review_refired(self):
        t, results, _ = run({"stage": "in-review", "pr": 5, "ci": "green", "since": FRESH,
                             "focus_missing": ["behaviour"],
                             "review_labels": [("factory:rereview-behaviour", OLD)]})
        self.assertEqual(results[0]["node"], "review")
        self.assertIn('"factory:review-behaviour"', logged(t, "label"))

    def test_command_label_without_outcome_refired(self):
        t, results, _ = run({"stage": "in-dev", "since": OLD, "command": "factory:implement-mid"})
        self.assertEqual(results[0]["reconcile"], "refire")
        self.assertIn('"factory:implement-mid"', logged(t, "label"))
        self.assertTrue(logged(t, "comment")[0].startswith(f'"{reconcile.PREFIX} stage=in-dev head= re-fired'))

    def test_conflicting_unapproved_pr_goes_to_fix(self):
        t, results, _ = run({"stage": "in-review", "pr": 5, "conflict": True, "since": OLD,
                             "round_complete": False})
        self.assertEqual(results[0]["reconcile"], "conflict")
        self.assertEqual(t.tickets[1]["stage"], "changes-requested")
        self.assertIn('"factory:fix-cheap"', logged(t, "label"))
        code, result = gate.check("fix", {"id": "recovered-conflict", "ticket": 1}, t, NODES, CONFIG)
        self.assertEqual((code, result["merge_main"]), (gate.GO, True))

    def test_stale_fix_label_on_conflicting_pr_refired(self):
        t, results, _ = run({"stage": "changes-requested", "pr": 5, "conflict": True, "since": OLD,
                             "round_complete": False, "command": "factory:fix-cheap",
                             "command_at": FRESH, "commit_at": OLD, "attrs": ["factory:fix-cheap"]})
        self.assertEqual((results[0]["node"], results[0]["reconcile"]), ("fix", "refire"))
        self.assertIn('"factory:fix-cheap"', logged(t, "label"))

    def test_second_miss_blocks_and_needs_human(self):
        refired = [["comment", '"[factory] reconcile: stage=in-dev head=aaa re-fired factory:implement-mid (x)"']]
        t, results, _ = run({"stage": "in-dev", "since": OLD, "head": "aaa", "command": "factory:implement-mid", "log": refired})
        self.assertEqual((results[0]["reconcile"], t.tickets[1]["stage"]), ("blocked", "blocked"))
        self.assertIn('"factory:needs-human"', logged(t, "label"))

    def test_second_miss_counts_per_step(self):
        refired = [["comment", '"[factory] reconcile: stage=in-review head=aaa re-fired factory:review-behaviour (x)"']]
        t, results, _ = run({"stage": "in-review", "pr": 5, "ci": "green", "since": OLD,
                             "head": "aaa", "focus_missing": ["structure-security"], "log": refired})
        self.assertEqual((results[0]["reconcile"], t.tickets[1]["stage"]), ("refire", "in-review"))

    def test_retry_is_per_head(self):
        refired = [["comment", '"[factory] reconcile: stage=in-review head=aaa re-fired factory:review-behaviour (x)"']]
        t, results, _ = run({"stage": "in-review", "pr": 5, "ci": "green", "since": OLD, "head": "bbb",
                             "focus_missing": ["behaviour"], "log": refired})
        self.assertEqual((results[0]["reconcile"], t.tickets[1]["stage"]), ("refire", "in-review"))

    def test_waiting_stage_not_refired(self):
        for stage in ("needs-answers", "blocked", "split"):
            _, results, _ = run({"stage": stage, "since": OLD, "command": "factory:implement-mid"})
            self.assertEqual(results, [], stage)

    def test_fresh_step_waits_for_the_timeout(self):
        _, results, _ = run({"stage": "in-dev", "since": FRESH, "command": "factory:implement-mid"})
        self.assertEqual(results, [])

    def test_steps_come_from_graph(self):
        nodes = copy.deepcopy(NODES)
        nodes["nodes"]["custom"] = {"role": "script", "triggers": ["issues.labeled factory:custom"],
                                    "when": {"stage": "investigating"}, "outcomes": {"x": {}}, "timeout_minutes": 5}
        _, results, _ = run({"stage": "investigating", "since": OLD}, nodes)
        self.assertEqual(results, [])  # a waiting stage even with a node
        nodes["nodes"]["custom"]["when"] = {"stage": "ready"}
        nodes["nodes"]["start"]["enabled"] = False
        t, results, _ = run({"stage": "ready", "since": "2026-11-03T11:54:00Z"}, nodes)
        self.assertEqual(results[0]["node"], "custom")
        self.assertIn('"factory:custom"', logged(t, "label"))


if __name__ == "__main__":
    unittest.main()

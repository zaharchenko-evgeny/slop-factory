#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/loop.py. Run: uv run factory/tests/test_loop.py"""
import copy
import json
import sys
import tempfile
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
        kw = {"last": datetime(2026, 1, 1, 0, 0), "now": datetime(2026, 1, 1, 0, 1), **kw}
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

    def test_blocked_stays_blocked_on_comment_and_resumes_on_ready(self):
        self.t.tickets[1].update(stage="blocked", log=[["comment", '"why"']])
        self.t.tickets[1]["log"].append(["comment", '"a human answer"'])  # a comment never resumes
        self.assertEqual(self.tick()["ran"], [])
        self.assertEqual(self.t.tickets[1]["stage"], "blocked")
        self.t.tickets[1]["stage"] = "ready"  # the human adds factory:ready
        self.t.tickets[1]["branch"] = "ez/1-x"
        self.assertEqual(self.tick()["ran"], [("intake", 1, "proceed")])
        self.assertEqual(self.t.tickets[1]["branch"], "ez/1-x")
        self.assertEqual(self.calls, ["intake"])

    def test_stale_command_label_dropped_on_ready(self):
        self.t.tickets[1]["log"] = [["label", '"factory:implement-cheap"']]
        facts = self.t.facts
        self.t.facts = lambda n: {**facts(n), "attrs": ["factory:implement-cheap" if
                                                        ["label", '"factory:implement-cheap"'] in self.t.tickets[1]["log"]
                                                        else "risk:low"]}
        errors = []
        loop.drop_stale_commands(self.t, 1, errors)
        self.assertEqual((self.t.tickets[1]["log"], errors), ([], []))

    def test_run_dir_does_not_change_next_node(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d) / "runs" / "1"
            run.mkdir(parents=True)
            (run / "intake.handoff.json").write_text("{}")
            before = loop.tick(self.t, [1], NODES, CONFIG, run_role=lambda *a: (None, {}))
            import shutil
            shutil.rmtree(run)
            after = loop.tick(self.t, [1], NODES, CONFIG, run_role=lambda *a: (None, {}))
            self.assertEqual(before, after)
        self.assertEqual(self.tick()["ran"], [("intake", 1, "proceed")])

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

    def test_error_and_stale_do_not_stop_the_tick(self):
        t = MemoryTracker({3: {"stage": "ready", "head": "a"}, 5: {"stage": "ready", "head": "b"}})
        def rr(node, check):
            return ("proceed", {}) if check["ticket"] == 3 else HANDOFFS[node]  # 3: payload misses fields
        res = loop.tick(t, [3, 5], NODES, CONFIG, run_role=rr)
        self.assertEqual([e[1] for e in res["errors"]], [3])
        self.assertEqual(res["ran"], [("intake", 5, "proceed")])
        def stale(node, check):
            t.tickets[5]["head"] = "new"  # the head moved while the role ran
            return "done", {}
        t.tickets[5]["stage"] = "in-dev"
        self.assertEqual(loop.tick(t, [5], NODES, CONFIG, run_role=stale)["ran"], [])
        self.assertEqual(t.tickets[5]["stage"], "in-dev")

    def test_quiet_tick_reports_no_due_nodes(self):
        res = loop.tick(MemoryTracker({}), [], NODES, CONFIG, run_role=self.run_role, last=None)
        self.assertEqual((res["ran"], res["due"]), ([], []))

    def test_intake_handoff_without_complexity_or_tier_proceeds(self):
        h = {"handoff": "ROUTER-INTAKE", "kind": "feature", "ready": "yes", "split": "none", "risk": "high",
             "size_estimate": "~250", "missing": "none", "next": "delivery"}
        res = loop.tick(self.t, [1], NODES, CONFIG, run_role=lambda n, c: (loop.outcome_of(h), {**loop.intake_fields(h), **h}),
                        last=datetime(2026, 1, 1), now=datetime(2026, 1, 1, 0, 1))
        self.assertEqual((res["ran"], res["errors"]), ([("intake", 1, "proceed")], []))
        self.assertEqual(self.t.tickets[1]["stage"], "in-dev")

    def test_due_node_that_applies_nothing_is_not_rerun(self):
        self.t.tickets[1]["stage"] = "in-review"
        calls = []
        def rr(node, check):
            calls.append(node)
            return "answered", {}  # NO_GO: stage not in from
        kw = {"last": datetime(2026, 1, 1, 0, 0), "now": datetime(2026, 1, 1, 0, 10)}
        res = loop.tick(self.t, [1], NODES, CONFIG, run_role=rr, **kw)
        self.assertEqual((calls, res["due"]), (["sweep"], ["sweep"]))
        with tempfile.TemporaryDirectory() as d:
            stamp = Path(d) / "loop" / "last-tick"
            loop.update_stamp(stamp, res, True)
            self.assertFalse(stamp.exists())  # restricted tick
            loop.update_stamp(stamp, {"due": []}, False)
            self.assertFalse(stamp.exists())  # nothing ran
            loop.update_stamp(stamp, res, False, now=1000.0)
            self.assertEqual(stamp.read_text(), "1000.0")

    def test_system_exit_does_not_stop_the_tick(self):
        t = MemoryTracker({7: {"stage": "in-dev", "head": "a"}, 8: {"stage": "in-dev", "head": "b"}})
        def rr(node, check):
            if check["ticket"] == 7:
                raise SystemExit("no worktree")
            return "done", {}
        res = loop.tick(t, [7, 8], NODES, CONFIG, run_role=rr, last=datetime(2026, 1, 1), now=datetime(2026, 1, 1, 0, 1))
        self.assertEqual(([e[1] for e in res["errors"]], res["ran"]), ([7], [("implement", 8, "done")]))

    def test_outcome_of(self):
        self.assertEqual(loop.outcome_of({"handoff": "REVIEWER", "verdict": "approved"}), "approve")
        self.assertEqual(loop.outcome_of({"verdict": "changes-requested"}), "changes")
        self.assertEqual(loop.outcome_of({"handoff": "ROUTER-INTAKE", "ready": "yes", "split": "none"}), "proceed")
        self.assertEqual(loop.outcome_of({"status": "done"}), "done")

    def test_cron(self):
        self.assertTrue(loop.cron_match("0 6 */2 * *", datetime(2026, 1, 1, 6, 0)))
        self.assertFalse(loop.cron_match("0 6 */2 * *", datetime(2026, 1, 2, 6, 0)))
        last, now = datetime(2026, 1, 1, 5, 55), datetime(2026, 1, 1, 6, 1)
        self.assertTrue(loop.cron_due("0 6 * * *", last, now))
        self.assertTrue(loop.cron_due("*/10 * * * *", last, now))
        self.assertFalse(loop.cron_due("0 7 * * *", last, now))


if __name__ == "__main__":
    unittest.main()

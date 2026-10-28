#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Unit tests for factory/sm.py. Run: uv run factory/tests/test_sm.py"""
import sys
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import sm  # noqa: E402

FIX = HERE / "fixtures"
CONFIG = yaml.safe_load((sm.ROOT / "factory.yaml").read_text())


def machine(name):
    return sm.validate(yaml.safe_load((FIX / name).read_text()), CONFIG)


def run_fixture(m, name, *extra):
    fixture = yaml.safe_load((FIX / name).read_text())
    return sm.simulate(m, sm.new_ticket(m, fixture.get("ticket")), fixture["events"] + list(extra))


class Delivery(unittest.TestCase):
    m = sm.validate(yaml.safe_load((sm.ROOT / "workflows/delivery.sm.yaml").read_text()), CONFIG)

    def test_happy_path(self):
        t, trace = run_fixture(self.m, "happy.yaml")
        self.assertEqual([r["to"] for r in trace if r["to"] != r["from"]],
                         ["in-dev", "in-review", "changes-requested", "in-review", "approved"])
        self.assertEqual((t["state"], trace[-1]["wait"], t["pending"]), ("approved", "human", []))
        self.assertEqual(trace[2]["note"], "retry intake 1/1")
        self.assertEqual((t["vars"]["risk"], t["vars"]["reviewed_sha"], t["vars"]["fix_round"]), ("high", "bbb", 2))
        t, rep = sm.step(self.m, t, "pr.merged", {"open_pr_urls": ["x"]})
        self.assertEqual((t["state"], rep["note"]), ("approved", "ignored: no transition matches"))
        t, _ = sm.step(self.m, t, "pr.merged", {"open_pr_urls": []})
        self.assertEqual(t["state"], "done")

    def test_fix_loop_to_exhaustion(self):
        t, trace = run_fixture(self.m, "fix-loop.yaml")
        self.assertEqual((t["state"], t["vars"]["fix_round"]), ("blocked", CONFIG["limits"]["fix_rounds"]))
        self.assertEqual(trace[-1]["note"], "fix_round exhausted at 3")

    def test_same_findings_twice_blocks(self):
        t = sm.new_ticket(self.m, {"state": "in-review", "pending": ["review"], "vars": {"last_blocking_ids": ["B1"]}})
        t, _ = sm.step(self.m, t, "review.done", {"verdict": "changes-requested", "blocking_ids": ["B1"]})
        self.assertEqual(t["state"], "blocked")

    def test_retry_exhausted_fails(self):
        t = sm.new_ticket(self.m, {"state": "in-dev", "pending": ["implement"]})
        t, _ = sm.simulate(self.m, t, ["implement.error", "implement.error"])
        self.assertEqual(t["state"], "failed")

    def test_unexpected_internal_event(self):
        with self.assertRaisesRegex(sm.SMError, "action review is not running"):
            sm.step(self.m, sm.new_ticket(self.m), "review.done")


class Fork(unittest.TestCase):
    def run_join(self, join, events):
        raw = yaml.safe_load((FIX / "fork.sm.yaml").read_text())
        raw["transitions"][-1]["do"]["join"] = join
        m = sm.validate(raw, CONFIG)
        return sm.simulate(m, sm.new_ticket(m, {"state": "approved"}), ["pr.merged", *events])

    def test_join_all_waits_for_every_branch(self):
        t, trace = self.run_join("all", ["lint.done", "test.done"])
        self.assertEqual(trace[1]["note"], "fork gate waits for test")
        self.assertEqual((t["pending"], t["out"]["gate"]["result"]), (["publish"], "done"))
        t, _ = self.run_join("all", ["lint.done", "test.failed"])
        self.assertEqual(t["state"], "blocked")

    def test_join_any_and_first(self):
        t, _ = self.run_join("any", ["lint.failed", "test.done"])
        self.assertEqual(t["pending"], ["publish"])
        t, _ = self.run_join("first", ["test.failed"])
        self.assertEqual((t["state"], t["out"]["gate"]["branches"]), ("blocked", {"lint": None, "test": "failed"}))


class Invalid(unittest.TestCase):
    def test_rejected_with_clear_message(self):
        cases = {
            "bad-guard.sm.yaml": r"function \$sum is outside the subset",
            "unreachable.sm.yaml": r"state failed: unreachable from in-review",
            "loop-no-max.sm.yaml": r"loop needs max",
            "not-mapping.sm.yaml": r"transition 1: must be a mapping .* got 'oops'",
            "unhashable.sm.yaml": r"from \['in-review'\] is not a state(.|\n)*loop.counter must be a declared int var"
                                  r"(.|\n)*loop.on_exhausted must be a state",
        }
        for name, msg in cases.items():
            with self.subTest(name), self.assertRaisesRegex(sm.SMError, msg):
                machine(name)

    def test_other_checks(self):
        base = yaml.safe_load((FIX / "unreachable.sm.yaml").read_text())
        base["states"]["failed"]["actor"] = "robot"
        base["transitions"].append({"from": "in-review", "on": "review.oops", "do": "deploy", "to": "failed"})
        msg = str(self.capture(base))
        for part in ("actor must be one of", "do 'deploy' is not a declared action"):
            self.assertIn(part, msg)

    def test_malformed_shapes(self):
        for raw in ({"states": [1]}, {"states": {"a": "x"}, "initial": "a", "vars": {"v": 1}, "actions": {"r": 2},
                     "events": [[1]], "transitions": [{"from": "a", "on": "r.done", "set": [1], "do": {"name": [1]}}]}):
            with self.subTest(raw):
                self.capture(raw)

    def capture(self, raw):
        with self.assertRaises(sm.SMError) as ctx:
            sm.validate(raw, CONFIG)
        return ctx.exception


class Guards(unittest.TestCase):
    def test_subset_evaluates(self):
        node = sm.compile_expr('$count(a.b) > 1 and ($not(c = "x") or d.missing = 1)')
        self.assertTrue(sm.evaluate(node, {"a": {"b": [1, 2]}, "c": "y", "d": {}}))
        self.assertFalse(sm.evaluate(node, {"a": {"b": [1, 2]}, "c": "x", "d": {}}))

    def test_outside_subset_rejected(self):
        for src, msg in [("not x", r"use \$not"), ("a + 1", "unsupported character '\\+'"),
                         ("a ~> $f", "unsupported character"), ("a = ", "unexpected end"), ("(a", "expected \\)")]:
            with self.subTest(src), self.assertRaisesRegex(sm.SMError, msg):
                sm.compile_expr(src)


if __name__ == "__main__":
    unittest.main()

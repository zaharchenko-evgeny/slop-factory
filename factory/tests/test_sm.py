#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Unit tests for factory/sm.py (the state chart table). Run: uv run factory/tests/test_sm.py"""
import copy
import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sm  # noqa: E402

CONFIG = yaml.safe_load((sm.ROOT / "factory.yaml").read_text())
RAW = yaml.safe_load((sm.ROOT / "workflows/delivery.sm.yaml").read_text())
M = sm.validate(copy.deepcopy(RAW), CONFIG)

START = "human_ready", "intake={result: start, risk: %s}"
JOIN_OK = "join={missing: 0, blocks: false, risk: %s, docs_impact: 'no'}"
JOIN_BLOCK = ("join={missing: 0, blocks: true, same_ids: false, risk: high, docs_impact: 'no', full_blocks: false, "
              "behaviour_blocks: true, security_blocks: false}")


def run(events):
    return sm.simulate(M, events)


def path(rows):
    return [r[2] for r in rows]


class Validate(unittest.TestCase):
    def broken(self, edit):
        raw = copy.deepcopy(RAW)
        edit(raw)
        with self.assertRaises(sm.SMError) as cm:
            sm.validate(raw, CONFIG)
        return str(cm.exception)

    def test_shipped_table_is_valid(self):
        self.assertEqual(len(M["states"]), 10)

    def test_state_without_exit(self):
        msg = self.broken(lambda r: r["transitions"].pop(0))  # draft, blocked and failed lose their exit
        self.assertIn("state draft: has no exit", msg)

    def test_unconsumed_command_label(self):
        msg = self.broken(lambda r: r["commands"].append("factory:extra"))
        self.assertIn("command label factory:extra: must have exactly one consuming event", msg)

    def test_label_applied_but_not_declared(self):
        msg = self.broken(lambda r: r["transitions"][1].update(labels=["factory:nope"]))
        self.assertIn("'factory:nope' is not a declared command label", msg)

    def test_human_transition_outside_the_list(self):
        msg = self.broken(lambda r: r["human_transitions"].pop())
        self.assertIn("human transition failed -> ready is not in human_transitions", msg)

    def test_guard_must_use_known_fields(self):
        msg = self.broken(lambda r: r["transitions"][1].update({"if": "nope = 1"}))
        self.assertIn("'nope' is not a var or event field", msg)

    def test_round_bound_follows_factory_yaml(self):
        msg = self.broken(lambda r: r["vars"]["max_rounds"].update(default=9))
        self.assertIn("must equal factory.yaml limits.fix_rounds", msg)

    def test_old_constructs_are_gone(self):
        msg = self.broken(lambda r: r["transitions"][1].update(loop={"counter": "fix_round"}))
        self.assertIn("unknown key", msg)


class Simulate(unittest.TestCase):
    def happy(self, risk, reviews, labels):
        _, rows = run([START[0], START[1] % risk, "implement={result: done}", "dispatch={expected: true}",
                       *reviews, JOIN_OK % risk, "approve={verdict: approve}", "ci={state: passed}",
                       "closed={merged: true}", "sweep_retro"])
        self.assertEqual(path(rows), ["ready", "in-dev", "in-dev", "in-review", *["in-review"] * len(reviews),
                                      "in-review", "in-review", "approved", "merged", "done"])
        self.assertEqual(rows[3][3], labels)

    def test_happy_path_low(self):
        self.happy("low", ["review_full"], "+review:full")

    def test_happy_path_high(self):
        self.happy("high", ["review_behaviour", "review_structure_security"], "+review:behaviour +review:structure-security")

    def test_fix_round(self):
        t, rows = run([START[0], START[1] % "high", "implement={result: done}", "dispatch={expected: true}",
                       "review_behaviour", "review_structure_security", JOIN_BLOCK, "fix={result: done}",
                       "dispatch={expected: true}"])
        self.assertEqual(path(rows)[-4:], ["in-review", "changes-requested", "changes-requested", "in-review"])
        self.assertEqual((t["vars"]["fix_round"], rows[-1][3]), (1, "+review:behaviour"))  # only the blocked focus

    def test_ci_failure_round_without_blocking_reviewer(self):  # problem 22
        t, rows = run([START[0], START[1] % "high", "implement={result: done}", "dispatch={expected: true}",
                       JOIN_OK % "high", "approve={verdict: approve}", "ci={state: failed}", "fix={result: done}",
                       "dispatch={expected: true}"])
        self.assertEqual(rows[-3][2], "changes-requested")
        self.assertEqual((rows[-1][2], rows[-1][3]), ("in-review", "+review:behaviour +review:structure-security"))
        self.assertEqual(t["vars"]["fix_round"], 1)

    def test_document_round(self):  # problem 24
        docs = "join={missing: 0, blocks: false, risk: low, docs_impact: 'yes'}"
        t, rows = run([START[0], START[1] % "low", "implement={result: done}", "dispatch={expected: true}", docs,
                       "document={result: done}", "dispatch={expected: true}", "approve={verdict: approve}",
                       "ci={state: pending}"])
        self.assertEqual((rows[4][3], rows[6][3]), ("+factory:document", "+factory:approve"))
        self.assertEqual((t["state"], t["vars"]["docs_impact"]), ("approved", "yes"))

    def test_document_blocked_exits(self):
        docs = "join={missing: 0, blocks: false, risk: low, docs_impact: 'yes'}"
        t, _ = run([START[0], START[1] % "low", "implement={result: done}", "dispatch={expected: true}", docs,
                    "document={result: blocked}"])
        self.assertEqual(t["state"], "blocked")

    def test_pr_closed_early(self):  # problem 25
        t, _ = run([START[0], START[1] % "low", "closed={merged: false}"])
        self.assertEqual(t["state"], "blocked")

    def test_ci_needs_the_approver_first(self):  # problem 19
        t, rows = run([START[0], START[1] % "low", "implement={result: done}", "dispatch={expected: true}",
                       "ci={state: passed}"])
        self.assertEqual((t["state"], rows[-1][3]), ("in-review", "ignored: no transition matches"))

    def test_risk_never_goes_down(self):  # B2: a low report after high keeps high
        t = sm.new_ticket(M, "in-review", {"risk": "high"})
        t, _ = sm.step(M, t, "join", {"missing": 0, "blocks": False, "risk": "low", "docs_impact": "no"})
        self.assertEqual(t["vars"]["risk"], "high")
        t, _ = sm.step(M, sm.new_ticket(M, "in-review"), "join", {"missing": 0, "blocks": False, "risk": "high", "docs_impact": "no"})
        self.assertEqual(t["vars"]["risk"], "high")

    def test_needs_human_verdict_reaches_approved(self):  # dry-run: either verdict reaches approved, a human merges
        t, _ = run(["human_ready", "intake={result: start, risk: low}", "implement={result: done}", "dispatch={expected: true}",
                    JOIN_OK % "low", "approve={verdict: needs-human}", "ci={state: passed}"])
        self.assertEqual(t["state"], "approved")

    def test_human_review_outside_approved_becomes_a_fix_round(self):  # problem 23
        ok = {"missing": 0, "blocks": False, "risk": "low", "docs_impact": "no"}
        t = sm.new_ticket(M, "in-review")
        t, _ = sm.step(M, t, "human_review")
        self.assertEqual((t["state"], t["vars"]["human_pending"]), ("in-review", True))
        j, _ = sm.step(M, t, "join", ok)  # at the join
        self.assertEqual((j["state"], j["vars"]["fix_round"], j["vars"]["human_pending"]), ("changes-requested", 1, False))
        t["vars"]["approval"] = "approve"  # or when the ticket would enter approved
        a, _ = sm.step(M, t, "ci", {"state": "passed"})
        self.assertEqual((a["state"], a["labels"]), ("changes-requested", ["factory:fix"]))

    def test_fix_rounds_are_bounded(self):
        t = sm.new_ticket(M, "in-review", {"fix_round": 3, "risk": "high"})
        t, _ = sm.step(M, t, "join", {"missing": 0, "blocks": True, "same_ids": False, "risk": "high"})
        self.assertEqual(t["state"], "blocked")

    def test_command_label_must_be_set(self):
        t, note = sm.step(M, sm.new_ticket(M, "in-dev"), "implement", {"result": "done"})
        self.assertEqual((t["state"], note), ("in-dev", "ignored: label factory:implement is not set"))


if __name__ == "__main__":
    unittest.main()

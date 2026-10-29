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
        t, rep = sm.step(self.m, t, "pr.merged", {"open_pr_urls": []})
        self.assertEqual((t["state"], t["pending"]), ("merged", ["retro"]))

    def test_retro_runs_after_approved_in_merged(self):
        for result in ("done", "failed"):  # best effort: a failed retro still ends done
            t, trace = sm.simulate(self.m, sm.new_ticket(self.m, {"state": "approved"}),
                                   [{"pr.merged": {"open_pr_urls": []}}, f"retro.{result}"])
            self.assertEqual([(r["from"], r["to"], r["run"]) for r in trace],
                             [("approved", "merged", ["retro"]), ("merged", "done", [])])
            self.assertEqual(trace[-1]["wait"], "human")

    def test_enter_passes_through_an_agent_state_without_actions(self):
        raw = yaml.safe_load((sm.ROOT / "workflows/delivery.sm.yaml").read_text())
        for tr in raw["transitions"]:
            if tr.get(True) == "pr.merged":  # YAML 1.1 reads a bare `on` key as true
                tr.pop("do")
            if tr.get(True) == "retro.done":
                tr[True] = "enter"
        m = sm.validate(raw, CONFIG)
        t, rep = sm.step(m, sm.new_ticket(m, {"state": "approved"}), "pr.merged", {"open_pr_urls": []})
        self.assertEqual((t["state"], rep["note"], rep["wait"]), ("done", "through merged", "human"))

    def test_entering_a_state_sets_its_on_enter_hooks_once(self):
        t, _ = sm.step(self.m, sm.new_ticket(self.m, {"state": "in-dev", "pending": ["implement"]}), "implement.blocked")
        self.assertEqual((t["state"], t["hooks"]), ("blocked", ["diagnose", "notify"]))
        t, _ = sm.step(self.m, {**t, "hooks": []}, "pr.merged", {"open_pr_urls": []})  # ignored: stays
        self.assertEqual(t["hooks"], [])
        raw = yaml.safe_load((sm.ROOT / "workflows/delivery.sm.yaml").read_text())
        raw["states"]["in-dev"]["on_enter"] = ["notify"]
        with self.assertRaisesRegex(sm.SMError, "on_enter runs only in human or external states"):
            sm.validate(raw, CONFIG)
        raw["states"]["in-dev"]["on_enter"] = ["nope"]
        with self.assertRaisesRegex(sm.SMError, "on_enter must be a list of declared actions"):
            sm.validate(raw, CONFIG)

    def test_failed_ci_is_a_fix_round_then_approved(self):
        t, trace = run_fixture(self.m, "ci-fix.yaml")
        self.assertEqual([r["to"] for r in trace if r["to"] != r["from"]],
                         ["in-review", "changes-requested", "in-review", "approved"])
        self.assertEqual(trace[7]["run"], ["review_behaviour", "review_structure_security"])  # re-review by risk
        self.assertEqual((t["state"], t["vars"]["fix_round"]), ("approved", 1))
        t, trace = sm.simulate(self.m, sm.new_ticket(self.m, {"state": "in-review", "pending": ["ci"],
                                                           "vars": {"fix_round": 3}}), ["ci.failed"])
        self.assertEqual((t["state"], trace[-1]["note"]), ("blocked", "fix_round exhausted at 3"))
        for result, state in (("pending", "approved"), ("error", "failed")):
            t, _ = sm.simulate(self.m, sm.new_ticket(self.m, {"state": "in-review", "pending": ["ci"]}), [f"ci.{result}"])
            self.assertEqual(t["state"], state)

    def test_review_then_approver_then_publish_for_both_verdicts(self):
        for name, verdict in (("approve-would.yaml", "would-auto-approve"), ("approve-needs-human.yaml", "needs-human")):
            with self.subTest(verdict=verdict):
                t, trace = run_fixture(self.m, name)
                self.assertEqual([r["run"] for r in trace[1:3]], [["approve"], ["publish"]])
                self.assertEqual((t["state"], t["vars"]["approval"], t["out"]["approve"]["verdict"]),
                                 ("approved", verdict, verdict))  # dry-run: a human still merges
        t, _ = sm.simulate(self.m, sm.new_ticket(self.m, {"state": "in-review", "pending": ["approve"]}),
                           ["approve.failed"])
        self.assertEqual(t["state"], "failed")

    def test_docs_impact_runs_the_doc_writer_before_the_approver(self):
        t, trace = run_fixture(self.m, "docs-impact.yaml")
        self.assertEqual([r["run"] for r in trace[2:5]], [["document"], ["approve"], ["publish"]])
        self.assertEqual((t["state"], t["vars"]["risk"], t["vars"]["reviewed_sha"]), ("approved", "high", "a1"))
        t, trace = run_fixture(self.m, "approve-would.yaml")  # docs_impact no: no doc-writer
        self.assertNotIn(["document"], [r["run"] for r in trace])
        for events, state in ((["document.blocked"], "blocked"), (["document.error"] * 2, "failed")):  # error retries once
            t, _ = sm.simulate(self.m, sm.new_ticket(self.m, {"state": "in-review", "pending": ["document"]}), events)
            self.assertEqual(t["state"], state)

    def test_fix_loop_to_exhaustion(self):
        t, trace = run_fixture(self.m, "fix-loop.yaml")
        self.assertEqual((t["state"], t["vars"]["fix_round"]), ("blocked", CONFIG["limits"]["fix_rounds"]))
        self.assertEqual(trace[-1]["note"], "fix_round exhausted at 3")

    def review(self, risk, *reports, base=None):
        """From in-dev, pass the checks and feed each (reviewer action, payload) report."""
        t = sm.new_ticket(self.m, {"state": "in-dev", "pending": ["checks"], **(base or {}),
                                   "vars": {"risk": risk, **((base or {}).get("vars") or {})}})
        t, trace = sm.simulate(self.m, t, ["checks.passed", *[{f"{a}.done": p} for a, p in reports]])
        return t, trace

    def test_high_risk_forks_two_reviewers_and_joins_when_both_report(self):
        t, trace = self.review("high", ("review_behaviour", {"verdict": "approved", "reviewed_sha": "s"}))
        self.assertEqual(trace[0]["run"], ["review_behaviour", "review_structure_security"])
        self.assertEqual((t["state"], t["pending"]), ("in-review", ["review_structure_security"]))
        t, _ = sm.step(self.m, t, "review_structure_security.done", {"verdict": "approved", "reviewed_sha": "s"})
        self.assertEqual((t["pending"], t["vars"]["reviewed_sha"]), (["approve"], "s"))
        t, trace = self.review("low")
        self.assertEqual(trace[0]["run"], ["review_full"])

    def test_rereview_runs_only_blocking_reviewers(self):
        cr = {"verdict": "changes-requested", "reviewed_sha": "s", "blocking_ids": ["X"]}
        ok = {"verdict": "approved", "reviewed_sha": "s"}
        for behaviour, structure, want in [(cr, ok, ["review_behaviour"]), (ok, cr, ["review_structure_security"]),
                                           (cr, cr, ["review_behaviour", "review_structure_security"])]:
            with self.subTest(want=want):
                t, _ = self.review("high", ("review_behaviour", behaviour), ("review_structure_security", structure))
                self.assertEqual((t["state"], t["pending"]), ("changes-requested", ["fix"]))
                t, trace = sm.simulate(self.m, t, ["fix.done", "checks.passed"])
                self.assertEqual(trace[-1]["run"], want)
        t, _ = self.review("low", ("review_full", cr))
        t, trace = sm.simulate(self.m, t, ["fix.done", "checks.passed"])
        self.assertEqual(trace[-1]["run"], ["review_full"])

    def test_blocking_ids_merge_and_same_findings_twice_blocks(self):
        t, _ = self.review("high", ("review_behaviour", {"verdict": "changes-requested", "blocking_ids": ["B1"]}),
                           ("review_structure_security", {"verdict": "changes-requested", "blocking_ids": ["S1"],
                                                          "reviewed_sha": "s"}))
        self.assertEqual(t["vars"]["last_blocking_ids"], ["B1", "S1"])
        t, _ = self.review("low", ("review_full", {"verdict": "changes-requested", "blocking_ids": ["B1"]}),
                           base={"vars": {"last_blocking_ids": ["B1"]}})
        self.assertEqual(t["state"], "blocked")

    def test_review_raises_risk_never_lowers(self):
        for verdict, reported, start, want in [("approved", "high", "low", "high"), ("approved", "low", "high", "high"),
                                               ("changes-requested", "high", "low", "high"),
                                               ("changes-requested", "low", "low", "low")]:
            with self.subTest(verdict=verdict, reported=reported, start=start):
                t, _ = self.review(start, *[(a, {"verdict": verdict, "reviewed_sha": "s", "blocking_ids": ["B1"],
                                                 "risk": reported})
                                            for a in (["review_full"] if start == "low" else
                                                      ["review_behaviour", "review_structure_security"])])
                self.assertEqual(t["vars"]["risk"], want)
                self.assertEqual(t["pending"], ["approve" if verdict == "approved" else "fix"])

    def test_reviewers_low_and_high_yield_high(self):
        for verdict in ("approved", "changes-requested"):
            with self.subTest(verdict=verdict):
                p = {"verdict": verdict, "reviewed_sha": "s", "blocking_ids": ["B1"]}
                t, _ = self.review("low", ("review_full", {**p, "risk": "high"}))
                self.assertEqual(t["vars"]["risk"], "high")
                t = sm.new_ticket(self.m, {"state": "in-dev", "pending": ["checks"], "vars": {"risk": "high"}})
                t, _ = sm.simulate(self.m, t, ["checks.passed", {"review_behaviour.done": {**p, "risk": "low"}},
                                               {"review_structure_security.done": {**p, "risk": "high"}}])
                self.assertEqual(t["vars"]["risk"], "high")

    def test_reviewer_error_after_retry_fails(self):
        t, _ = self.review("high", ("review_behaviour", {"verdict": "approved"}))
        t, _ = sm.simulate(self.m, t, ["review_structure_security.error", "review_structure_security.error"])
        self.assertEqual(t["state"], "failed")

    def test_retry_exhausted_fails(self):
        t = sm.new_ticket(self.m, {"state": "in-dev", "pending": ["implement"]})
        t, _ = sm.simulate(self.m, t, ["implement.error", "implement.error"])
        self.assertEqual(t["state"], "failed")

    def test_unexpected_internal_event(self):
        with self.assertRaisesRegex(sm.SMError, "action review is not running"):
            sm.step(self.m, sm.new_ticket(self.m), "review.done")
        with self.assertRaisesRegex(sm.SMError, "action review_full is not running"):
            sm.step(self.m, sm.new_ticket(self.m), "review_full.done")


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
        node = sm.compile_expr("$append(a, $append(b, c))")
        self.assertEqual(sm.evaluate(node, {"a": [1], "c": 2}), [1, 2])
        self.assertIs(sm.evaluate(node, {}), sm.MISSING)

    def test_outside_subset_rejected(self):
        for src, msg in [("not x", r"use \$not"), ("a + 1", "unsupported character '\\+'"),
                         ("a ~> $f", "unsupported character"), ("a = ", "unexpected end"), ("(a", "expected \\)"),
                         ("$append(a)", "expected ,")]:
            with self.subTest(src), self.assertRaisesRegex(sm.SMError, msg):
                sm.compile_expr(src)


if __name__ == "__main__":
    unittest.main()

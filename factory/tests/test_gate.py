#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/gate.py on both fakes. Run: uv run factory/tests/test_gate.py"""
import copy
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
cfg.set_team(Path(__file__).resolve().parents[2])
import deploy  # noqa: E402
import gate  # noqa: E402
import nodes  # noqa: E402
from gate_port import MemoryTracker, TagTracker  # noqa: E402

CONFIG = cfg.load()
NODES = nodes.load()
PAYLOAD = {"risk": "low", "complexity": "small", "tier": "mid"}


def make(cls, tid, **kw):
    t = {"head": "abc", "blockers_open": 0, "ci": "green"}  # reviewers start on green CI
    if cls is TagTracker:
        t["tags"] = ["stage:ready", "risk:low"]
    else:
        t["stage"] = "ready"
    return cls({tid: {**t, **kw}})


class GateTest(unittest.TestCase):
    def test_writer_answer_resumes_needs_answers(self):
        self.assertEqual(gate.sweep_plan({"stage": "needs-answers", "answered": True}), ("finish", "answered", {}))

    def test_bot_comment_does_not_resume(self):
        self.assertIsNone(gate.sweep_plan({"stage": "needs-answers", "answered": False}))

    def test_verdict_approve_and_approved_both_approve(self):
        for verdict in ("approve", "approved"):
            with self.subTest(verdict=verdict):
                self.assertEqual(gate.outcome_of({"verdict": verdict}), "approve")
        for verdict in ("changes", "changes-requested"):
            with self.subTest(verdict=verdict):
                self.assertEqual(gate.outcome_of({"verdict": verdict}), "changes")

    def test_unknown_verdict_fails_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            event = root / "event.json"
            handoff = root / "handoff.json"
            tracker = root / "tracker.json"
            event.write_text(json.dumps({"id": "e", "ticket": "7"}))
            original = json.dumps({"7": {"stage": "in-review"}})
            tracker.write_text(original)
            for verdict in ("maybe", "", None, ["approve"], {"value": "approve"}):
                with self.subTest(verdict=verdict):
                    handoff.write_text(json.dumps({"ticket": "7", "verdict": verdict, "findings": []}))
                    output = StringIO()
                    with redirect_stdout(output):
                        result = gate.main(["apply", "--node", "review", "--event", str(event),
                                            "--handoff", str(handoff), "--tracker", f"memory:{tracker}"])
                    self.assertEqual(result, gate.BAD)
                    self.assertIn(repr(verdict), output.getvalue())
                    self.assertEqual(tracker.read_text(), original)

    def test_changes_without_blocking_finding_rejected(self):
        tr = MemoryTracker({"7": {"stage": "in-review"}})
        with self.assertRaisesRegex(gate.Bad, "requires a blocking finding"):
            gate.apply("review", "changes", {"ticket": "7", "findings": []}, tr, NODES)
        self.assertEqual(tr.tickets["7"]["stage"], "in-review")

    def test_approve_with_blocking_finding_rejected(self):
        tr = MemoryTracker({"7": {"stage": "in-review"}})
        finding = {"severity": "blocking"}
        with self.assertRaisesRegex(gate.Bad, "cannot include a blocking finding"):
            gate.apply("review", "approve", {"ticket": "7", "findings": [finding]}, tr, NODES)
        self.assertEqual(tr.tickets["7"]["stage"], "in-review")


def tests_for(cls, tid):
    class T(GateTest):
        def tr(self, **kw):
            return make(cls, tid, **kw)

        def ev(self, **kw):
            return {"id": "e1", "ticket": tid, **kw}

        def check(self, tr, event):
            return gate.check("intake", event, tr, NODES, CONFIG)

        def apply(self, tr, payload=None, outcome="proceed"):
            return gate.apply("intake", outcome, {"ticket": tid, "event": "e1", **(payload or PAYLOAD)}, tr, NODES)

        def test_go(self):
            code, out = self.check(self.tr(), self.ev(head="abc"))
            self.assertEqual((code, out["go"], out["role"], out["tier"]), (0, True, "router", "cheap"))
            self.assertEqual(out["role_file"], "roles/router/role.md")
            self.assertIn("grill", out["outcomes"])

        def test_condition_fails(self):
            self.assertEqual(self.check(self.tr(blockers_open=1), self.ev())[0], 10)

        def test_stale_head(self):
            code, out = self.check(self.tr(), self.ev(head="old"))
            self.assertEqual((code, out["reason"]), (10, "stale head"))

        def test_duplicate_event(self):
            tr = self.tr()
            self.assertEqual(self.apply(tr)[0], 0)
            tr.set_stage(tr.tickets[tid], "ready")  # a person re-readies the ticket; same event id
            self.assertEqual(self.check(tr, self.ev())[1]["reason"], "duplicate event")
            self.assertEqual(self.check(tr, self.ev())[0], 10)

        def test_duplicate_event_on_apply(self):
            tr = self.tr()
            self.assertEqual(self.apply(tr)[0], 0)
            tr.set_stage(tr.tickets[tid], "ready")
            code, out = self.apply(tr)
            self.assertEqual((code, out["reason"]), (10, "duplicate event"))
            self.assertEqual(tr.facts(tid)["stage"], "ready")

        def test_duplicate_event_without_stage_effect(self):
            tr = self.tr(focus_missing=["full"])
            tr.set_stage(tr.tickets[tid], "in-review")
            pl = {"ticket": tid, "event": "r1", "head": "abc"}
            self.assertEqual(gate.apply("review", "approve", pl, tr, NODES)[0], 0)
            code, out = gate.apply("review", "approve", pl, tr, NODES)
            self.assertEqual((code, out["reason"]), (10, "duplicate event"))
            self.assertEqual(gate.check("review", {"id": "r1", "ticket": tid}, tr, NODES, CONFIG)[0], 10)

        def test_apply_twice_is_noop(self):
            tr = self.tr()
            self.assertEqual(self.apply(tr)[0], 0)
            after = copy.deepcopy(tr.tickets)
            self.assertEqual(self.apply(tr)[0], 10)
            self.assertEqual(tr.tickets, after)
            self.assertEqual(tr.facts(tid)["stage"], "in-dev")

        def test_effects_skip_when_state_holds(self):
            tr = self.tr()
            effects = [{"kind": "label", "ticket": tid, "value": "x"}, {"kind": "set-field", "ticket": tid, "value": {"a": 1}}]
            self.assertEqual([tr.apply(e) for e in effects + effects], [True, True, False, False])

        def test_failing_effect_leaves_stage(self):
            tr = self.tr()
            tr.fail_on = "label"
            with self.assertRaises(RuntimeError):
                self.apply(tr)
            self.assertEqual(tr.facts(tid)["stage"], "ready")
            tr.fail_on = None
            self.assertEqual(self.apply(tr)[0], 0)  # a retry completes
            self.assertEqual(tr.facts(tid)["stage"], "in-dev")

        def test_bad_payload_names_field(self):
            tr = self.tr()
            with self.assertRaisesRegex(gate.Bad, "`complexity`"):
                self.apply(tr, {"risk": "low", "tier": "mid"})
            self.assertEqual(tr.facts(tid)["stage"], "ready")

        def test_outcome_from(self):
            tr = self.tr()
            code, _ = gate.apply("human", "ready", {"ticket": tid, "actor": "human"}, tr, NODES)
            self.assertEqual(code, 10)  # stage ready is not in the outcome's from

        def test_unknown_node(self):
            with self.assertRaises(gate.Bad):
                gate.check("nope", self.ev(), self.tr(), NODES, CONFIG)

    T.__name__ = f"Gate{cls.__name__}"
    return T


GateMemory = tests_for(MemoryTracker, 7)
GateTag = tests_for(TagTracker, "PROJ-7")


class CliAndValidate(unittest.TestCase):
    def test_implement_done_blocks_branch_past_estimate_and_diff_limit(self):
        tr = MemoryTracker({"7": {"stage": "in-dev", "body": "Size: about 250 lines with tests."}})
        with patch.object(gate, "branch_size", return_value=(500, [("factory/gate.py", 300), ("factory/tests/test_gate.py", 200)])):
            code, out = gate.apply("implement", "done", {"ticket": "7", "event": "e"}, tr, NODES)
        self.assertEqual((code, tr.tickets["7"]["stage"]), (gate.GO, "blocked"))
        comment = next(json.loads(v) for kind, v in tr.tickets["7"]["log"] if kind == "comment")
        self.assertIn("500 lines", comment)
        self.assertIn("250-line estimate", comment)
        self.assertIn("factory/gate.py (300)", comment)

    def test_small_estimate_does_not_block_below_diff_limit(self):
        tr = MemoryTracker({"7": {"stage": "in-dev", "body": "size_estimate: ~50"}})
        with patch.object(gate, "branch_size", return_value=(100, [("factory/gate.py", 100)])):
            code, _ = gate.apply("implement", "done", {"ticket": "7", "event": "e"}, tr, NODES)
        self.assertEqual((code, tr.tickets["7"]["stage"]), (gate.GO, "in-review"))

    def test_fix_done_uses_current_size_line(self):
        tr = MemoryTracker({"7": {"stage": "changes-requested", "round_complete": True,
                                   "body": "Size: about 250 lines."}})
        with patch.object(gate, "branch_size", return_value=(501, [("deleted.txt", 1)])):
            gate.apply("fix", "done", {"ticket": "7", "event": "e"}, tr, NODES)
        self.assertEqual(tr.tickets["7"]["stage"], "blocked")

    def test_all_disputed_fix_rerequests_focus(self):
        review = {"agent": True, "focus": "behaviour", "verdict": "changes", "commit_id": "abc",
                  "findings": ["B1"]}
        tr = MemoryTracker({"7": {"stage": "changes-requested", "head": "abc", "handed_to_fix": True,
                                   "round_complete": True, "reviews": [review]}})
        payload = {"ticket": "7", "event": "e", "findings_addressed": ["B1: disputed — operator-only redeploy"],
                   "disputed_focuses": ["behaviour"]}
        with patch.object(gate, "local_head", return_value="abc"):
            gate.apply("fix", "done", payload, tr, NODES)
        log = [(kind, json.loads(value)) for kind, value in tr.tickets["7"]["log"]]
        self.assertIn(("label", "factory:rereview-behaviour"), log)
        self.assertIn(("comment", "[factory] Disputed findings:\nB1: disputed — operator-only redeploy"), log)
        self.assertEqual(tr.tickets["7"]["stage"], "in-review")

    def test_undisputed_finding_without_commit_blocks(self):
        review = {"agent": True, "focus": "behaviour", "verdict": "changes", "commit_id": "abc",
                  "findings": ["B1", "B2"]}
        tr = MemoryTracker({"7": {"stage": "changes-requested", "head": "abc", "handed_to_fix": True,
                                   "round_complete": True, "reviews": [review]}})
        payload = {"ticket": "7", "event": "e", "findings_addressed": ["B1: disputed — reason"],
                   "disputed_focuses": ["behaviour"]}
        with patch.object(gate, "local_head", return_value="abc"):
            gate.apply("fix", "done", payload, tr, NODES)
        self.assertEqual(tr.tickets["7"]["stage"], "blocked")

    def test_second_dispute_on_same_head_blocks(self):
        review = {"agent": True, "focus": "behaviour", "verdict": "changes", "commit_id": "abc",
                  "rereview": True, "findings": ["B1"]}
        tr = MemoryTracker({"7": {"stage": "changes-requested", "head": "abc", "handed_to_fix": True,
                                   "round_complete": True, "reviews": [review]}})
        payload = {"ticket": "7", "event": "e", "findings_addressed": ["B1: disputed — reason"],
                   "disputed_focuses": ["behaviour"]}
        with patch.object(gate, "local_head", return_value="abc"):
            gate.apply("fix", "done", payload, tr, NODES)
        self.assertEqual(tr.tickets["7"]["stage"], "blocked")

    def test_branch_size_uses_delivery_checkout_and_counts_modified_lines(self):
        with tempfile.TemporaryDirectory() as engine, tempfile.TemporaryDirectory() as delivery:
            (Path(delivery) / "retained.txt").write_text("still here\n")
            result = type("Result", (), {
                "returncode": 0,
                "stdout": "5\t7\tretained.txt\n0\t9\tdeleted.txt\n4\t0\tskills/vendor/copy.txt\n",
                "stderr": "",
            })()
            with patch.object(gate, "ROOT", Path(engine)), \
                    patch.object(gate.subprocess, "run", return_value=result) as run:
                self.assertEqual(gate.branch_size("origin/main", checkout=delivery),
                                 (13, [("retained.txt", 12), ("deleted.txt", 1)]))
            self.assertEqual(run.call_args.kwargs["cwd"], Path(delivery))

    def test_generated_paths_do_not_count_toward_size(self):
        with tempfile.TemporaryDirectory() as delivery:
            result = type("Result", (), {
                "returncode": 0,
                "stdout": "175\t0\tfactory/tests/fixtures/deploy/air-plan.json\n"
                          "12\t3\tdocs/statechart.md\n"
                          "9\t0\tskills/factory-setup/templates/roles/router/role.md\n"
                          "4\t1\tfactory/gate.py\n",
                "stderr": "",
            })()
            with patch.object(gate.subprocess, "run", return_value=result):
                self.assertEqual(gate.branch_size(
                    "origin/main", checkout=delivery,
                    generated_paths=["factory/tests/fixtures/deploy/air-plan.json",
                                     "docs/statechart.md", "docs/statechart-proposed.md",
                                     "skills/factory-setup/templates/**"],
                ), (5, [("factory/gate.py", 5)]))

    def test_intake_size_uses_first_number(self):
        fields = gate.intake_fields({
            "handoff": "ROUTER-INTAKE",
            "size_estimate": "about 40 lines changed; deletions about 2,200 lines",
            "missing": "none",
            "split": "none",
        })
        self.assertEqual((fields["complexity"], fields["tier"]), ("small", "cheap"))

    def test_routines_run_without_a_ticket(self):
        for name in ("retro", "distill"):
            with tempfile.TemporaryDirectory() as d:
                d = Path(d)
                (d / "ev.json").write_text(json.dumps({"source": "schedule", "schedule": name}))
                (d / "h.json").write_text(json.dumps({"handoff": name.upper(), "status": "done"}))
                tr = ["--tracker", f"memory:{d / 't.json'}"]
                (d / "t.json").write_text("{}")
                out = StringIO()
                with redirect_stdout(out):
                    self.assertEqual(gate.main(["check", "--node", name, "--event", str(d / "ev.json"), *tr]), 0)
                self.assertEqual(json.loads(out.getvalue())["ticket"], None)
                with redirect_stdout(StringIO()):
                    self.assertEqual(gate.main(["apply", "--node", name, "--handoff", str(d / "h.json"), *tr]), 0)
                    self.assertEqual(gate.main(["apply", "--node", name, "--outcome", "nope", "--handoff", str(d / "h.json"), *tr]), 2)

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "t.json").write_text(json.dumps({"7": {"stage": "ready", "head": "abc"}}))
            (d / "bad.json").write_text("{")
            (d / "ev.json").write_text(json.dumps({"id": "e", "ticket": "7"}))
            (d / "p.json").write_text(json.dumps({"ticket": "7", "risk": "low"}))
            tr = f"memory:{d / 't.json'}"
            self.assertEqual(gate.main(["check", "--node", "intake", "--event", str(d / "ev.json"), "--tracker", tr]), 0)
            self.assertEqual(gate.main(["check", "--node", "intake", "--event", str(d / "bad.json"), "--tracker", tr]), 2)
            self.assertEqual(gate.main(["check", "--node", "nope", "--event", str(d / "ev.json"), "--tracker", tr]), 2)
            self.assertEqual(gate.main(["apply", "--node", "intake", "--outcome", "proceed", "--payload", str(d / "p.json"), "--tracker", tr]), 2)

    def test_cli_focus_and_tier_reach_check(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "t.json").write_text(json.dumps({"7": {"stage": "ready", "head": "abc", "focus_missing": ["full", "behaviour"]}}))
            (d / "ev.json").write_text(json.dumps({"id": "e", "ticket": "7"}))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                gate.main(["check", "--node", "implement", "--event", str(d / "ev.json"), "--tier", "strong",
                           "--focus", "behaviour", "--tracker", f"memory:{d / 't.json'}"])
            got = json.loads(out.getvalue())
            self.assertEqual((got["tier"], got["focus"]), ("strong", "behaviour"))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                gate.main(["check", "--node", "review", "--event", str(d / "ev.json"), "--tier", "review",
                           "--focus", "behaviour", "--tracker", f"memory:{d / 't.json'}"])
            self.assertEqual(json.loads(out.getvalue())["tier"], "review")

    def test_implement_check_persists_the_pr_head_for_apply(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as d:
            old = Path.cwd()
            try:
                os.chdir(d)
                tracker = Path(d) / "t.json"
                tracker.write_text(json.dumps({"7": {"stage": "in-dev", "head": "pr-head", "pr": 9, "ci": "green"}}))
                event = Path(d) / "ev.json"
                event.write_text(json.dumps({"id": "e", "ticket": "7"}))
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = gate.main(["check", "--node", "implement", "--event", str(event),
                                      "--tracker", f"memory:{tracker}"])
                out = json.loads(output.getvalue())
                self.assertEqual(code, gate.GO)
                self.assertEqual(out["context"]["head"], "pr-head")
                self.assertEqual(gate.checked_head("7"), "pr-head")
            finally:
                os.chdir(old)

    def test_apply_puts_automation_focus_in_payload(self):
        class Rec(MemoryTracker):
            seen_effects = []

            def apply(self, e):
                self.seen_effects.append(e)
                return True
        tr = Rec({"7": {"stage": "in-review", "head": "abc", "ci": "green", "focus_missing": ["full"]}})
        code, _ = gate.apply("review", "approve", {"ticket": "7", "event": "e", "round": 1}, tr, NODES, focus="behaviour")
        self.assertEqual(code, 10)  # focus behaviour is not missing: not applied
        tr.tickets["7"]["focus_missing"] = ["behaviour"]
        self.assertEqual(gate.apply("review", "approve", {"ticket": "7", "event": "e", "round": 1}, tr, NODES, focus="behaviour")[0], 0)
        self.assertEqual([e["focus"] for e in Rec.seen_effects if e["kind"] == "review"], ["behaviour"])

    def test_join_ci_rule(self):
        def run(node, ci, stage="in-review"):
            tr = MemoryTracker({"7": {"stage": stage, "head": "abc", "round_complete": True, "round_head": "abc", "ci": ci}})
            code, _ = gate.apply(node, "approved", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES)
            return code, tr.tickets["7"]["stage"]
        for node in ("join",):
            self.assertEqual(run(node, "none"), (10, "in-review"), node)  # no CI result yet: no transition
            self.assertEqual(run(node, "pending"), (10, "in-review"), node)  # a new push waits for its CI label
            self.assertEqual(run(node, "green"), (0, "approved"), node)
            self.assertEqual(run(node, "red"), (0, "changes-requested"), node)
        tr = MemoryTracker({"7": {"stage": "in-review", "head": "abc", "round_complete": True, "round_head": "abc", "ci": "red"}})
        tr.apply = lambda e, f=tr.apply: labels.append(e["value"]) or f(e)
        labels = []
        gate.apply("join", "approved", {"ticket": "7", "event": "e"}, tr, NODES)  # no tier from the agent
        self.assertIn("factory:fix-cheap", labels)

    def test_ci_list(self):
        self.assertTrue(gate.holds({"ci": ["green", "red"]}, {"ci": "red"}, {}))
        self.assertFalse(gate.holds({"ci": ["green", "red"]}, {"ci": "none"}, {}))

    def test_validate_rejects_unknown_when_key(self):
        n = copy.deepcopy(NODES)
        n["nodes"]["intake"]["when"]["bogus"] = True
        self.assertTrue(any("unknown `when` key bogus" in e for e in nodes.validate(n, CONFIG)))


PR = {"number": 9, "updated_at": "2026-10-31T10:00:00Z", "head": {"ref": "ez/7-x", "sha": "abc"}}
TRIGGERS = {  # recorded webhook payloads, trimmed to the fields the gate reads
    "issues": {"action": "labeled", "issue": {"number": 7, "updated_at": "2026-10-31T09:00:00Z"},
               "sender": {"login": "me"}},
    "pull_request": {"action": "opened", "pull_request": PR},
    "pull_request_review": {"action": "submitted", "pull_request": PR,
                            "review": {"submitted_at": "2026-10-31T11:00:00Z"}},
}


class TriggerTest(unittest.TestCase):
    def run_main(self, argv):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = gate.main(argv)
        return code, json.loads(out.getvalue())

    def test_check_resolves_ticket_and_head_from_raw_payloads(self):
        want = {"issues": (None, "issues:2026-10-31T09:00:00Z"), "pull_request": ("abc", "pull_request:2026-10-31T10:00:00Z"),
                "pull_request_review": ("abc", "pull_request_review:2026-10-31T11:00:00Z")}
        for name, payload in TRIGGERS.items():
            ev = gate.read(self.file(payload), "event", raw=True)
            self.assertEqual((ev["ticket"], ev["head"], ev["id"]), (7, *want[name]), name)

    def test_check_names_a_truncated_event_as_malformed(self):
        fd, name = tempfile.mkstemp()
        os.close(fd)
        f = Path(name)
        f.write_text('{"issue":')
        try:
            code, out = self.run_main(["check", "--node", "intake", "--event", str(f), "--tracker", "memory:" + str(self.file({}))])
            self.assertEqual(code, gate.BAD)
            self.assertIn("malformed event", out["error"])
        finally:
            f.unlink(missing_ok=True)

    def run_sweep(self, args, tracker, stderr=None):
        import contextlib
        import io
        from unittest.mock import patch
        stdout = io.StringIO()
        stderr = stderr or io.StringIO()
        with patch.object(gate.GitHubGateTracker, "from_config", return_value=tracker), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = gate.main(["sweep", *args])
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_sweep_invalid_event_warns_and_runs_full_sweep(self):
        from gate_port import MemoryTracker
        tracker = MemoryTracker({1: {"stage": "in-review"}})
        tracker.sweep_tickets = lambda stages: [1]
        facts = tracker.facts
        tracker.facts = lambda ticket: {**facts(ticket), "round_complete": True, "round_verdict": "approve"}
        fd, name = tempfile.mkstemp()
        os.close(fd)
        f = Path(name)
        f.write_text('{"action":')
        try:
            code, out, warning = self.run_sweep(["--event", str(f)], tracker)
            self.assertEqual(code, gate.GO)
            self.assertEqual((out["sweep"][0]["ticket"], out["sweep"][0]["outcome"]), (1, "approved"))
            self.assertIn("running full sweep", warning)
        finally:
            f.unlink(missing_ok=True)

    def test_sweep_non_event_object_warns_and_runs_full_sweep(self):
        from gate_port import MemoryTracker
        tracker = MemoryTracker({})
        tracker.sweep_tickets = lambda stages: []
        f = self.file({"action": "closed"})
        code, out, warning = self.run_sweep(["--event", str(f)], tracker)
        self.assertEqual((code, out["sweep"]), (gate.GO, []))
        self.assertIn("running full sweep", warning)

    def test_sweep_issue_refires_ready_dependent_of_closed_blocker(self):
        from gate_port import MemoryTracker
        tracker = MemoryTracker({10: {"blocking": [11]}, 11: {"stage": "ready", "blockers_open": 0}})
        tracker.sweep_tickets = lambda stages: []
        code, out, _ = self.run_sweep(["--issue", "10"], tracker)
        self.assertEqual(code, gate.GO)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in out["sweep"]], [(11, "unblocked")])
        self.assertEqual(tracker.tickets[11]["log"][0], ["label", '"factory:ready"'])

    def test_sweep_refires_body_linked_dependent_after_blocker_closes(self):
        from gate_port import MemoryTracker
        tracker = MemoryTracker({10: {"blocking": [11]}, 11: {"stage": "ready", "blockers_open": 0}})
        tracker.sweep_tickets = lambda stages: []
        code, out, _ = self.run_sweep(["--issue", "10"], tracker)
        self.assertEqual(code, gate.GO)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in out["sweep"]], [(11, "unblocked")])
        self.assertEqual(tracker.tickets[11]["log"][0], ["label", '"factory:ready"'])

    def test_check_accepts_air_trigger_wrapper_with_same_facts_as_raw_event(self):
        wrapper = json.loads((ROOT / "factory/tests/fixtures/air-issues-labeled.json").read_text())
        raw = wrapper["data"]
        tracker = self.file({"228": {"stage": "in-dev", "head": "abc", "title": wrapper["data"]["issue"]["title"]}})
        args = ["check", "--node", "implement", "--tier", "cheap"]

        wrapper_code, wrapper_facts = self.run_main([*args, "--event", str(self.file(wrapper)), "--tracker", f"memory:{tracker}"])
        raw_code, raw_facts = self.run_main([*args, "--event", str(self.file(raw)), "--tracker", f"memory:{tracker}"])
        self.assertEqual((wrapper_code, wrapper_facts), (raw_code, raw_facts))
        self.assertTrue(wrapper_facts["go"])
        self.assertEqual((wrapper_facts["ticket"], wrapper_facts["branch"]), (228, "ez/228-gate-check-accepts-air"))

    def file(self, obj):
        d = Path(tempfile.mkdtemp())
        (d / "f.json").write_text(json.dumps(obj))
        return d / "f.json"

    def test_pr_without_ez_branch_exits_10(self):
        f = self.file({"pull_request": {**PR, "head": {"ref": "main", "sha": "a"}}})
        code, out = self.run_main(["check", "--node", "intake", "--event", str(f), "--tracker", "memory:" + str(self.file({}))])
        self.assertEqual((code, out["go"]), (10, False))

    def test_check_branch_from_title_or_pr(self):
        tr = self.file({"7": {"stage": "ready", "head": "abc", "title": "Add the widget thing"}})
        code, out = self.run_main(["check", "--node", "intake", "--event", str(self.file(TRIGGERS["issues"])), "--tracker", f"memory:{tr}"])
        self.assertEqual((out["branch"], out["handoff_path"]), ("ez/7-add-widget-thing", ".factory/handoff/intake.json"))
        tr = self.file({"7": {"stage": "ready", "head": "abc", "title": "t", "branch": "ez/7-x"}})
        code, out = self.run_main(["check", "--node", "intake", "--event", str(self.file(TRIGGERS["pull_request"])), "--tracker", f"memory:{tr}"])
        self.assertEqual((code, out["branch"]), (0, "ez/7-x"))

    def test_apply_handoff_router_intake(self):
        h = {"handoff": "ROUTER-INTAKE", "kind": "feature", "ready": "yes", "split": "none", "risk": "high",
             "size_estimate": "~250", "missing": "none", "next": "delivery"}
        tr = self.file({"7": {"stage": "ready", "head": "abc"}})
        code, out = self.run_main(["apply", "--node", "intake", "--handoff", str(self.file(h)),
                                   "--event", str(self.file(TRIGGERS["issues"])), "--tracker", f"memory:{tr}"])
        t = json.loads(tr.read_text())["7"]
        self.assertEqual((code, t["stage"]), (0, "in-dev"))
        self.assertEqual(t["fields"]["risk"], "high")
        self.assertEqual(t["fields"]["complexity"], "medium")
        self.assertIn(["label", '"factory:implement-mid"'], t["log"])

    def test_check_on_recorded_payload_and_other_handoffs(self):
        tr = MemoryTracker({7: {"stage": "ready", "head": "abc", "title": "t"}})
        for payload in TRIGGERS.values():
            ev = gate.read(self.file(payload), "event", raw=True)
            code, out = gate.check("intake", {**ev, "head": None}, tr, NODES, CONFIG)
            self.assertEqual((code, out["ticket"], out["branch"]), (0, 7, "ez/7-t"))
        grill = {"handoff": "ROUTER-INTAKE", "kind": "feature", "ready": "no", "missing": "acceptance check 2",
                 "risk": "low", "size_estimate": "~120", "split": "none", "next": "blocked"}
        tr = self.file({"7": {"stage": "ready", "head": "abc"}})
        ev = str(self.file(TRIGGERS["issues"]))
        code, _ = self.run_main(["apply", "--node", "intake", "--handoff", str(self.file(grill)), "--event", ev, "--tracker", f"memory:{tr}"])
        self.assertEqual((code, json.loads(tr.read_text())["7"]["stage"]), (0, "needs-answers"))
        split = {**grill, "ready": "yes", "missing": "none", "split": "\n  - First — scope (~100)\n  - Second — more (~90)"}
        tr = self.file({"7": {"stage": "ready", "head": "abc"}})
        code, _ = self.run_main(["apply", "--node", "intake", "--handoff", str(self.file(split)), "--event", ev, "--tracker", f"memory:{tr}"])
        self.assertEqual((code, json.loads(tr.read_text())["7"]["stage"]), (0, "split"))
        self.assertEqual([c["title"] for c in gate.intake_fields(split)["children"]], ["First", "Second"])
        objs = [{"title": "One", "body": "scope\nchecks"}, {"title": "Two"}]
        self.assertEqual(gate.slices(objs), [{"title": "One", "body": "scope\nchecks"}, {"title": "Two", "body": "Two"}])
        tr = self.file({"7": {"stage": "in-dev", "head": "abc"}})
        blocked = {"handoff": "DEVELOPER", "status": "blocked", "branch": "ez/7-x", "blocked_reason": "ambiguous acceptance"}
        code, _ = self.run_main(["apply", "--node", "implement", "--handoff", str(self.file(blocked)), "--event", ev, "--tracker", f"memory:{tr}"])
        self.assertEqual((code, json.loads(tr.read_text())["7"]["stage"]), (0, "blocked"))

    def test_run_cap_blocks_the_transition_that_would_start_a_run(self):
        tr = MemoryTracker({"7": {"stage": "in-review", "head": "abc", "ci": "red", "agent_runs": 8, "run_cap": 8}})
        code, out = gate.apply("join", "changes", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES)
        self.assertEqual((code, tr.tickets["7"]["stage"]), (0, "blocked"))
        self.assertNotIn(["label", '"factory:fix-mid"'], tr.tickets["7"].get("log", []))  # no fix run starts
        tr = MemoryTracker({"7": {"stage": "in-review", "head": "abc", "ci": "red", "agent_runs": 7, "run_cap": 8}})
        gate.apply("join", "changes", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES)
        self.assertEqual(tr.tickets["7"]["stage"], "changes-requested")

    def test_fix_round_cap_blocks_the_join_instead_of_a_silent_refusal(self):
        tr = MemoryTracker({"7": {"stage": "in-review", "head": "abc", "ci": "red", "fix_rounds": 3, "fix_cap": 3}})
        gate.apply("join", "changes", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES)
        self.assertEqual(tr.tickets["7"]["stage"], "blocked")
        self.assertIn(["label", '"factory:needs-human"'], tr.tickets["7"]["log"])
        self.assertEqual(tr.tickets["7"]["assignees"], [CONFIG["notify"]["github_user"]])

    def test_person_change_request_addressed_assigns_reviewer(self):
        seen = []
        class Capture(MemoryTracker):
            def apply(self, effect):
                seen.append(effect.copy())
                return super().apply(effect)
        tr = Capture({"7": {"stage": "in-review", "head": "new-head", "pr": 42, "ci": "green",
                             "waiting_reviewers": ["reviewer"], "focus_missing": []}})
        gate.apply("join", "waiting", {"ticket": "7", "event": "e"}, tr, NODES)
        self.assertIn(["label", '"factory:needs-human"'], tr.tickets["7"]["log"])
        self.assertEqual(tr.tickets["7"]["assignees"], ["reviewer"])
        self.assertIn('"[factory] Re-review PR #42: your change request was addressed at new-head"',
                      [value for kind, value in tr.tickets["7"]["log"] if kind == "comment"])
        self.assertIn({"kind": "comment", "ticket": "7",
                       "value": "[factory] Re-review PR #42: your change request was addressed at new-head",
                       "on": "pr"}, seen)

    def test_needs_answers_assigns_ticket_author(self):
        tr = MemoryTracker({"7": {"stage": "ready", "author": "ticket-author"}})
        gate.apply("intake", "grill", {"ticket": "7", "questions": "[factory] Which API?"}, tr, NODES)
        self.assertIn(["label", '"factory:needs-human"'], tr.tickets["7"]["log"])
        self.assertEqual(tr.tickets["7"]["assignees"], ["ticket-author"])

    def test_label_removed_when_stage_changes(self):
        tr = MemoryTracker({"7": {"stage": "blocked", "attrs": ["factory:needs-human"],
                                   "assignees": [CONFIG["notify"]["github_user"]]}})
        gate.apply("human", "ready", {"ticket": "7", "actor": "human"}, tr, NODES)
        self.assertNotIn("factory:needs-human", tr.tickets["7"]["attrs"])
        self.assertEqual(tr.tickets["7"]["assignees"], [])

    def test_a_stop_clears_every_command_label(self):
        # review of PR #198: a label left on the ticket makes the command after factory:ready a no-op
        seen = []
        class T(MemoryTracker):
            def apply(self, e):
                seen.append((e["kind"], e["value"], e.get("on")))
                return super().apply(e)
        tr = T({"7": {"stage": "in-review", "head": "abc", "ci": "red", "fix_rounds": 3, "fix_cap": 3}})
        gate.apply("join", "changes", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES)
        self.assertIn(("unlabel", "factory:fix-*", "pr"), seen)
        self.assertIn(("unlabel", "factory:implement-*", None), seen)
        self.assertEqual(seen[-1], ("stage", "blocked", None))

    def test_implement_error_swaps_the_tier_label(self):
        eff = [list(e)[0] for e in NODES["nodes"]["implement"]["outcomes"]["error"]["effects"]]
        self.assertEqual(eff[:2], ["unlabel", "label"])

    def test_implement_after_push_does_not_refire_ci(self):
        seen = []
        class T(MemoryTracker):
            def apply(self, e):
                seen.append((e["kind"], e["value"], e.get("on")))
                return super().apply(e)
        tr = T({"7": {"stage": "in-dev", "head": "pushed", "ci": "green", "pr": 5}})
        gate.apply("implement", "done", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES,
                   checked_pr_head="before-push")
        self.assertNotIn(("label", "factory:ci-green", "pr"), seen)
        self.assertEqual(tr.tickets["7"]["stage"], "in-review")

    def test_resumed_implement_with_no_commit_refires_the_ci_label(self):
        seen = []
        class T(MemoryTracker):
            def apply(self, e):
                seen.append((e["kind"], e["value"], e.get("on")))
                return super().apply(e)
        tr = T({"7": {"stage": "in-dev", "head": "abc", "ci": "green", "pr": 5}})
        with patch.object(gate, "local_head", return_value="abc"):
            gate.apply("implement", "done", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES,
                       checked_pr_head="abc")
        self.assertIn(("label", "factory:ci-green", "pr"), seen)
        self.assertEqual(tr.tickets["7"]["stage"], "in-review")
        self.assertEqual(seen[-1][0], "join")  # after the stage change: reviews already at this head decide
        self.assertEqual(seen[-2], ("stage", "in-review", None))

    def test_resumed_implement_with_unpublished_commit_does_not_refire_old_ci(self):
        seen = []
        class T(MemoryTracker):
            def apply(self, e):
                seen.append((e["kind"], e["value"], e.get("on")))
                return super().apply(e)
        tr = T({"7": {"stage": "in-dev", "head": "abc", "ci": "green", "pr": 5}})
        with patch.object(gate, "local_head", return_value="new-local-commit"):
            gate.apply("implement", "done", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES,
                       checked_pr_head="abc")
        self.assertNotIn(("label", "factory:ci-green", "pr"), seen)

    def test_bug_intake_proceeds(self):
        h = {"handoff": "ROUTER-INTAKE", "kind": "bug", "ready": "yes", "split": "none"}
        self.assertEqual(gate.outcome_of(h), "proceed")

    def test_join_tier_from_complexity_label(self):
        facts = {"round_complete": True, "round_verdict": "changes", "attrs": ["complexity:large"]}
        self.assertEqual(gate.join_decision(facts), ("changes", {"tier": "strong"}))
        self.assertEqual(gate.join_decision({**facts, "attrs": []})[1], {"tier": "mid"})
        self.assertIsNone(gate.join_decision({**facts, "round_complete": False}))


class StaleSweepTest(unittest.TestCase):
    NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    OLD, FRESH = "2026-11-01T08:00:00Z", "2026-11-01T11:30:00Z"

    def sweep(self, tickets):
        class Tr(MemoryTracker):
            def sweep_tickets(self, stages):
                return [n for n, t in self.tickets.items() if self.get_stage(t) in stages]
        tr = Tr(tickets)
        return tr, gate.sweep_stale(tr, NODES, 2, self.NOW)[0]

    def test_stale_ready_is_refired_once_then_blocked(self):
        tr, res = self.sweep({1: {"stage": "ready", "since": self.OLD}})
        self.assertEqual([(r["recover"], r["applied"]) for r in res], [("refire", True)])
        self.assertEqual(tr.tickets[1]["stage"], "ready")
        self.assertIn(["label", '"factory:ready"'], tr.tickets[1]["log"])
        self.assertIn("[factory] recover: stage=ready", tr.tickets[1]["log"][-1][1])
        res = gate.sweep_stale(tr, NODES, 2, self.NOW)[0]
        self.assertEqual([r["recover"] for r in res], ["blocked"])
        self.assertEqual(tr.tickets[1]["stage"], "blocked")
        self.assertIn("did not respond twice", json.dumps(tr.tickets[1]["log"]))

    def test_stale_sweep_reports_ticket_as_progress(self):
        class Tr(MemoryTracker):
            def sweep_tickets(self, stages):
                return [1]
        tr = Tr({1: {"stage": "ready", "since": self.OLD}})
        progress = []
        results, failed = gate.sweep_stale(tr, NODES, 2, self.NOW, progress=progress.append)
        self.assertFalse(failed)
        self.assertEqual(progress, results)

    def test_fresh_ticket_is_not_touched(self):
        tr, res = self.sweep({1: {"stage": "ready", "since": self.FRESH}, 2: {"stage": "ready", "since": self.OLD,
                                                                              "blockers_open": 1}})
        self.assertEqual(res, [])
        self.assertNotIn("log", tr.tickets[1])
        self.assertNotIn("log", tr.tickets[2])  # waiting on a blocker, not on a lost run

    def test_command_label_without_outcome_stale_after_30_minutes(self):
        tr, res = self.sweep({1: {"stage": "ready", "since": "2026-11-01T11:00:00Z", "command_pending": True}})
        self.assertEqual([(r["recover"], r["stage"]) for r in res], [("refire", "ready")])

    def test_fresh_command_label_not_stale(self):
        tr, res = self.sweep({1: {"stage": "ready", "since": "2026-11-01T11:45:00Z", "command_pending": True}})
        self.assertEqual(res, [])
        self.assertNotIn("log", tr.tickets[1])

    def test_in_review_with_ci_green_and_no_review_refires_ci_green(self):
        tr, res = self.sweep({3: {"stage": "in-review", "since": self.OLD, "pr": 9, "ci_label": "green", "reviews": []}})
        self.assertEqual([(r["recover"], r["stage"]) for r in res], [("refire", "in-review")])
        self.assertIn(["label", '"factory:ci-green"'], tr.tickets[3]["log"])

    def test_in_review_without_ci_label_only_comments(self):
        tr, res = self.sweep({3: {"stage": "in-review", "since": self.OLD, "pr": 9, "ci_label": None}})
        self.assertEqual([r["recover"] for r in res], ["comment"])
        self.assertFalse([e for e in tr.tickets[3]["log"] if e[0] == "label"])

    def test_comment_only_note_does_not_use_the_retry(self):
        tr, res = self.sweep({3: {"stage": "in-review", "since": self.OLD, "pr": 9, "ci_label": None}})
        self.assertEqual([r["recover"] for r in res], ["comment"])
        tr.tickets[3].update(ci_label="green", reviews=[])  # CI finished, the reviewer run then died
        res = gate.sweep_stale(tr, NODES, 2, self.NOW)[0]
        self.assertEqual([r["recover"] for r in res], ["refire"])
        self.assertEqual(gate.sweep_stale(tr, NODES, 2, self.NOW)[0][0]["recover"], "blocked")

    def test_in_dev_command_without_pr_and_fix_without_commit(self):
        tr, res = self.sweep({4: {"stage": "in-dev", "since": self.OLD, "command": "factory:implement-mid"},
                              5: {"stage": "changes-requested", "since": self.OLD, "pr": 7, "command": "factory:fix-mid",
                                  "command_at": "2026-11-01T07:00:00Z", "commit_at": "2026-11-01T06:00:00Z"},
                              6: {"stage": "changes-requested", "since": self.OLD, "pr": 8, "command": "factory:fix-mid",
                                  "command_at": "2026-11-01T07:00:00Z", "commit_at": "2026-11-01T07:30:00Z"}})
        self.assertEqual(sorted(r["ticket"] for r in res), [4, 5])
        self.assertIn(["label", '"factory:implement-mid"'], tr.tickets[4]["log"])

    def test_stale_update_command_is_refired_once_then_blocked(self):
        tr, res = self.sweep({5: {"stage": "changes-requested", "since": self.OLD, "pr": 7,
                                  "command": "factory:update-cheap", "command_at": self.FRESH,
                                  "commit_at": self.OLD}})
        self.assertEqual([r["recover"] for r in res], ["refire"])
        res = gate.sweep_stale(tr, NODES, 2, self.NOW)[0]
        self.assertEqual([r["recover"] for r in res], ["blocked"])
        self.assertEqual(tr.tickets[5]["stage"], "blocked")

    def test_conflicting_pr_with_old_update_label_refired(self):
        tr, res = self.sweep({5: {"stage": "changes-requested", "since": self.OLD, "pr": 7, "conflict": True,
                                  "attrs": ["factory:update-cheap"],
                                  "command": "factory:update-cheap", "command_at": "2026-10-31T08:00:00Z",
                                  "commit_at": self.OLD, "command_pending": False}})
        self.assertEqual([(r["recover"], r["why"]) for r in res],
                         [("refire", "conflicting PR has an update label with no later run")])
        self.assertIn(["label", '"factory:update-cheap"'], tr.tickets[5]["log"])

    def test_dry_run_writes_nothing(self):
        tr = MemoryTracker({1: {"stage": "ready", "since": self.OLD}})
        tr.sweep_tickets = lambda stages: [1]
        self.assertEqual(len(gate.sweep_stale(tr, NODES, 2, self.NOW, dry_run=True)[0]), 1)
        self.assertNotIn("log", tr.tickets[1])

    def test_idle_unmatched_ticket_flagged_by_recover(self):
        tr, res = self.sweep({3: {"stage": "in-review", "since": self.OLD, "issue_state": "open"}})
        self.assertEqual([r["recover"] for r in res], ["human"])
        self.assertIn(["label", '"factory:needs-human"'], tr.tickets[3]["log"])
        self.assertEqual(tr.tickets[3]["assignees"], [CONFIG["notify"]["github_user"]])

    def test_recover_preserves_existing_reviewer_wait(self):
        tr, res = self.sweep({3: {"stage": "in-review", "since": self.OLD, "issue_state": "open",
                                  "attrs": ["factory:needs-human"], "assignees": ["reviewer"]}})
        self.assertEqual([r["recover"] for r in res], ["human"])
        self.assertEqual(tr.tickets[3]["assignees"], ["reviewer"])
        self.assertNotIn("log", tr.tickets[3])


class SpecTest(unittest.TestCase):
    """`--spec`: check and apply need the automation's spec, never workflows/nodes.yaml."""

    def run_gate(self, d, *args, stage="in-dev"):
        import contextlib
        import io
        (d / "t.json").write_text(json.dumps({"7": {"stage": stage, "head": "abc", "ci": "none"}}))
        (d / "ev.json").write_text(json.dumps({"id": "e", "ticket": "7"}))
        out = io.StringIO()
        real_load = nodes.load
        nodes.load = lambda *a: self.fail("read the node graph")
        try:
            with contextlib.redirect_stdout(out):
                code = gate.main([*args, "--tracker", f"memory:{d / 't.json'}", "--spec", str(d / "spec.json")])
        finally:
            nodes.load = real_load
        return code, out.getvalue()

    def spec(self, d):
        spec = deploy.node_spec("implement", NODES["nodes"]["implement"], CONFIG, "mid", None, NODES)
        (d / "spec.json").write_text(json.dumps(spec))

    def test_check_and_apply_read_only_the_spec(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self.spec(d)
            code, out = self.run_gate(d, "check", "--node", "implement", "--event", str(d / "ev.json"), "--tier", "mid")
            self.assertEqual((code, json.loads(out)["outcomes"]), (0, ["done", "error", "blocked", "failed"]))
            (d / "p.json").write_text(json.dumps({"ticket": "7", "event": "e", "tier": "mid"}))
            code, _ = self.run_gate(d, "apply", "--node", "implement", "--outcome", "done", "--payload", str(d / "p.json"))
            self.assertEqual(code, 0)

    def test_sweep_spec_carries_the_script_nodes_it_applies(self):
        with tempfile.TemporaryDirectory() as d:
            spec = deploy.node_spec("recover", NODES["nodes"]["recover"], CONFIG, "cheap", None, NODES)
            (Path(d) / "s.json").write_text(json.dumps(spec))
            got = gate.spec_nodes(Path(d) / "s.json")["nodes"]
            self.assertLessEqual({"join", "finish", "recover"}, set(got))
            self.assertEqual(got["join"]["else_tier"], "cheap")

    def test_apply_rejects_an_outcome_outside_the_spec_and_a_wrong_stage(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            self.spec(d)
            (d / "p.json").write_text(json.dumps({"ticket": "7", "event": "e"}))
            code, out = self.run_gate(d, "apply", "--node", "implement", "--outcome", "approve", "--payload", str(d / "p.json"))
            self.assertEqual((code, json.loads(out)["error"]), (2, "unknown outcome: approve"))
            code, _ = self.run_gate(d, "check", "--node", "implement", "--event", str(d / "ev.json"), stage="ready")
            self.assertEqual(code, 10)  # the spec's `when` still holds the stage
            spec = json.loads((d / "spec.json").read_text())
            spec["outcomes"]["done"]["from"] = ["approved"]
            (d / "spec.json").write_text(json.dumps(spec))
            code, out = self.run_gate(d, "apply", "--node", "implement", "--outcome", "done", "--payload", str(d / "p.json"))
            self.assertEqual((code, json.loads(out)["reason"]), (10, "stage in-dev is not in the outcome's from"))


class ConflictTest(unittest.TestCase):
    def sweep(self, tickets):
        tr = MemoryTracker(tickets)
        tr.sweep_tickets = lambda stages: [n for n, t in tr.tickets.items() if t["stage"] in stages]
        return tr, gate.sweep(tr, NODES)

    def test_merge_flags_approved_conflicting_pr_for_update(self):
        tr, (results, failed) = self.sweep({
            7: {"stage": "approved", "pr_state": "open", "conflict": True},
            8: {"stage": "approved", "pr_state": "open"}})
        self.assertFalse(failed)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in results], [(7, "conflict")])
        self.assertIn(["label", '"factory:update-cheap"'], tr.tickets[7]["log"])
        self.assertEqual(tr.tickets[7]["stage"], "changes-requested")
        self.assertNotIn("log", tr.tickets[8])

    def test_conflict_waits_for_join_then_rides_fix_round(self):
        tr, (results, _) = self.sweep({7: {"stage": "in-review", "pr_state": "open", "conflict": True,
                                           "round_complete": False}})
        self.assertEqual((results, tr.tickets[7]["stage"]), ([], "in-review"))
        tr.tickets[7].update(round_complete=True, round_verdict="changes", ci="green")
        gate.sweep(tr, NODES)
        self.assertEqual(tr.tickets[7]["stage"], "changes-requested")
        code, out = gate.check("fix", {"id": "e", "ticket": 7}, tr, NODES, CONFIG)
        self.assertEqual((code, out["merge_main"]), (0, True))
        tr.tickets[7]["conflict"] = False
        self.assertFalse(gate.check("fix", {"id": "e", "ticket": 7}, tr, NODES, CONFIG)[1]["merge_main"])

    def test_dispute_rereview_sweep_approves_or_starts_a_new_fix_round(self):
        original = {"agent": True, "focus": "full", "verdict": "changes", "commit_id": "abc",
                    "findings": ["B1"]}
        for verdict, expected in (("approve", "approved"), ("changes", "changes-requested")):
            rereview = {**original, "verdict": verdict, "rereview": True}
            tr, (results, failed) = self.sweep({7: {"stage": "in-review", "head": "abc", "handed_to_fix": True,
                                                    "round_complete": True, "round_verdict": verdict,
                                                    "reviews": [original, rereview], "ci": "green",
                                                    "attrs": ["risk:low", "complexity:small"],
                                                    "labels": ["factory:fix-mid"]}})
            self.assertFalse(failed)
            self.assertEqual(tr.tickets[7]["stage"], expected)

        pending = {"agent": True, "focus": "full", "verdict": "approve", "commit_id": "abc", "rereview": True}
        self.assertEqual(gate.sweep_plan({"stage": "in-review", "head": "abc", "handed_to_fix": True,
                                          "round_complete": False, "waiting_reviewers": ["alice"],
                                          "reviews": [pending]}), ("join", "waiting", {}))

    def test_dispute_join_requires_agent_and_all_requested_focuses(self):
        untrusted = {"agent": False, "focus": "behaviour", "verdict": "approve", "commit_id": "abc",
                     "rereview": True}
        facts = {"stage": "in-review", "head": "abc", "handed_to_fix": True, "round_complete": True,
                 "round_verdict": "approve", "reviews": [untrusted]}
        self.assertIsNone(gate.join_decision(facts))

        facts.update(handed_to_fix=False, pending_rereviews=["structure-security"])
        self.assertIsNone(gate.join_decision(facts))
        tr, (results, failed) = self.sweep({7: {**facts, "stage": "in-review", "ci": "green"}})
        self.assertFalse(failed)
        self.assertEqual((results, tr.tickets[7]["stage"]), ([], "in-review"))

    def test_unresolvable_update_blocks_with_files(self):
        tr = MemoryTracker({7: {"stage": "changes-requested", "attrs": ["factory:update-cheap"]}})
        reason = "[factory] Blocked: merge conflict in factory/gate.py, workflows/nodes.yaml"
        code, _ = gate.apply("update", "unresolvable", {"ticket": 7, "event": "e", "reason": reason}, tr, NODES)
        self.assertEqual((code, tr.tickets[7]["stage"]), (0, "blocked"))
        self.assertIn(["comment", json.dumps(reason)], tr.tickets[7]["log"])
        self.assertNotIn("factory:update-cheap", tr.tickets[7]["attrs"])

    def test_update_resolved_removes_label(self):
        tr = MemoryTracker({7: {"stage": "changes-requested", "attrs": ["factory:update-cheap"]}})
        code, _ = gate.apply("update", "resolved", {"ticket": 7, "event": "e"}, tr, NODES)
        self.assertEqual((code, tr.tickets[7]["stage"]), (gate.GO, "in-review"))
        self.assertNotIn("factory:update-cheap", tr.tickets[7]["attrs"])


    def test_escalate_labels_next_tier_and_strong_blocks(self):
        for tier, label in (("cheap", "mid"), ("mid", "strong")):
            tr = MemoryTracker({7: {"stage": "changes-requested"}})
            code, _ = gate.apply("update", "escalate", {"ticket": 7, "event": "e"}, tr, NODES, tier=tier)
            self.assertEqual(code, 0)
            self.assertIn(["label", json.dumps(f"factory:update-{label}")], tr.tickets[7]["log"])
        tr = MemoryTracker({7: {"stage": "changes-requested"}})
        code, _ = gate.apply("update", "escalate", {"ticket": 7, "event": "e", "reason": "[factory] conflicts in a.py"},
                             tr, NODES, tier="strong")
        self.assertEqual((code, tr.tickets[7]["stage"]), (0, "blocked"))
        self.assertIn(["comment", json.dumps("[factory] conflicts in a.py")], tr.tickets[7]["log"])

    def test_deployed_update_spec_escalates_through_its_tiers(self):
        with tempfile.TemporaryDirectory() as d:
            spec = deploy.node_spec("update", NODES["nodes"]["update"], CONFIG, "cheap", None, NODES)
            (Path(d) / "s.json").write_text(json.dumps(spec))
            nodes = gate.spec_nodes(Path(d) / "s.json")
            tr = MemoryTracker({7: {"stage": "changes-requested"}})
            code, _ = gate.apply("update", "escalate", {"ticket": 7, "event": "e"}, tr, nodes, tier="cheap")
            self.assertEqual(code, 0)
            self.assertIn(["label", json.dumps("factory:update-mid")], tr.tickets[7]["log"])

    def test_recover_flags_approved_conflict_and_names_conflict_without_ci(self):
        class Tr(MemoryTracker):
            def sweep_tickets(self, stages):
                return [n for n, t in self.tickets.items() if self.get_stage(t) in stages]
        old = "2026-11-01T08:00:00Z"
        now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        tr = Tr({1: {"stage": "approved", "since": old, "pr_state": "open", "conflict": True},
                 2: {"stage": "in-review", "since": old, "pr": 9, "ci_label": None, "conflict": True}})
        res = gate.sweep_stale(tr, NODES, 2, now)[0]
        self.assertEqual([r["recover"] for r in res], ["conflict", "comment"])
        self.assertIn(["label", '"factory:update-cheap"'], tr.tickets[1]["log"])
        self.assertEqual(tr.tickets[1]["stage"], "changes-requested")
        self.assertIn("conflicts with main", json.dumps(tr.tickets[2]["log"]))
        self.assertEqual(tr.tickets[2]["stage"], "in-review")


class ApprovedSweepTest(unittest.TestCase):
    def sweep(self, ticket):
        tr = MemoryTracker({7: ticket})
        tr.sweep_tickets = lambda stages: [n for n, t in tr.tickets.items() if t["stage"] in stages]
        return tr, gate.sweep(tr, NODES)

    def test_approved_with_new_head_returns_to_review(self):
        tr, (results, failed) = self.sweep({"stage": "approved", "pr_state": "open", "head": "new",
                                            "round_head": "old", "ci": "green"})

        self.assertFalse(failed)
        self.assertEqual([(r["node"], r["outcome"]) for r in results], [("reopen", "reopened")])
        self.assertEqual(tr.tickets[7]["stage"], "in-review")

    def test_approved_with_red_ci_returns_to_review_and_join_changes(self):
        tr, (results, failed) = self.sweep({"stage": "approved", "pr_state": "open", "head": "same",
                                            "round_head": "same", "ci": "red", "round_complete": True,
                                            "round_verdict": "changes"})

        self.assertFalse(failed)
        self.assertEqual([(r["node"], r["outcome"]) for r in results],
                         [("reopen", "reopened"), ("join", "changes")])
        self.assertEqual(tr.tickets[7]["stage"], "changes-requested")
        self.assertIn(["label", json.dumps("factory:fix-mid")], tr.tickets[7]["log"])

    def test_approved_green_unchanged_stays(self):
        tr, (results, failed) = self.sweep({"stage": "approved", "pr_state": "open", "head": "same",
                                            "round_head": "same", "ci": "green"})

        self.assertFalse(failed)
        self.assertEqual(results, [])
        self.assertEqual(tr.tickets[7]["stage"], "approved")


class AutoMergeTest(unittest.TestCase):
    OPEN = {"stage": "approved", "pr_state": "open", "head": "h", "round_head": "h", "round_complete": True,
            "round_verdict": "approve", "ci": "green", "risk": "high", "pr": 9}
    ON = {"enabled": True, "max_risk": "high"}

    def sweep(self, auto_merge=ON, **kw):
        tr = MemoryTracker({7: {**self.OPEN, **kw}})
        tr.sweep_tickets = lambda stages: [n for n, t in tr.tickets.items() if t["stage"] in stages]
        results, failed = gate.sweep(tr, NODES, auto_merge=auto_merge)
        return tr, [(r["node"], r["outcome"]) for r in results if not failed]

    def log(self, tr, kind):
        return [json.loads(v) for k, v in tr.tickets[7].get("log", []) if k == kind]

    def test_merge_when_all_gates_pass(self):
        tr, results = self.sweep()
        self.assertEqual(results, [("merge", "merged")])
        self.assertEqual(self.log(tr, "merge"), ["match-head"])

    def test_no_merge_when_head_not_reviewed(self):
        for kw in ({"round_head": "old"}, {"round_head": None}, {"round_verdict": "changes"}, {"round_complete": False}):
            with self.subTest(kw=kw):
                tr, results = self.sweep(**kw)
                self.assertNotIn(("merge", "merged"), results)
                self.assertEqual(self.log(tr, "merge"), [])

    def test_no_merge_when_ci_not_green_at_head(self):
        for ci in ("pending", "red", "none"):
            with self.subTest(ci=ci):
                tr, results = self.sweep(ci=ci)
                self.assertNotIn(("merge", "merged"), results)
                self.assertEqual(self.log(tr, "merge"), [])

    def test_behind_main_updates_branch_not_merge(self):
        tr, results = self.sweep(behind=True)
        self.assertEqual(results, [("merge", "updated")])
        self.assertEqual((self.log(tr, "merge"), self.log(tr, "update-branch")), ([], ["match-head"]))
        self.assertEqual(tr.tickets[7]["stage"], "approved")

    def test_risk_above_limit_waits_for_person(self):
        for auto_merge in ({"enabled": True, "max_risk": "low"}, {"enabled": False, "max_risk": "high"}):
            with self.subTest(auto_merge=auto_merge):
                tr, results = self.sweep(auto_merge)
                self.assertEqual(results, [("merge", "waiting")])
                self.assertEqual(self.log(tr, "merge"), [])
                self.assertEqual(tr.tickets[7]["stage"], "approved")
                self.assertIn("Merge PR #9", self.log(tr, "comment")[0])
                self.assertIn(["label", '\"factory:needs-human\"'], tr.tickets[7]["log"])
                self.assertEqual(tr.tickets[7]["assignees"], [CONFIG["notify"]["github_user"]])

    def test_merge_wait_comment_targets_pr(self):
        effects = []
        class Capture(MemoryTracker):
            def apply(self, effect):
                effects.append(effect.copy())
                return super().apply(effect)
        tr = Capture({7: dict(self.OPEN)})
        tr.sweep_tickets = lambda stages: [7]
        gate.sweep(tr, NODES, auto_merge={"enabled": False, "max_risk": "high"})
        comment = next(e for e in effects if e["kind"] == "comment")
        self.assertEqual(comment["on"], "pr")

    def test_one_merge_per_sweep(self):
        tr = MemoryTracker({7: dict(self.OPEN), 8: dict(self.OPEN)})
        tr.sweep_tickets = lambda stages: [7, 8]
        results, _ = gate.sweep(tr, NODES, auto_merge=self.ON)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in results if r["node"] == "merge"], [(7, "merged")])

    def test_apply_refuses_a_merge_whose_gate_failed(self):
        tr = MemoryTracker({7: {**self.OPEN, "ci": "red"}})
        code, out = gate.apply("merge", "merged", {"ticket": 7}, tr, NODES)
        self.assertEqual((code, out["applied"]), (gate.NO_GO, False))


class UnblockedSweepTest(unittest.TestCase):
    def sweep(self, tickets, closed=10):
        tr = MemoryTracker(tickets)
        tr.sweep_tickets = lambda stages: []
        return tr, gate.sweep(tr, NODES, closed_ticket=closed)

    def test_closing_last_blocker_refires_ready_and_does_not_use_recover(self):
        tr, (results, failed) = self.sweep({
            10: {"blocking": [11]},
            11: {"stage": "ready", "blockers_open": 0},
        })
        self.assertFalse(failed)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in results], [(11, "unblocked")])
        self.assertEqual(tr.tickets[11]["log"], [
            ["label", '"factory:ready"'],
            ["comment", '"[factory] unblocked by #10"'],
        ])
        self.assertNotIn("recover", json.dumps(tr.tickets[11]["log"]))
        tr.tickets[11]["stage"] = "in-dev"  # intake accepted the re-fire
        tr.tickets[11]["log"].clear()
        self.assertEqual(gate.sweep(tr, NODES, closed_ticket=10), ([], False))
        self.assertEqual(tr.tickets[11]["log"], [])

    def test_ready_dependent_with_another_open_blocker_is_untouched(self):
        tr, (results, failed) = self.sweep({
            10: {"blocking": [11]},
            11: {"stage": "ready", "blockers_open": 1},
        })
        self.assertFalse(failed)
        self.assertEqual(results, [])
        self.assertNotIn("log", tr.tickets[11])

    def test_dependent_outside_ready_is_untouched(self):
        tr, (results, failed) = self.sweep({
            10: {"blocking": [11]},
            11: {"stage": "in-dev", "blockers_open": 0},
        })
        self.assertFalse(failed)
        self.assertEqual(results, [])
        self.assertNotIn("log", tr.tickets[11])

    def test_merged_ticket_refires_ready_dependent_in_same_sweep(self):
        tr = MemoryTracker({
            10: {"stage": "approved", "pr_state": "merged", "blocking": [11]},
            11: {"stage": "ready", "blockers_open": 0},
        })
        tr.sweep_tickets = lambda stages: [10]
        facts = tr.facts
        tr.facts = lambda ticket: {**facts(ticket), **({"pr_state": "merged", "pr": 20,
                                                          "reviews": [], "fix_rounds": 0}
                                                         if ticket == 10 else {})}

        results, failed = gate.sweep(tr, NODES)

        self.assertFalse(failed)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in results], [(10, "merged"), (11, "unblocked")])
        self.assertIn(["comment", '"[factory] unblocked by #10"'], tr.tickets[11]["log"])

    def test_config_change_merge_updates_open_prs(self):
        class ConfigTracker(MemoryTracker):
            def config_change_merged(self, pr):
                self.updated_prs = getattr(self, "updated_prs", []) + [pr]

        tr = ConfigTracker({10: {"stage": "approved", "pr_state": "open", "head": "h", "round_head": "h",
                                 "round_complete": True, "round_verdict": "approve", "ci": "green", "risk": "low"}})
        tr.sweep_tickets = lambda stages: [10]
        facts = tr.facts
        tr.facts = lambda ticket: {**facts(ticket), "pr": 300, "reviews": [], "fix_rounds": 0}

        results, failed = gate.sweep(tr, NODES, auto_merge={"enabled": True, "max_risk": "high"})

        self.assertFalse(failed, results)
        self.assertEqual([(r["node"], r["outcome"]) for r in results], [("merge", "merged")])
        self.assertFalse(hasattr(tr, "updated_prs"))

        tr.tickets[10]["pr_state"] = "merged"
        results, failed = gate.sweep(tr, NODES)

        self.assertFalse(failed, results)
        self.assertEqual([(r["node"], r["outcome"]) for r in results], [("finish", "merged")])
        self.assertEqual(tr.updated_prs, [300])

    def test_blocking_error_does_not_fail_sweep(self):
        class BrokenBlockingTracker(MemoryTracker):
            def blocking(self, ticket):
                raise RuntimeError("tracker unavailable")

        tr = BrokenBlockingTracker({})
        tr.sweep_tickets = lambda stages: []

        results, failed = gate.sweep(tr, NODES, closed_ticket=10)

        self.assertFalse(failed)
        self.assertEqual(results, [{"ticket": 10, "error": "RuntimeError: tracker unavailable"}])

    def test_dependent_is_refired_once_when_listed_more_than_once(self):
        tr = MemoryTracker({11: {"stage": "ready", "blockers_open": 0}})
        tr.sweep_tickets = lambda stages: []
        tr.blocking = lambda ticket: [11, 11]

        results, failed = gate.sweep(tr, NODES, closed_ticket=10)

        self.assertFalse(failed)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in results], [(11, "unblocked")])


if __name__ == "__main__":
    unittest.main()

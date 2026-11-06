#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/gate.py on both fakes. Run: uv run factory/tests/test_gate.py"""
import copy
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from unittest.mock import patch
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
sys.path.insert(0, str(ROOT / "factory/tests"))
import config as cfg  # noqa: E402
import project_mode  # noqa: E402
cfg.set_team(project_mode.team(Path(__file__).resolve().parent / "fixtures/team"))
import deploy  # noqa: E402
import gate  # noqa: E402
import nodes  # noqa: E402
from gate_port import MemoryTracker, TagTracker  # noqa: E402

CONFIG = cfg.load()
NODES = nodes.load()
SENT = []
gate.notify = lambda kind, ticket, result=None, pr=None: SENT.append((kind, ticket))  # no network or gh
PAYLOAD = {"risk": "low", "complexity": "small", "tier": "mid"}


def make(cls, tid, **kw):
    t = {"head": "abc", "blockers_open": 0, "ci": "green"}  # reviewers start on green CI
    if cls is TagTracker:
        t["tags"] = ["stage:triage", "risk:low"]
    else:
        t["stage"] = "triage"
    return cls({tid: {**t, **kw}})


class GateTest(unittest.TestCase):
    def setUp(self):
        SENT.clear()

    def test_label_guard_falls_back(self):
        """ADR-0021: an outcome's `if` guard that fails applies its `else`; without one the outcome is refused."""
        graph = copy.deepcopy(NODES)
        graph["nodes"]["intake"]["outcomes"]["cheap"] = {
            "if": {"label": "risk:low"}, "else": "proceed", "effects": [{"stage": "ready"}, {"label": "factory:cheap"}]}
        high = MemoryTracker({"7": {"stage": "triage", "attrs": ["risk:high"]}})
        gate.apply("intake", "cheap", {"ticket": "7", "risk": "high", "complexity": "small"}, high, graph)
        self.assertNotIn("factory:cheap", high.facts("7")["attrs"])
        low = MemoryTracker({"7": {"stage": "triage", "attrs": ["risk:low"]}})
        gate.apply("intake", "cheap", {"ticket": "7"}, low, graph)
        self.assertIn("factory:cheap", low.facts("7")["attrs"])
        del graph["nodes"]["intake"]["outcomes"]["cheap"]["else"]
        with self.assertRaises(gate.Bad):
            gate.apply("intake", "cheap", {"ticket": "7"}, MemoryTracker({"7": {"stage": "triage"}}), graph)

    def test_guard_facts(self):
        f = {"attrs": ["risk:low"], "pr_state": "open", "behind": True, "conflict": False, "ci": "green",
             "head": "h2", "blockers_open": 0, "stage": "in-review",
             "reviews": [{"agent": True, "commit_id": "h1", "verdict": "changes"},
                         {"agent": True, "commit_id": "h2", "verdict": "approve"}]}
        for guard, want in [({"label": "risk:low"}, True), ({"label": ["risk:low", "x"]}, False),
                            ({"pr": "behind"}, True), ({"pr": "conflicting"}, False), ({"pr": "open"}, True),
                            ({"review": "approved-at-head"}, True), ({"review": "changes-at-head"}, False),
                            ({"ci": "green", "stage": "in-review"}, True), ({"blockers": "closed"}, True)]:
            self.assertEqual(gate.guard_holds(guard, f), want, guard)
        with self.assertRaises(gate.Bad):
            gate.guard_holds({"colour": "red"}, f)
        self.assertTrue(gate.guard_holds({"children": "closed"}, {**f, "children_open": 0}))

    def test_merge_effect_keeps_its_invariants(self):
        """ADR-0021: for a definition team, `merge` is refused without an approval at head, with red CI, or behind."""
        graph = copy.deepcopy(NODES)
        graph["nodes"]["intake"]["outcomes"]["ship"] = {"effects": [{"merge": "match-head"}]}
        ok = {"stage": "triage", "head": "h2", "ci": "green", "pr": 5, "pr_state": "open",
              "reviews": [{"agent": True, "commit_id": "h2", "verdict": "approve"}]}
        cases = [({}, True), ({"ci": "red"}, False), ({"behind": True}, False),
                 ({"reviews": [{"agent": True, "commit_id": "h1", "verdict": "approve"}]}, False),   # an older head
                 ({"reviews": [{"agent": True, "commit_id": "h2", "verdict": "approve"},
                               {"agent": True, "commit_id": "h2", "verdict": "changes"}]}, False)]
        with patch("gate.definition_team", return_value=True):
            for change, applied in cases:
                tr = MemoryTracker({"7": {**ok, **change}})
                status, out = gate.apply("intake", "ship", {"ticket": "7"}, tr, graph)
                self.assertEqual(out["applied"], applied, (change, out))
                if not applied:
                    self.assertTrue(out["reason"].startswith("merge refused"), out)

    def test_decide_applies_the_first_outcome_whose_guards_hold(self):
        graph = copy.deepcopy(NODES)
        graph["nodes"]["finisher"] = {"role": "clerk", "when": {"always": True}, "triggers": ["manual"], "outcomes": {
            "done": {"if": {"pr": "merged"}, "effects": [{"stage": "done"}]},
            "children-done": {"from": ["split"], "if": {"children": "closed"}, "effects": [{"stage": "done"}]},
            "waiting": {}}}
        merged = MemoryTracker({"7": {"stage": "in-review", "pr_state": "merged"}})
        status, out = gate.decide("finisher", "7", merged, graph)
        self.assertEqual((out["outcome"], merged.get_stage(merged._t("7"))), ("done", "done"))
        split = MemoryTracker({"8": {"stage": "split", "children": [{"state": "closed"}]}})
        split.children = lambda n: [{"state": "closed"}]
        self.assertEqual(gate.decide("finisher", "8", split, graph)[1]["outcome"], "children-done")
        idle = MemoryTracker({"9": {"stage": "in-review"}})
        self.assertEqual(gate.decide("finisher", "9", idle, graph)[1]["outcome"], "waiting")
        del graph["nodes"]["finisher"]["outcomes"]["waiting"]
        status, out = gate.decide("finisher", "9", idle, graph)
        self.assertEqual((status, out["outcome"]), (gate.NO_GO, None))

    def test_reaching_a_final_stage_wakes_unblocked_dependents(self):
        """ADR-0021: automatic, no effect in the workflow: done re-adds `ready` to each fully unblocked dependent."""
        graph = copy.deepcopy(NODES)
        graph["nodes"]["intake"]["outcomes"]["closing"] = {"effects": [{"stage": "done"}]}
        tr = MemoryTracker({"7": {"stage": "triage"}, "8": {"stage": "ready", "blockers_open": 1},
                            "9": {"stage": "ready", "blockers_open": 2}, "10": {"stage": "in-dev", "blockers_open": 1}})
        tr.blocking = lambda n: [{"number": "8"}, {"number": "9"}, {"number": "10"}] if str(n) == "7" else []
        sent = []
        apply = tr.apply
        tr.apply = lambda e: sent.append(e) or apply(e)
        with patch("gate.definition_team", return_value=True):
            gate.apply("intake", "closing", {"ticket": "7"}, tr, graph)
        woken = [(e["ticket"], e["kind"]) for e in sent if e["ticket"] != "7"]
        self.assertEqual(woken, [("8", "unlabel"), ("8", "label")])   # 9 has another blocker; 10 is not ready

    def test_ci_state_waits_until_ci_concludes(self):
        states = iter(["pending", "pending", "red"])
        tr = MemoryTracker({"7": {"stage": "in-review", "head": "h1", "ci": "pending"}})
        facts = tr.facts
        tr.facts = lambda n: {**facts(n), "ci": next(states)}
        tr.ci_failure = lambda head: "lint: E501"
        naps = []
        out = gate.ci_state(tr, "7", wait_seconds=600, poll=30, sleep=naps.append)
        self.assertEqual((out["ci"], out["failing"], len(naps)), ("red", "lint: E501", 2))
        self.assertEqual(gate.ci_state(MemoryTracker({"7": {"ci": "pending"}}), "7")["ci"], "pending")   # no wait

    def test_label_group_holds_one_value(self):
        graph = copy.deepcopy(NODES)
        graph["nodes"]["intake"]["outcomes"]["risky"] = {"effects": [{"label": "risk:high"}, {"stage": "ready"}]}
        tr = MemoryTracker({"7": {"stage": "triage", "attrs": ["risk:low", "complexity:small"]}})
        gate.apply("intake", "risky", {"ticket": "7"}, tr, graph)
        attrs = tr.facts("7")["attrs"]
        self.assertIn("risk:high", attrs)
        self.assertNotIn("risk:low", attrs)
        self.assertIn("complexity:small", attrs)   # another group stays

    def test_lessons_comment_and_label(self):
        tr = MemoryTracker({"7": {"stage": "triage"}})
        gate.apply("intake", "not-actionable", {"ticket": "7", "reason": "[factory] no", "lessons": [
            {"text": "The router  misses ADRs", "scope": "factory", "about": "router", "evidence": "#7"}]}, tr, NODES)
        log = tr.tickets["7"]["log"]
        lesson = [json.loads(v) for k, v in log if k == "comment" and "factory:lesson" in v]
        self.assertEqual(lesson, ["[factory] Lessons from `intake`\n<!-- factory:lesson -->\n"
                                  "- [factory] router: The router misses ADRs (evidence: #7)"])
        self.assertIn("factory:lesson", tr.facts("7")["attrs"])
        with self.assertRaises(gate.Bad):
            gate.apply("intake", "not-actionable", {"ticket": "7", "reason": "r", "lessons": ["just text"]},
                       MemoryTracker({"7": {"stage": "triage"}}), NODES)

    def test_blocked_sends_one_decision_needed(self):
        gate.apply("implement", "blocked", {"ticket": "7", "reason": "[factory] blocked"}, MemoryTracker({"7": {"stage": "in-dev"}}), NODES)
        self.assertEqual(SENT, [("automation-error", "7"), ("decision-needed", "7")])  # both: error and entering blocked
        SENT.clear()
        gate.apply("intake", "grill", {"ticket": "7", "questions": "[factory] Which API?"}, MemoryTracker({"7": {"stage": "triage"}}), NODES)
        self.assertEqual(SENT, [("decision-needed", "7")])

    def test_join_waiting_with_reviewers_sends_one_review_needed(self):
        tr = MemoryTracker({"7": {"stage": "in-review", "head": "h", "pr": 42, "ci": "green",
                                  "waiting_reviewers": ["rv"], "focus_missing": []}})
        gate.apply("join", "waiting", {"ticket": "7", "event": "e"}, tr, NODES)
        self.assertEqual(SENT, [("review-needed", "7")])

    def test_implement_failed_sends_automation_error_but_error_sends_nothing(self):
        gate.apply("implement", "failed", {"ticket": "7", "reason": "[factory] x"}, MemoryTracker({"7": {"stage": "in-dev"}}), NODES)
        self.assertEqual(SENT, [("automation-error", "7")])
        SENT.clear()
        gate.apply("implement", "error", {"ticket": "7", "tier": "mid"}, MemoryTracker({"7": {"stage": "in-dev"}}), NODES)
        self.assertEqual(SENT, [])

    def test_dry_run_sends_nothing(self):
        tr = MemoryTracker({7: {"stage": "ready", "since": "2026-11-01T00:00:00Z"}})
        tr.sweep_tickets = lambda stages: [7]
        gate.sweep_stale(tr, NODES, 2, datetime(2026, 11, 1, 12, tzinfo=timezone.utc), dry_run=True)
        gate.sweep(tr, NODES, dry_run=True)
        self.assertEqual(SENT, [])

    def test_plan_change_opens_deploy_ticket(self):
        class PlanTracker(MemoryTracker):
            def config_change_merged(self, pr):
                self.deploy_ticket = {"title": "Deploy needed", "pr": pr}
        tr = PlanTracker({10: {"stage": "approved", "pr_state": "merged", "pr": 300}})
        tr.sweep_tickets = lambda stages: [10]
        facts = tr.facts
        tr.facts = lambda ticket: {**facts(ticket), "reviews": [], "fix_rounds": 0}
        gate.sweep(tr, NODES)
        self.assertEqual(tr.deploy_ticket, {"title": "Deploy needed", "pr": 300})

    def test_failed_plan_check_still_moves_the_parent(self):
        class PlanTracker(MemoryTracker):
            def config_change_merged(self, pr):
                raise RuntimeError("git worktree add failed")
        tr = PlanTracker({10: {"stage": "approved", "pr_state": "merged", "pr": 300}, 20: {"stage": "split"}})
        tr.sweep_tickets = lambda stages: [10]
        tr.parent = lambda n: 20 if n == 10 else None
        seen = []
        facts = tr.facts
        tr.facts = lambda ticket: (seen.append(ticket), {**facts(ticket), "reviews": [], "fix_rounds": 0})[1]
        results, failed = gate.sweep(tr, NODES, issue=10)
        self.assertTrue(failed)
        self.assertEqual(tr.tickets[10]["stage"], "done")
        self.assertIn(20, seen)  # the parent was still read after the failed check
        self.assertTrue(any("deploy check" in r.get("error", "") for r in results))

    def test_unchanged_plan_opens_nothing(self):
        class PlanTracker(MemoryTracker):
            def config_change_merged(self, pr):
                self.deploy_ticket = None
        tr = PlanTracker({10: {"stage": "approved", "pr_state": "merged", "pr": 300}})
        tr.sweep_tickets = lambda stages: [10]
        facts = tr.facts
        tr.facts = lambda ticket: {**facts(ticket), "reviews": [], "fix_rounds": 0}
        gate.sweep(tr, NODES)
        self.assertIsNone(tr.deploy_ticket)

    def test_new_labels_created_at_merge(self):
        plan = deploy.plan_air(NODES, CONFIG)
        self.assertIn("factory:needs-human", plan["labels"])

    def test_low_risk_wakes_full_only(self):
        facts = {"stage": "in-review", "ci": "green", "focus_missing": ["full"]}
        self.assertEqual(gate.review_wake_labels(facts), ["factory:review-full"])

    def test_ci_completion_wakes_matching_head_while_workflow_is_still_running(self):
        with tempfile.TemporaryDirectory() as d:
            tracker = Path(d, "tickets.json")
            tracker.write_text(json.dumps({"7": {"stage": "in-review", "ci": "pending", "head": "abc",
                                                   "focus_missing": ["full"]}}))
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(gate.main(["wake-reviewers", "--issue", "7", "--completed-head", "abc",
                                            "--tracker", f"memory:{tracker}"]), 0)
            self.assertEqual(json.loads(out.getvalue())["labels"], ["factory:review-full"])
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(gate.main(["wake-reviewers", "--issue", "7", "--completed-head", "stale",
                                            "--tracker", f"memory:{tracker}"]), 0)
            self.assertEqual(json.loads(out.getvalue())["labels"], [])

    def test_high_risk_wakes_two_focuses(self):
        facts = {"stage": "in-review", "ci": "green", "focus_missing": ["behaviour", "structure-security"]}
        self.assertEqual(gate.review_wake_labels(facts),
                         ["factory:review-behaviour", "factory:review-structure-security"])

    def test_reviewed_focus_not_woken_again(self):
        facts = {"stage": "in-review", "ci": "green", "focus_missing": ["structure-security"]}
        self.assertEqual(gate.review_wake_labels(facts), ["factory:review-structure-security"])

    def test_review_check_reads_rereview_focus_from_tracker_facts(self):
        tracker = MemoryTracker({"7": {
            "stage": "in-review", "head": "abc", "ci": "green",
            "focus_missing": [], "pending_rereviews": ["behaviour"],
        }})
        event = {"id": "pull_request:7:labeled", "ticket": 7,
                 "event_name": "pull_request", "action": "labeled", "focus": "behaviour"}

        code, result = gate.check("review", event, tracker, NODES, CONFIG)

        self.assertEqual(code, gate.GO)
        self.assertTrue(result["go"])

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
            self.assertEqual(Path(out["role_file"]).resolve(), cfg.resolve("roles/router/role.md").resolve())
            self.assertIn("grill", out["outcomes"])

        def test_condition_fails(self):
            tr = self.tr()
            tr.set_stage(tr.tickets[tid], "in-dev")
            self.assertEqual(self.check(tr, self.ev())[0], 10)

        def test_stale_head(self):
            code, out = self.check(self.tr(), self.ev(head="old"))
            self.assertEqual((code, out["reason"]), (10, "stale head"))

        def test_duplicate_event(self):
            tr = self.tr()
            self.assertEqual(self.apply(tr)[0], 0)
            tr.set_stage(tr.tickets[tid], "triage")  # a person re-triages the ticket; same event id
            self.assertEqual(self.check(tr, self.ev())[1]["reason"], "duplicate event")
            self.assertEqual(self.check(tr, self.ev())[0], 10)

        def test_duplicate_event_on_apply(self):
            tr = self.tr()
            self.assertEqual(self.apply(tr)[0], 0)
            tr.set_stage(tr.tickets[tid], "triage")
            code, out = self.apply(tr)
            self.assertEqual((code, out["reason"]), (10, "duplicate event"))
            self.assertEqual(tr.facts(tid)["stage"], "triage")

        def test_duplicate_event_without_stage_effect(self):
            tr = self.tr(focus_missing=["full"])
            tr.set_stage(tr.tickets[tid], "in-review")
            pl = {"ticket": tid, "event": "r1", "head": "abc"}
            self.assertEqual(gate.apply("review", "approve", pl, tr, NODES, focus="full")[0], 0)
            code, out = gate.apply("review", "approve", pl, tr, NODES, focus="full")
            self.assertEqual((code, out["reason"]), (10, "duplicate event"))
            self.assertEqual(gate.check("review", {"id": "r1", "ticket": tid}, tr, NODES, CONFIG)[0], 10)

        def test_apply_twice_is_noop(self):
            tr = self.tr()
            self.assertEqual(self.apply(tr)[0], 0)
            after = copy.deepcopy(tr.tickets)
            self.assertEqual(self.apply(tr)[0], 10)
            self.assertEqual(tr.tickets, after)
            self.assertEqual(tr.facts(tid)["stage"], "ready")

        def test_effects_skip_when_state_holds(self):
            tr = self.tr()
            effects = [{"kind": "label", "ticket": tid, "value": "x"}, {"kind": "set-field", "ticket": tid, "value": {"a": 1}}]
            self.assertEqual([tr.apply(e) for e in effects + effects], [True, True, False, False])

        def test_failing_effect_leaves_stage(self):
            tr = self.tr()
            tr.fail_on = "set-field"
            with self.assertRaises(RuntimeError):
                self.apply(tr)
            self.assertEqual(tr.facts(tid)["stage"], "triage")
            tr.fail_on = None
            self.assertEqual(self.apply(tr)[0], 0)  # a retry completes
            self.assertEqual(tr.facts(tid)["stage"], "ready")

        def test_bad_payload_names_field(self):
            tr = self.tr()
            with self.assertRaisesRegex(gate.Bad, "`complexity`"):
                self.apply(tr, {"risk": "low", "tier": "mid"})
            self.assertEqual(tr.facts(tid)["stage"], "triage")

        def test_outcome_from(self):
            tr = self.tr()
            code, _ = gate.apply("human", "triage", {"ticket": tid, "actor": "human"}, tr, NODES)
            self.assertEqual(code, 10)  # stage triage is not in the outcome's from

        def test_unknown_node(self):
            with self.assertRaises(gate.Bad):
                gate.check("nope", self.ev(), self.tr(), NODES, CONFIG)

    T.__name__ = f"Gate{cls.__name__}"
    return T


GateMemory = tests_for(MemoryTracker, 7)
GateTag = tests_for(TagTracker, "PROJ-7")


class YouTrackGateTest(unittest.TestCase):
    """One implement step on LME-1701-shaped stub data (fixtures/project-youtrack): the ticket in a stubbed YouTrack,
    its state a `field` store."""
    FIXTURE = Path(__file__).resolve().parent / "fixtures/project-youtrack"

    def setUp(self):
        import project
        from gate_github import GitHubGateTracker
        from test_youtrack_tracker import FakeYouTrack
        from youtrack_tracker import YouTrackTicketStore
        self.addCleanup(cfg.set_team, cfg.team_root())  # importing the stub set a team of its own
        cfg.set_team(self.FIXTURE)
        self.fake = FakeYouTrack(json.loads((self.FIXTURE / "issue.json").read_text()), tags=("factory:implement-mid", "factory:needs-human"))
        descriptor, self.config, self.nodes = project.load(self.FIXTURE), cfg.load(), nodes.load()
        store = YouTrackTicketStore(self.fake, state_field="State")
        self.tracker = GitHubGateTracker("example/project", self.config["tracker"]["stages"], "factory[bot]",
                                         run=lambda *a, **k: "", project=descriptor, tickets=store)

    def test_check_then_apply_blocked_moves_the_state_field_and_comments(self):
        code, out = gate.check("implement", {"id": "e1", "ticket": "LME-1701"}, self.tracker, self.nodes, self.config)
        self.assertEqual((code, out["context"]["stage"]), (gate.GO, "in-dev"))
        code, _ = gate.apply("implement", "blocked", {"ticket": "LME-1701", "event": "e1", "reason": "[factory] stuck"},
                             self.tracker, self.nodes)
        self.assertEqual(code, gate.GO)
        self.assertEqual(self.tracker.facts("LME-1701")["stage"], "blocked")
        self.assertIn(("POST", "/api/issues/LME-1701/comments?fields=id", {"text": unittest.mock.ANY}), self.fake.calls)


class CliAndValidate(unittest.TestCase):
    def test_opaque_ticket_id_in_branch_and_title(self):
        class Host:  # the code host knows PRs, the tracker knows tickets
            def ticket_for_pr(self, number):
                return gate.ticket_from_ref("ez/INFRA-123-add-thing")

        tracker = TagTracker({"INFRA-123": {"tags": ["stage:in-dev"], "title": "Add the thing"}})
        branch = gate.branch_of({"ticket": "INFRA-123", "title": "Add the thing"}, CONFIG)
        self.assertEqual(branch, "ez/INFRA-123-add-thing")
        self.assertEqual(gate.ticket_from_ref(branch), "INFRA-123")
        self.assertEqual(gate.ticket_from_ref("INFRA-123: Add the thing"), "INFRA-123")
        self.assertEqual((gate.ticket_from_ref("ez/7-x"), gate.ticket_from_ref("42-fix"), gate.ticket_from_ref("main")), (7, None, None))
        ev = gate.from_trigger({"eventType": "pull_request.opened", "data": {"action": "opened", "pull_request": {"number": 9}}}, Host())
        self.assertEqual(ev["ticket"], "INFRA-123")
        self.assertEqual(tracker.facts("INFRA-123")["stage"], "in-dev")
        code, out = gate.check("implement", ev, tracker, NODES, CONFIG)  # the gate runs on both ports
        self.assertEqual(code, gate.GO)
        self.assertEqual(out["ticket"], "INFRA-123")

    def test_github_code_host_resolves_ticket_from_head_ref_then_title(self):
        calls = []

        def run(args):
            calls.append(args)
            return json.dumps({"head": {"ref": "feature/x"}, "title": "INFRA-9 Add thing"})

        host = gate.GitHubCodeHost("org/code", run)
        self.assertEqual(host.ticket_for_pr(4), "INFRA-9")
        self.assertEqual(calls, [["api", "repos/org/code/pulls/4"]])

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

    def _done(self, **facts):
        tr = MemoryTracker({"7": {"stage": "changes-requested", "pr": 9, "head": "abc", "ci": "green",
                                   "round_complete": True, "focus_missing": ["full"], **facts}})
        with patch.object(gate, "local_head", return_value="abc"):
            gate.apply("fix", "done", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES)
        return tr.tickets["7"]

    def test_done_after_green_ci_wakes_reviewers(self):
        t = self._done()
        self.assertEqual(t["stage"], "in-review")
        self.assertIn(["label", json.dumps("factory:review-full")], t["log"])

    def test_done_before_ci_or_after_wake_adds_no_review_label(self):
        for facts in ({"ci": "pending"}, {"pr_labels": ["factory:review-full"]}):
            log = self._done(**facts).get("log", [])
            self.assertNotIn(("label", json.dumps("factory:review-full")), [tuple(x) for x in log])

    def test_green_ci_after_done_wakes_reviewers_once(self):
        facts = {"stage": "in-review", "ci": "green", "head": "abc", "focus_missing": ["full"]}
        self.assertEqual(gate.review_wake_labels(facts, "abc"), ["factory:review-full"])  # CI last: wakes it
        t = self._done(ci="pending")  # done first: no wake yet, so the CI step is the only one
        self.assertNotIn(("label", json.dumps("factory:review-full")), [tuple(x) for x in t.get("log", [])])

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
                "stdout": "5\t7\tretained.txt\n0\t9\tdeleted.txt\n4\t0\tengine/template-v1/skills/copy.txt\n",
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
                          "9\t0\tengine/template-v1/roles/router/role.md\n"
                          "4\t1\tfactory/gate.py\n",
                "stderr": "",
            })()
            with patch.object(gate.subprocess, "run", return_value=result):
                self.assertEqual(gate.branch_size(
                    "origin/main", checkout=delivery,
                    generated_paths=["factory/tests/fixtures/deploy/air-plan.json",
                                     "docs/statechart.md", "docs/statechart-proposed.md",
                                     "skills/factory-setup/templates/**", "engine/template-v1/roles/**"],
                ), (5, [("factory/gate.py", 5)]))

    def test_intake_size_uses_first_number(self):
        fields = gate.intake_fields({
            "size_estimate": "about 40 lines changed; deletions about 2,200 lines",
        })
        self.assertEqual((fields["complexity"], fields["tier"]), ("small", "cheap"))

    def test_routines_run_without_a_ticket(self):
        for name in ("retro", "distill"):
            with tempfile.TemporaryDirectory() as d:
                d = Path(d)
                (d / "ev.json").write_text(json.dumps({"source": "schedule", "schedule": name}))
                (d / "h.json").write_text(json.dumps({"status": "done"}))
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
            (d / "t.json").write_text(json.dumps({"7": {"stage": "triage", "head": "abc"}}))
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
        want = {"issues": 7, "pull_request": 7, "pull_request_review": 7}
        for name, payload in TRIGGERS.items():
            ev = gate.read(self.file(payload), "event", raw=True)
            self.assertEqual(ev["ticket"], want[name], name)
            self.assertNotIn("head", ev)

    def test_begin_reads_head_from_port_not_payload(self):
        tracker = MemoryTracker({7: {"stage": "in-review", "head": "trusted", "risk": "low", "ci": "green",
                                     "pr": 9, "pr_state": "open", "focus_missing": ["full"]}})
        payload = {"eventType": "pull_request.labeled", "data": {"action": "labeled", "number": 9,
                                                                      "head": {"sha": "untrusted"}}}
        path = self.file(payload)
        host = type("Host", (), {"ticket_for_pr": lambda self, number: 7})()
        with patch.object(gate.GitHubCodeHost, "from_config", return_value=host), \
                patch.object(gate.GitHubGateTracker, "from_config", return_value=tracker):
            code, out = self.run_main(["check", "--node", "review", "--event", str(path)])
        self.assertEqual(code, gate.GO)
        self.assertTrue(out["go"])
        self.assertEqual(out["context"]["head"], "trusted")

    def test_begin_without_number_exits_11(self):
        path = self.file({"eventType": "issues.labeled", "data": {"action": "labeled"}})
        code, out = self.run_main(["check", "--node", "review", "--event", str(path),
                                   "--tracker", "memory:" + str(self.file({}))])
        self.assertEqual(code, 11)
        self.assertEqual(out, {"go": False, "why": "payload has no issue or PR number"})

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

    def run_sweep(self, args, tracker, stderr=None, host=None):
        import contextlib
        import io
        from unittest.mock import patch
        stdout = io.StringIO()
        stderr = stderr or io.StringIO()
        with patch.object(gate.GitHubGateTracker, "from_config", return_value=tracker), \
                patch.object(gate.GitHubCodeHost, "from_config", return_value=host), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = gate.main(["sweep", *args])
        return code, json.loads(stdout.getvalue().splitlines()[0]), stderr.getvalue()

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
            self.assertEqual(code, gate.BAD)
            self.assertIn("invalid sweep event", out["error"])
        finally:
            f.unlink(missing_ok=True)

    def test_sweep_non_event_object_warns_and_runs_full_sweep(self):
        from gate_port import MemoryTracker
        tracker = MemoryTracker({})
        tracker.sweep_tickets = lambda stages: []
        f = self.file({"action": "closed"})
        code, out, warning = self.run_sweep(["--event", str(f)], tracker)
        self.assertEqual(code, gate.BAD)
        self.assertIn("invalid sweep event", out["error"])

    def test_sweep_event_resolves_ticket_from_tracker_pr_head(self):
        from gate_port import MemoryTracker
        tracker = MemoryTracker({335: {"stage": "ready"}})
        tracker.sweep_tickets = lambda stages: self.fail("event sweep must not list all tickets")
        host = type("Host", (), {"ticket_for_pr": lambda self, number: 335})()
        f = self.file({"eventType": "pull_request.labeled", "data": {"action": "labeled", "number": 44,
                                                                         "title": "untrusted"}})
        code, out, _ = self.run_sweep(["--event", str(f), "--dry-run"], tracker, host=host)
        self.assertEqual(code, gate.GO)
        self.assertEqual([x["ticket"] for x in out["sweep"]], [335])

    def test_sweep_without_scope_is_rejected_except_stale(self):
        from gate_port import MemoryTracker
        tracker = MemoryTracker({})
        tracker.sweep_tickets = lambda stages: self.fail("unscoped sweep must be rejected")
        code, out, _ = self.run_sweep([], tracker)
        self.assertEqual(code, gate.BAD)
        self.assertIn("needs --issue or --event", out["error"])

    def test_sweep_reports_skipped_ticket_as_progress_without_changing_result(self):
        from gate_port import MemoryTracker
        tracker = MemoryTracker({1: {"stage": "ready", "blockers_open": 1}})
        tracker.sweep_tickets = lambda stages: [1]
        code, out, progress = self.run_sweep(["--issue", "1"], tracker)
        self.assertEqual((code, out["sweep"]), (gate.GO, []))
        self.assertEqual(json.loads(progress), {"ticket": 1, "stage": "ready", "outcome": "skipped"})

    def test_sweep_reports_ineligible_dependent_as_progress(self):
        from gate_port import MemoryTracker
        tracker = MemoryTracker({10: {"blocking": [11]}, 11: {"stage": "in-dev"}})
        tracker.sweep_tickets = lambda stages: []
        code, out, progress = self.run_sweep(["--issue", "10"], tracker)
        self.assertEqual((code, out["sweep"]), (gate.GO, []))
        self.assertEqual(json.loads(progress.splitlines()[-1]), {"ticket": 11, "stage": "in-dev", "outcome": "skipped"})

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
        tr = self.file({"7": {"stage": "triage", "head": "abc", "title": "Add the widget thing"}})
        code, out = self.run_main(["check", "--node", "intake", "--event", str(self.file(TRIGGERS["issues"])), "--tracker", f"memory:{tr}"])
        self.assertEqual((out["branch"], out["handoff_path"]), ("ez/7-add-widget-thing", ".factory/handoff/intake.json"))
        tr = self.file({"7": {"stage": "triage", "head": "abc", "title": "t", "branch": "ez/7-x"}})
        code, out = self.run_main(["check", "--node", "intake", "--event", str(self.file(TRIGGERS["pull_request"])), "--tracker", f"memory:{tr}"])
        self.assertEqual((code, out["branch"]), (0, "ez/7-x"))

    def test_apply_handoff_router_intake(self):
        h = {"outcome": "proceed", "risk": "high", "size_estimate": "~250"}
        tr = self.file({"7": {"stage": "triage", "head": "abc"}})
        code, out = self.run_main(["apply", "--node", "intake", "--handoff", str(self.file(h)),
                                   "--event", str(self.file(TRIGGERS["issues"])), "--tracker", f"memory:{tr}"])
        t = json.loads(tr.read_text())["7"]
        self.assertEqual((code, t["stage"]), (0, "ready"))
        self.assertEqual(t["fields"]["risk"], "high")
        self.assertEqual(t["fields"]["complexity"], "medium")

    def test_check_on_recorded_payload_and_other_handoffs(self):
        tr = MemoryTracker({7: {"stage": "triage", "head": "abc", "title": "t"}})
        for payload in TRIGGERS.values():
            ev = gate.read(self.file(payload), "event", raw=True)
            code, out = gate.check("intake", {**ev, "head": None}, tr, NODES, CONFIG)
            self.assertEqual((code, out["ticket"], out["branch"]), (0, 7, "ez/7-t"))
        grill = {"outcome": "grill", "reason": "acceptance check 2", "risk": "low", "size_estimate": "~120"}
        tr = self.file({"7": {"stage": "triage", "head": "abc"}})
        ev = str(self.file(TRIGGERS["issues"]))
        code, _ = self.run_main(["apply", "--node", "intake", "--handoff", str(self.file(grill)), "--event", ev, "--tracker", f"memory:{tr}"])
        self.assertEqual((code, json.loads(tr.read_text())["7"]["stage"]), (0, "needs-answers"))
        split = {**grill, "outcome": "split", "slices": "\n  - First — scope (~100)\n  - Second — more (~90)"}
        tr = self.file({"7": {"stage": "triage", "head": "abc"}})
        code, _ = self.run_main(["apply", "--node", "intake", "--handoff", str(self.file(split)), "--event", ev, "--tracker", f"memory:{tr}"])
        self.assertEqual((code, json.loads(tr.read_text())["7"]["stage"]), (0, "split"))
        self.assertEqual([c["title"] for c in gate.intake_fields(split)["children"]], ["First", "Second"])
        objs = [{"title": "One", "body": "scope\nchecks"}, {"title": "Two"}]
        self.assertEqual(gate.slices(objs), [{"title": "One", "body": "scope\nchecks"}, {"title": "Two", "body": "Two"}])
        tr = self.file({"7": {"stage": "in-dev", "head": "abc"}})
        blocked = {"outcome": "blocked", "reason": "ambiguous acceptance"}
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
        tr = MemoryTracker({"7": {"stage": "triage", "author": "ticket-author"}})
        gate.apply("intake", "grill", {"ticket": "7", "questions": "[factory] Which API?"}, tr, NODES)
        self.assertIn(["label", '"factory:needs-human"'], tr.tickets["7"]["log"])
        self.assertEqual(tr.tickets["7"]["assignees"], ["ticket-author"])

    def test_label_removed_when_stage_changes(self):
        tr = MemoryTracker({"7": {"stage": "blocked", "attrs": ["factory:needs-human"],
                                   "assignees": [CONFIG["notify"]["github_user"]]}})
        gate.apply("human", "ready", {"ticket": "7", "actor": "human"}, tr, NODES)
        self.assertNotIn("factory:needs-human", tr.tickets["7"]["attrs"])
        self.assertEqual(tr.tickets["7"]["assignees"], [])

    def test_blocked_ticket_marks_its_pr(self):
        effects = []
        class Capture(MemoryTracker):
            def apply(self, effect):
                effects.append(effect.copy())
                return super().apply(effect)
        tr = Capture({"7": {"stage": "in-dev", "pr": 42}})
        gate.apply("implement", "blocked", {"ticket": "7", "reason": "[factory] blocked"}, tr, NODES)
        self.assertIn({"kind": "label", "ticket": "7", "value": "factory:needs-human", "on": "pr"}, effects)
        self.assertIn({"kind": "assign", "ticket": "7", "value": CONFIG["notify"]["github_user"], "on": "pr"}, effects)

    def test_resume_clears_pr_mark(self):
        effects = []
        class Capture(MemoryTracker):
            def apply(self, effect):
                effects.append(effect.copy())
                return super().apply(effect)
        tr = Capture({"7": {"stage": "blocked", "attrs": ["factory:needs-human"], "pr": 42}})
        gate.apply("human", "ready", {"ticket": "7", "actor": "human"}, tr, NODES)
        self.assertIn({"kind": "unlabel", "ticket": "7", "value": "factory:needs-human", "on": "pr"}, effects)
        self.assertIn({"kind": "unassign", "ticket": "7", "value": None, "on": "pr"}, effects)

    def test_human_changes_request_marks_pr(self):
        effects = []
        class Capture(MemoryTracker):
            def apply(self, effect):
                effects.append(effect.copy())
                return super().apply(effect)
        tr = Capture({"7": {"stage": "in-review", "head": "abc", "pr": 42, "ci": "green",
                             "round_complete": True, "round_verdict": "changes",
                             "reviews": [{"author": "reviewer", "agent": False, "verdict": "changes", "commit_id": "abc"}]}})
        gate.apply("join", "changes", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES)
        self.assertIn({"kind": "label", "ticket": "7", "value": "factory:needs-human", "on": "pr"}, effects)
        self.assertIn({"kind": "assign", "ticket": "7", "value": ["reviewer"], "on": "pr"}, effects)

    def test_held_approved_marks_pr(self):
        effects = []
        class Capture(MemoryTracker):
            def apply(self, effect):
                effects.append(effect.copy())
                return super().apply(effect)
        tr = Capture({"7": {"stage": "approved", "head": "abc", "pr": 42, "pr_state": "open",
                             "ci": "green", "risk": "low", "round_complete": True,
                             "round_verdict": "approve", "round_head": "abc"}})
        gate.apply("merge", "waiting", {"ticket": "7", "reason": "[factory] merge"}, tr, NODES,
                   auto_merge={"enabled": False, "max_risk": "high"})
        self.assertIn({"kind": "label", "ticket": "7", "value": "factory:needs-human", "on": "pr"}, effects)
        self.assertIn({"kind": "assign", "ticket": "7", "value": CONFIG["notify"]["github_user"], "on": "pr"}, effects)

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

    def test_join_tier_from_complexity_label(self):
        facts = {"round_complete": True, "round_verdict": "changes", "attrs": ["complexity:large"]}
        self.assertEqual(gate.join_decision(facts), ("changes", {"tier": "strong"}))
        self.assertEqual(gate.join_decision({**facts, "attrs": []})[1], {"tier": "mid"})
        self.assertIsNone(gate.join_decision({**facts, "round_complete": False}))


class TriageReadyTest(unittest.TestCase):
    """`factory:triage` runs the router; `factory:ready` starts a developer (the `start` script node)."""

    def sweep(self, ticket, default_tier="mid"):
        tr = MemoryTracker({7: ticket})
        tr.sweep_tickets = lambda stages: [7]
        return tr, gate.sweep(tr, NODES, default_tier=default_tier)[0]

    def test_triage_proceed_sets_ready_and_complexity(self):
        tr = MemoryTracker({7: {"stage": "triage"}})
        code, _ = gate.apply("intake", "proceed", {"ticket": 7, "risk": "low", "complexity": "large"}, tr, NODES)
        self.assertEqual((code, tr.tickets[7]["stage"]), (gate.GO, "ready"))
        self.assertEqual(tr.tickets[7]["fields"], {"risk": "low", "complexity": "large"})
        self.assertNotIn("factory:implement-strong", json.dumps(tr.tickets[7].get("log", [])))  # `start` adds it

    def test_ready_starts_implement_at_complexity_tier(self):
        for attrs, tier in ((["complexity:small"], "cheap"), (["complexity:large"], "strong"), ([], "mid")):
            with self.subTest(attrs=attrs):
                tr, res = self.sweep({"stage": "ready", "attrs": attrs})
                self.assertEqual([(r["node"], r["outcome"]) for r in res], [("start", "started")])
                self.assertEqual(tr.tickets[7]["stage"], "in-dev")
                self.assertIn(["label", json.dumps(f"factory:implement-{tier}")], tr.tickets[7]["log"])
        tr, _ = self.sweep({"stage": "ready"}, default_tier="cheap")  # no complexity label: descriptor default
        self.assertIn(["label", '"factory:implement-cheap"'], tr.tickets[7]["log"])

    def test_ready_with_open_blocker_waits(self):
        tr, res = self.sweep({"stage": "ready", "blockers_open": 1})
        self.assertEqual(res, [])
        self.assertEqual(tr.tickets[7]["stage"], "ready")
        self.assertNotIn("log", tr.tickets[7])
        self.assertEqual(gate.check("start", {"id": "e", "ticket": 7}, tr, NODES, CONFIG)[0], gate.NO_GO)

    def test_split_children_are_ready_in_order(self):
        tr = MemoryTracker({7: {"stage": "triage"}})
        h = {"outcome": "split", "size_estimate": "~900",
             "slices": [{"title": "First"}, {"title": "Second"}]}
        self.assertEqual(gate.outcome_of(h), "split")
        code, _ = gate.apply("intake", "split", {"ticket": 7, **gate.intake_fields(h)}, tr, NODES)
        self.assertEqual((code, tr.tickets[7]["stage"]), (gate.GO, "split"))
        kids = json.loads(next(v for k, v in tr.tickets[7]["log"] if k == "create-issues"))
        self.assertEqual([c["title"] for c in kids], ["First", "Second"])  # GitHubGateTracker creates them `factory:ready`

    def test_stale_triage_is_refired(self):
        tr = MemoryTracker({1: {"stage": "triage", "since": "2026-11-01T08:00:00Z"}})
        tr.sweep_tickets = lambda stages: [1] if "triage" in stages else []
        now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
        res = gate.sweep_stale(tr, NODES, 2, now)[0]
        self.assertEqual([(r["recover"], r["stage"], r["why"]) for r in res], [("refire", "triage", "no intake outcome")])
        self.assertIn(["label", '"factory:triage"'], tr.tickets[1]["log"])
        self.assertEqual([r["recover"] for r in gate.sweep_stale(tr, NODES, 2, now)[0]], ["blocked"])


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

    def test_stale_sweep_reports_fresh_ticket_as_progress_without_changing_result(self):
        class Tr(MemoryTracker):
            def sweep_tickets(self, stages):
                return [1]
        tr = Tr({1: {"stage": "ready", "since": self.FRESH}})
        progress = []
        results, failed = gate.sweep_stale(tr, NODES, 2, self.NOW, progress=progress.append)
        self.assertFalse(failed)
        self.assertEqual(results, [])
        self.assertEqual(progress, [{"ticket": 1, "stage": "ready", "recover": "skipped"}])

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
        tr, res = self.sweep({3: {"stage": "in-review", "since": self.OLD, "pr": 9, "ci_label": "green", "reviews": [], "focus_missing": ["behaviour"]}})
        self.assertEqual([(r["recover"], r["stage"]) for r in res], [("refire", "in-review")])
        self.assertIn(["label", '"factory:review-behaviour"'], tr.tickets[3]["log"])

    def test_in_review_without_ci_label_only_comments(self):
        tr, res = self.sweep({3: {"stage": "in-review", "since": self.OLD, "pr": 9, "ci_label": None}})
        self.assertEqual([r["recover"] for r in res], ["comment"])
        self.assertFalse([e for e in tr.tickets[3]["log"] if e[0] == "label"])

    def test_comment_only_note_does_not_use_the_retry(self):
        tr, res = self.sweep({3: {"stage": "in-review", "since": self.OLD, "pr": 9, "ci_label": None}})
        self.assertEqual([r["recover"] for r in res], ["comment"])
        tr.tickets[3].update(ci_label="green", reviews=[], focus_missing=["behaviour"])  # CI finished, the reviewer run then died
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
        self.assertEqual(len(gate.sweep_stale(tr, NODES, 2, datetime(2026, 11, 1, 12, tzinfo=timezone.utc), dry_run=True)[0]), 1)
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
    """`--spec`: check and apply need the automation's spec, never engine/nodes.yaml."""

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
            self.assertEqual((code, json.loads(out)["outcomes"]), (0, ["done", "error", "blocked", "failed", "env-broken"]))
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

    def test_fix_starts_for_conflict_without_findings(self):
        tr = MemoryTracker({7: {"stage": "changes-requested", "head": "abc", "pr": 42,
                                "conflict": True, "round_complete": False}})
        code, out = gate.check("fix", {"id": "e", "ticket": 7}, tr, NODES, CONFIG)
        self.assertEqual((code, out["merge_main"]), (gate.GO, True))

    def test_conflict_only_fix_done_applies_with_incomplete_round(self):
        tr = MemoryTracker({7: {"stage": "changes-requested", "head": "abc", "pr": 42,
                                "conflict": True, "round_complete": False}})
        with patch.object(gate, "local_head", return_value="def"):  # the merge commit is not pushed yet
            code, out = gate.apply("fix", "done", {"ticket": 7, "event": "e"}, tr, NODES)
        self.assertEqual((code, out["applied"]), (gate.GO, True))
        self.assertEqual(tr.tickets[7]["stage"], "in-review")

    def test_fix_done_without_conflict_still_needs_complete_round(self):
        tr = MemoryTracker({7: {"stage": "changes-requested", "head": "abc", "pr": 42,
                                "conflict": False, "round_complete": False}})
        with patch.object(gate, "local_head", return_value="def"):
            code, out = gate.apply("fix", "done", {"ticket": 7, "event": "e"}, tr, NODES)
        self.assertEqual((code, out), (gate.NO_GO, {"applied": False, "reason": "condition does not hold"}))
        self.assertEqual(tr.tickets[7]["stage"], "changes-requested")

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
        reason = "[factory] Blocked: merge conflict in factory/gate.py, engine/nodes.yaml"
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

    def test_merge_after_update_branch_without_rereview(self):
        tr, results = self.sweep(behind=True, head="h2", round_head="h2")  # facts carry a clean-merge approval to the new head
        self.assertEqual(results, [("merge", "updated")])
        tr, results = self.sweep(head="h2", round_head="h2")
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

    def test_not_planned_blocker_sends_dependent_to_triage_with_comment(self):
        tr, (results, failed) = self.sweep({
            10: {"blocking": [11], "state_reason": "not_planned"},
            11: {"stage": "ready", "blockers_open": 0},
        })
        self.assertFalse(failed)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in results], [(11, "re-triage")])
        self.assertEqual(tr.tickets[11]["stage"], "triage")
        self.assertEqual(tr.tickets[11]["log"], [
            ["comment", '"[factory] Blocker #10 was closed as not planned; re-triage this ticket"']])

    def test_completed_blocker_wakes_dependent(self):
        tr, (results, failed) = self.sweep({
            10: {"blocking": [11], "state_reason": "completed"},
            11: {"stage": "ready", "blockers_open": 0},
        })
        self.assertFalse(failed)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in results], [(11, "unblocked")])
        self.assertEqual(tr.tickets[11]["stage"], "ready")

    def test_dependent_with_other_open_blocker_untouched(self):
        tr, (results, failed) = self.sweep({
            10: {"blocking": [11], "state_reason": "not_planned"},
            11: {"stage": "ready", "blockers_open": 1},
        })
        self.assertFalse(failed)
        self.assertEqual(results, [])
        self.assertEqual(tr.tickets[11]["stage"], "ready")
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

    def test_sweep_finishes_person_merged_pr_from_changes_requested(self):
        tr = MemoryTracker({10: {"stage": "changes-requested", "pr_state": "merged"}})
        tr.sweep_tickets = lambda stages: [10]
        results, failed = gate.sweep(tr, NODES)
        self.assertFalse(failed)
        self.assertEqual([(r["node"], r["outcome"]) for r in results], [("finish", "merged")])
        self.assertEqual(tr.tickets[10]["stage"], "done")

    def test_sweep_finishes_person_merged_pr_from_blocked_and_drops_needs_human(self):
        tr = MemoryTracker({10: {"stage": "blocked", "pr_state": "merged",
                                 "attrs": ["factory:needs-human"]}})
        tr.sweep_tickets = lambda stages: [10]
        results, failed = gate.sweep(tr, NODES)
        self.assertFalse(failed)
        self.assertEqual([(r["node"], r["outcome"]) for r in results], [("finish", "merged")])
        self.assertEqual(tr.tickets[10]["stage"], "done")
        self.assertNotIn("factory:needs-human", tr.tickets[10]["attrs"])

    def test_after_merge_checks_listed_on_finish(self):
        tr = MemoryTracker({10: {"stage": "approved", "pr_state": "merged",
                                 "body": "- [ ] Operator check after the merge (not part of the PR): re-fire Air"}})
        tr.sweep_tickets = lambda stages: [10]
        results, failed = gate.sweep(tr, NODES)
        self.assertFalse(failed)
        self.assertEqual([(r["node"], r["outcome"]) for r in results], [("finish", "merged")])
        self.assertEqual(tr.tickets[10]["stage"], "approved")
        self.assertIn("factory:needs-human", tr.tickets[10]["attrs"])
        body = next(json.loads(value) for kind, value in tr.tickets[10]["log"] if kind == "comment")
        self.assertIn("- [ ] re-fire Air", body)

        tr.tickets[10]["comments"] = [{"body": body.replace("- [ ]", "- [x]")}]
        results, failed = gate.sweep(tr, NODES)
        self.assertFalse(failed)
        self.assertEqual([(r["node"], r["outcome"]) for r in results], [("finish", "merged")])
        self.assertEqual(tr.tickets[10]["stage"], "done")

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

    def test_wake_skips_closed_dependent(self):
        tr, (results, failed) = self.sweep({
            10: {"blocking": [11], "state_reason": "not_planned"},
            11: {"stage": "ready", "blockers_open": 0, "issue_state": "closed"},
        })
        self.assertFalse(failed)
        self.assertEqual(results, [])
        self.assertNotIn("log", tr.tickets[11])
        self.assertEqual(tr.tickets[11]["stage"], "ready")

    def test_wake_refires_once_per_event(self):
        tr = MemoryTracker({11: {"stage": "ready", "blockers_open": 0}})
        tr.sweep_tickets = lambda stages: []
        tr.blocking = lambda ticket: [11]
        self.assertEqual([r["outcome"] for r in gate.sweep(tr, NODES, closed_ticket=10)[0]], ["unblocked"])
        tr.tickets[11]["stage_at"] = datetime.now(timezone.utc).isoformat()  # the re-fire label just landed
        tr.tickets[11]["log"].clear()
        self.assertEqual(gate.sweep(tr, NODES, closed_ticket=10), ([], False))
        self.assertEqual(tr.tickets[11]["log"], [])
        tr.tickets[11]["stage_at"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        self.assertEqual([r["outcome"] for r in gate.sweep(tr, NODES, closed_ticket=10)[0]], ["unblocked"])
        tr.tickets[11]["log"].clear()
        tr.tickets[11]["since"] = datetime.now(timezone.utc).isoformat()  # unrelated recent activity, not a re-fire
        self.assertEqual([r["outcome"] for r in gate.sweep(tr, NODES, closed_ticket=10)[0]], ["unblocked"])

    def test_dependent_is_refired_once_when_listed_more_than_once(self):
        tr = MemoryTracker({11: {"stage": "ready", "blockers_open": 0}})
        tr.sweep_tickets = lambda stages: []
        tr.blocking = lambda ticket: [11, 11]

        results, failed = gate.sweep(tr, NODES, closed_ticket=10)

        self.assertFalse(failed)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in results], [(11, "unblocked")])


class RecalibratedBlocksTest(unittest.TestCase):
    def test_env_broken_parks_ticket_behind_main_red_ticket(self):
        tr = MemoryTracker({"7": {"stage": "in-dev", "attrs": ["factory:implement-mid"]},
                            "8": {"stage": "in-dev", "attrs": ["factory:implement-mid"]}})
        for n in ("7", "8"):
            code, out = gate.apply("implement", "env-broken", {"ticket": n, "failing_check": "doc-references"}, tr, NODES)
            self.assertEqual(code, gate.GO)
        red = [k for k, t in tr.tickets.items() if t.get("title") == "Main is red in Air: doc-references"]
        self.assertEqual(len(red), 1)  # reused, not duplicated
        for n in ("7", "8"):
            t = tr.tickets[n]
            self.assertEqual((t["stage"], t["blockers_open"], t["blocked_by"]), ("ready", 1, red))
            self.assertNotIn("factory:implement-mid", t["attrs"])

    def test_nothing_to_fix_reruns_join_not_block(self):
        tr = MemoryTracker({"7": {"stage": "changes-requested", "pr": 3, "round_complete": True,
                                   "attrs": ["factory:fix-mid"]}})
        code, _ = gate.apply("fix", "nothing-to-fix", {"ticket": "7"}, tr, NODES)
        self.assertEqual(code, gate.GO)
        t = tr.tickets["7"]
        self.assertEqual(t["stage"], "in-review")
        self.assertNotIn("factory:fix-mid", t["attrs"])
        self.assertIn(["join", '"all-approve"'], t["log"])
        self.assertNotIn(["label", '"factory:needs-human"'], t["log"])

    def test_finding_free_round_not_counted(self):
        def review(head, findings):
            return {"agent": True, "verdict": "changes", "commit_id": head, "rereview": False, "findings": findings}
        tr = MemoryTracker({"7": {"stage": "in-review", "reviews": [
            review("a", []), review("b", ["M1"]), review("c", ["B1", "M1"])]}})
        self.assertEqual(tr.facts("7")["fix_rounds"], 1)

    def test_grill_includes_proposed_checks(self):
        fields = gate.intake_fields({
            "handoff": "ROUTER-INTAKE", "ready": "no", "missing": ["a check says 'clean'"], "split": "none",
            "size_estimate": "~40",
            "proposed_checks": ["`uv run factory/tests/test_gate.py` passes", "no match: `rg TODO factory/`"]})
        self.assertIn("- `uv run factory/tests/test_gate.py` passes", fields["questions"])
        self.assertIn("- no match: `rg TODO factory/`", fields["questions"])
        tr = MemoryTracker({"7": {"stage": "triage"}})
        gate.apply("intake", "grill", {"ticket": "7", **fields}, tr, NODES)
        self.assertEqual(tr.tickets["7"]["stage"], "needs-answers")


class MultiRepositoryTest(unittest.TestCase):
    """One ticket with a PR in each of two repositories (ADR-0017): the PR facts combine."""
    ON = {"enabled": True, "max_risk": "high"}

    def pr(self, repo, head, **kw):
        return {"repo": repo, "pr": len(repo), "head": head, "branch": "ez/7-x", "pr_state": "open", "ci": "green",
                "round_complete": True, "round_verdict": "approve", "round_head": head, "mergeable": True, **kw}

    def facts(self, *prs, stage="approved"):
        return MemoryTracker({7: {"stage": stage, "risk": "low", "prs": list(prs)}}).facts(7)

    def test_one_pr_is_unchanged(self):
        from gate_port import combine_prs
        one = self.pr("a", "h1", reviews=[{"x": 1}])
        self.assertEqual(combine_prs([one]), one)

    def test_ticket_repositories_default_listed_and_unknown(self):
        from gate_port import ticket_repositories
        self.assertEqual(ticket_repositories("Affected repositories: a.", ["a", "b"], "a"), ["a"])
        self.assertEqual(ticket_repositories("repositories: a, b\n\n## Scope", ["a", "b"], "a"), ["a", "b"])
        with self.assertRaisesRegex(ValueError, "unknown repositories: c"):
            ticket_repositories("repositories: a c", ["a", "b"], "a")
        with self.assertRaisesRegex(ValueError, "start with the primary repository a"):
            ticket_repositories("repositories: b, a", ["a", "b"], "a")

    def test_both_approved_and_green_merge(self):
        facts = self.facts(self.pr("a", "h1"), self.pr("b", "h2"))
        self.assertEqual((facts["ci"], facts["round_head"], facts["heads"]), ("green", "h1", {"a": "h1", "b": "h2"}))
        self.assertEqual(gate.sweep_plan(facts, self.ON), ("merge", "merged", {}))

    def test_one_red_pr_makes_the_ticket_red_and_reopens_it(self):
        facts = self.facts(self.pr("a", "h1"), self.pr("b", "h2", ci="red"))
        self.assertEqual(facts["ci"], "red")
        self.assertEqual(gate.sweep_plan(facts, self.ON)[:2], ("reopen", "reopened"))

    def test_a_pr_pushed_after_its_review_reopens_and_never_merges(self):
        facts = self.facts(self.pr("a", "h1"), self.pr("b", "h3", round_head="h2"))
        self.assertIsNone(gate.merge_decision(facts, self.ON))
        self.assertEqual(gate.sweep_plan(facts, self.ON)[:2], ("reopen", "reopened"))

    def test_join_waits_for_every_pr_and_one_change_request_completes_the_round(self):
        open_round = self.facts(self.pr("a", "h1"), self.pr("b", "h2", round_complete=False, round_verdict=None,
                                                           round_head=None), stage="in-review")
        self.assertIsNone(gate.join_decision(open_round))
        changes = self.facts(self.pr("a", "h1"), self.pr("b", "h2", round_verdict="changes"), stage="in-review")
        self.assertEqual(gate.join_decision(changes)[0], "changes")

    def test_finish_waits_until_every_pr_merged(self):
        partly = self.facts(self.pr("a", "h1", pr_state="merged"), self.pr("b", "h2"))
        self.assertEqual(partly["pr_state"], "open")
        self.assertNotEqual(gate.sweep_plan(partly, self.ON)[:2], ("finish", "merged"))
        both = self.facts(self.pr("a", "h1", pr_state="merged"), self.pr("b", "h2", pr_state="merged"))
        self.assertEqual(gate.sweep_plan(both, self.ON), ("finish", "merged", {}))

    def test_sweep_merges_the_other_repository_first_then_finishes(self):
        tr = MemoryTracker({7: {"stage": "approved", "risk": "low", "prs": [self.pr("a", "h1"), self.pr("b", "h2")]}})
        tr.sweep_tickets = lambda stages: [7]
        gate.sweep(tr, NODES, auto_merge=self.ON)
        merged = [json.loads(v) for k, v in tr.tickets[7]["log"] if k == "merge"]
        self.assertEqual(merged, [{"repo": "b", "head": "h2"}, {"repo": "a", "head": "h1"}])
        self.assertEqual(tr.tickets[7]["stage"], "approved")  # the primary's merge event runs `finish`
        gate.sweep(tr, NODES, auto_merge=self.ON)
        self.assertEqual(tr.tickets[7]["stage"], "done")

    def test_a_partly_merged_ticket_merges_only_the_rest_on_the_next_sweep(self):
        tr = MemoryTracker({7: {"stage": "approved", "risk": "low",
                                "prs": [self.pr("a", "h1"), self.pr("b", "h2", pr_state="merged")]}})
        tr.sweep_tickets = lambda stages: [7]
        gate.sweep(tr, NODES, auto_merge=self.ON)
        merged = [json.loads(v) for k, v in tr.tickets[7]["log"] if k == "merge"]
        self.assertEqual(merged, [{"repo": "a", "head": "h1"}])

    def test_ticket_for_pr_finds_single_and_multi_repository_tickets(self):
        tr = MemoryTracker({7: {"pr": 9}, 8: {"prs": [self.pr("a", "h1"), self.pr("bb", "h2")]}})
        self.assertEqual([tr.ticket_for_pr(9), tr.ticket_for_pr(9, "o/primary")], [7, 7])
        self.assertEqual([tr.ticket_for_pr(2, "bb"), tr.ticket_for_pr(2), tr.ticket_for_pr(2, "a")], [8, 8, None])

    def test_check_lists_checkouts_and_refuses_a_stale_head_in_any_repository(self):
        tr = MemoryTracker({7: {"stage": "approved", "risk": "low",
                                "prs": [self.pr("a", "h1"), self.pr("b", "h2")]}})
        code, out = gate.check("merge", {"id": "e1", "ticket": 7, "heads": {"a": "h1", "b": "h2"}}, tr, NODES, CONFIG)
        self.assertEqual((code, out["checkouts"]), (gate.GO, [{"repo": "a", "branch": "ez/7-x"},
                                                              {"repo": "b", "branch": "ez/7-x"}]))
        code, out = gate.check("merge", {"id": "e2", "ticket": 7, "heads": {"a": "h1", "b": "old"}}, tr, NODES, CONFIG)
        self.assertEqual((code, out["reason"]), (gate.NO_GO, "stale head"))


class RepoSplitTest(unittest.TestCase):
    """Triage splits a ticket over repositories into one ready child each, in dependency order (ADR-0017)."""
    SLICES = [{"title": "API", "body": "api part", "repo": "api", "subsystem": "core"},
              {"title": "Client", "body": "client part", "repo": "client", "subsystem": "core", "after": ["api"]},
              {"title": "Deploy", "body": "deploy part", "repo": "deploy", "subsystem": "core", "after": ["api"]}]

    def split(self, slices):
        from gate_port import MemoryTicketStore
        store = MemoryTicketStore()
        n = store.add("parent")
        store.create_children(n, gate.slices(slices), "factory:ready")
        return store, n, {c["title"]: c["number"] for c in store.children(n) for c in [{**c, "title": store.tickets[c["number"]]["title"]}]}

    def test_split_creates_repo_children_in_dependency_order(self):
        store, n, ids = self.split(self.SLICES)
        self.assertEqual([store.tickets[i]["attrs"] for i in ids.values()],
                         [["factory:ready", f"repo:{r}", "subsystem:core"] for r in ("api", "client", "deploy")])
        self.assertEqual([store.open_blockers(ids[t]) for t in ("API", "Client", "Deploy")],
                         [[], [ids["API"]], [ids["API"]]])  # client and deploy wait for api only
        self.assertFalse(store.create_children(n, gate.slices(self.SLICES), "factory:ready"))  # a retry changes nothing

    def test_child_pr_in_its_repository(self):
        from gate_port import MemoryCodeHost
        store, n, ids = self.split(self.SLICES)
        pr = lambda number, ticket: {"number": number, "branch": f"ez/{ticket}-x", "head": "h", "labels": []}  # noqa: E731
        hosts = {"o/api": MemoryCodeHost({"o/api": [pr(5, ids["API"])]}),
                 "o/client": MemoryCodeHost({"o/client": [pr(5, ids["Client"])]})}
        found = {r: h.ticket_for_pr(5, r) for r, h in hosts.items()}
        self.assertEqual(found, {"o/api": ids["API"], "o/client": ids["Client"]})  # same PR number, own repository
        self.assertIn("repo:client", store.tickets[found["o/client"]]["attrs"])
        self.assertIsNone(hosts["o/api"].ticket_for_pr(5, "o/client"))

    def test_parent_done_when_all_children_done(self):
        store, n, ids = self.split(self.SLICES)

        def plan():
            kids = [{**c, "blocked": False} for c in store.children(n)]
            return gate.sweep_plan({"stage": "split", "children": kids, "comments": []}, None)

        for t in ("API", "Client"):
            store.close(ids[t])
        self.assertIsNone(plan())
        store.close(ids["Deploy"])
        self.assertEqual(plan()[:2], ("finish", "children-done"))

    def test_one_repository_ticket_continues_without_children(self):
        h = {"outcome": "proceed", "size_estimate": "~100"}
        self.assertEqual(gate.intake_fields(h)["children"], [])
        tr = MemoryTracker({7: {"stage": "triage"}})
        code, _ = gate.apply("intake", "proceed", {"ticket": 7, **PAYLOAD, **gate.intake_fields(h)}, tr, NODES)
        self.assertEqual((code, tr.tickets[7]["stage"]), (gate.GO, "ready"))
        self.assertNotIn("create-issues", [k for k, _ in tr.tickets[7].get("log", [])])


class CiSyncTest(unittest.TestCase):
    """The scheduled CI label sync of the project repositories' factory PRs (gate.ci_sync)."""
    GREEN, RED = "factory:ci-green", "factory:ci-red"

    def host(self):
        from gate_port import MemoryCodeHost
        pr = lambda n, head, labels=(), labeled_at=None, branch=None: {  # noqa: E731
            "number": n, "branch": branch or f"ez/{n}-x", "head": head, "labels": list(labels), "labeled_at": labeled_at or {}}
        ok = lambda at="2026-11-05T10:00:00Z": {"conclusion": "success", "at": at}  # noqa: E731
        return MemoryCodeHost(
            {"o/app": [pr(1, "a1"), pr(2, "a2", [self.GREEN]), pr(3, "a3", [self.GREEN]), pr(4, "a4", branch="feature")],
             "o/web": [pr(5, "w5", [self.GREEN], {self.GREEN: "2026-11-05T10:05:00Z"}),
                       pr(6, "w6", [self.GREEN], {self.GREEN: "2026-11-05T09:00:00Z"}), pr(7, "w7")]},
            {"a1": [ok(), {"conclusion": "neutral", "at": None}],  # a check run and a commit status
             "a2": [ok(), {"conclusion": "failure", "at": "2026-11-05T10:01:00Z"}],
             "a3": [ok(), {"conclusion": None, "at": None}], "a4": [ok()],
             "w5": [ok()], "w6": [ok()]}, now="2026-11-05T11:00:00Z")

    def test_sync_sets_ci_label_from_head_checks(self):
        repos = gate.project_repositories({"repositories": [
            {"name": "main", "url": "https://github.com/o/main", "role": "primary"},
            {"name": "app", "url": "https://github.com/o/app", "role": "project"},
            {"name": "web", "url": "https://github.com/o/web/", "role": "project"}]})
        self.assertEqual(repos, ["o/app", "o/web"])
        host = self.host()
        results, failed = gate.ci_sync(host, repos)
        self.assertFalse(failed)
        self.assertEqual([(r["repo"], r["pr"], r["ci"]) for r in results],  # PR 4 is no factory branch
                         [("o/app", 1, "green"), ("o/app", 2, "red"), ("o/app", 3, "pending"),
                          ("o/web", 5, "green"), ("o/web", 6, "green"), ("o/web", 7, "none")])
        self.assertEqual(host.edits, [("o/app", 1, [self.GREEN], []), ("o/app", 2, [self.RED], [self.GREEN]),
                                      # labelled before the head's result: an earlier head's label, set again
                                      ("o/web", 6, [self.GREEN], [self.GREEN])])
        labels = {(repo, p["number"]): p["labels"] for repo, prs in host.prs.items() for p in prs}
        self.assertEqual(labels[("o/app", 3)], [self.GREEN])  # pending: no change
        self.assertEqual(labels[("o/web", 5)], [self.GREEN])  # labelled after this head's result: no change
        gate.ci_sync(host, repos)
        self.assertEqual(len(host.edits), 3)  # a second pass writes nothing

    def test_sync_dry_run_writes_nothing_and_one_failing_repository_does_not_stop_the_others(self):
        host = self.host()
        results, failed = gate.ci_sync(host, ["o/missing", "o/app"], dry_run=True)
        self.assertTrue(failed)
        self.assertEqual((results[0]["repo"], results[1]["added"], host.edits), ("o/missing", [self.GREEN], []))


class MentionTest(unittest.TestCase):
    """`@factory:<persona>` (ADR-0020): the gate wakes the persona's node, or answers once and wakes nothing."""

    def setUp(self):
        self.nodes = copy.deepcopy(NODES)
        self.nodes["nodes"]["implement"]["agent"], self.nodes["nodes"]["intake"]["agent"] = "dev", "ria"
        self.config = {**CONFIG, "agents": {"dev": {"persona": "dev", "role": "developer", "tier": "mid"},
                                            "ria": {"persona": "ria", "role": "router", "tier": "cheap"}}}
        self.woken = []

    def mention(self, body, comment_id=1):
        tracker = self.tracker = getattr(self, "tracker", None) or MemoryTracker({"7": {"stage": "in-dev", "comments": []}})
        tracker.tickets["7"]["comments"].append({"id": comment_id, "author": "ann", "body": body})
        return gate.mention(tracker, "ticket", 7, comment_id, self.nodes, self.config, lambda node, p: self.woken.append((node, p)))

    def replies(self):
        return [json.loads(v) for k, v in self.tracker.tickets["7"].get("log", []) if k == "comment"]

    def test_person_mentions_dev_wakes_its_node_on_that_subject(self):
        self.mention("@factory:dev why is this slow?")
        self.assertEqual(self.woken, [("implement", {"subject": "ticket", "n": 7, "comment_id": 1, "from": "ann",
                                                      "persona": "dev", "text": "why is this slow?"})])

    def test_unknown_persona_gets_one_reply_and_no_wake(self):
        self.mention("@factory:nobody hi")
        self.assertEqual(self.woken, [])
        self.assertEqual(len(self.replies()), 1)
        self.assertIn("nobody", self.replies()[0])
        self.assertNotIn("@factory:", self.replies()[0])  # a reply must not mention anyone again

    def test_disallowed_agent_pair_wakes_nothing(self):
        self.mention("[factory] **ria (router):** @factory:rex look at this")  # rex acts on pull requests only
        self.assertEqual(self.woken, [])

    def test_same_comment_twice_wakes_once(self):
        self.mention("@factory:dev hi")
        self.tracker.tickets["7"]["comments"].clear()
        self.mention("@factory:dev hi")
        self.assertEqual(len(self.woken), 1)

    def test_reply_posts_one_comment_with_persona_role_and_trailer_and_keeps_the_stage(self):
        tracker = MemoryTracker({"7": {"stage": "in-dev"}})
        gate.reply(tracker, self.nodes, "implement", {"n": 7, "persona": "dev", "comment_id": 1}, "because of X")
        self.assertEqual([json.loads(v) for k, v in tracker.tickets["7"]["log"]],
                         ["[factory] **dev (developer):** because of X\n\nFactory-Run: reply:1"])
        self.assertEqual((tracker.tickets["7"]["stage"], tracker.tickets["7"].get("attrs", [])), ("in-dev", []))


if __name__ == "__main__":
    unittest.main()

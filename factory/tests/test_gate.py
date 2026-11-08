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


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
sys.path.insert(0, str(ROOT / "factory/tests"))
import config as cfg  # noqa: E402
import project_mode  # noqa: E402
cfg.set_team(project_mode.team(Path(__file__).resolve().parent / "fixtures/team"))
import deploy  # noqa: E402
import gate  # noqa: E402
import nodes  # noqa: E402
import definition  # noqa: E402
from factory_plugins.comments import node as comment_node  # noqa: E402
from factory_plugins.github import ci_sync as gh_ci_sync, gate as gh_gate  # noqa: E402
from gate_port import MemorySystem, TagTracker  # noqa: E402

CONFIG = cfg.load()
NODES = nodes.load()
PAYLOAD = {"risk": "low", "complexity": "small", "tier": "mid"}


def make(cls, tid, **kw):
    t = {"head": "abc", "blockers_open": 0, "ci": "green"}  # reviewers start on green CI
    if cls is TagTracker:
        t["tags"] = ["stage:triage", "risk:low"]
    else:
        t["stage"] = "triage"
    return cls({tid: {**t, **kw}})


class GateTest(unittest.TestCase):
    def test_slop_ci_red_routes_to_fix_and_pending_ci_blocks_review(self):
        graph = definition.nodes(definition.load(ROOT / "factory/tests/fixtures/slop"))

        red = MemorySystem({"7": {"stage": "in-review", "ci": "red"}})
        status, outcome = gate.decide("ci-red", "7", red, graph, dry_run=True)
        self.assertEqual((status, outcome["outcome"]), (gate.GO, "fix"))

        pending = MemorySystem({"7": {"stage": "in-review", "ci": "pending", "attrs": ["risk:high"]}})
        status, _ = gate.check("review-high", {"id": "e1", "ticket": "7"}, pending, graph, CONFIG)
        self.assertEqual(status, gate.NO_GO)

    def test_partial_outcome_failure_reports_effects_that_landed(self):
        graph = {"nodes": {"review": {"when": {}, "outcomes": {"changes": {"effects": [
            {"review": "request-changes-at-head"}, {"unlabel": "factory:review-x", "on": "pr"}]}}}}}

        class FailsAfterReview(MemorySystem):
            def apply(self, effect):
                if effect["kind"] == "unlabel":
                    raise RuntimeError("unlabel failed")
                if effect["kind"] == "comment":
                    self.report = effect
                return super().apply(effect)

        tr = FailsAfterReview({"7": {"stage": "in-review", "head": "abc"}})
        with self.assertRaisesRegex(RuntimeError, "unlabel failed"):
            gate.apply("review", "changes", {"ticket": "7", "focus": "x", "round": 1,
                                                "run_id": "run-1"}, tr, graph)
        comments = [json.loads(v) for kind, v in tr.tickets["7"]["log"] if kind == "comment"]
        self.assertEqual(len(comments), 1)
        self.assertIn("review", comments[0])
        self.assertEqual((tr.report["on"], tr.report["run_id"]), ("pr", "run-1"))

    def test_sweep_plan_replays_stale_review_marker_instead_of_refiring(self):
        facts = {"stage": "in-review", "head": "abc", "pr_labels": ["factory:review-x"],
                 "reviews": [{"agent": True, "focus": "x", "verdict": "changes", "commit_id": "abc", "round": 2}]}
        self.assertEqual(gate.sweep_plan(facts),
                         ("review", "changes", {"focus": "x", "round": 2, "review_replay": True}))

    def test_definition_replay_uses_review_node_and_marker_outcome(self):
        node = {"triggers": [{"label": "factory:review-high"}], "outcomes": {
            "approve-low": {"effects": [{"review": "approve-at-head"}]},
            "approve-risky": {"effects": [{"review": "approve-at-head"}]},
            "changes": {"effects": [{"review": "request-changes-at-head"}]}}}
        graph = {"nodes": {"review-high": node}}
        review = {"agent": True, "focus": "review-high", "verdict": "approve", "commit_id": "abc", "round": 1}
        facts = {"stage": "in-review", "head": "abc", "pr_labels": ["factory:review-high"], "reviews": [review]}
        with patch("gate.definition_team", return_value=True):
            self.assertIsNone(gate.stale_review_plan(facts, graph))  # two approvals: an unnamed one is not guessed
            facts["reviews"] = [{**review, "outcome": "approve-risky"}]
            self.assertEqual(gate.stale_review_plan(facts, graph)[:2], ("review-high", "approve-risky"))
            facts["reviews"] = [{**review, "verdict": "changes"}]
            self.assertEqual(gate.stale_review_plan(facts, graph)[:2], ("review-high", "changes"))
            self.assertIsNone(gate.stale_review_plan({**facts, "pr_labels": []}, graph))

    def test_assignment_effect_resolves_the_payload_field(self):
        self.assertEqual(gate.resolve({"assign": {"field": "assignee"}}, {"assignee": "bob"}, "7"),
                         {"kind": "assign", "ticket": "7", "value": "bob"})
        self.assertEqual(gate.resolve({"assign": {"field": "assignee"}, "on": "pr"}, {"assignee": ["a", "b"]}, "7"),
                         {"kind": "assign", "ticket": "7", "value": ["a", "b"], "on": "pr"})

    def test_assignment_field_missing_or_empty_fails(self):
        for payload in ({}, {"assignee": ""}, {"assignee": "  "}, {"assignee": []}, {"assignee": [""]}, {"assignee": 3}):
            with self.subTest(payload=payload), self.assertRaises(gate.Bad):
                gate.resolve({"assign": {"field": "assignee"}}, payload, "7")

    def test_assignment_effect_resolves_owner_and_preserves_logins(self):
        self.assertEqual(gate.resolve({"assign": "owner", "on": "pr"}, {}, "7"),
                         {"kind": "assign", "ticket": "7", "value": cfg.owner(CONFIG), "on": "pr"})
        self.assertEqual(gate.resolve({"assign": ["a", "b"]}, {}, "7"),
                         {"kind": "assign", "ticket": "7", "value": ["a", "b"]})

    def test_label_guard_falls_back(self):
        """ADR-0021: an outcome's `if` guard that fails applies its `else`; without one the outcome is refused."""
        graph = copy.deepcopy(NODES)
        graph["nodes"]["intake"]["outcomes"]["cheap"] = {
            "if": {"label": "risk:low"}, "else": "proceed", "effects": [{"stage": "ready"}, {"label": "factory:cheap"}]}
        high = MemorySystem({"7": {"stage": "triage", "attrs": ["risk:high"]}})
        gate.apply("intake", "cheap", {"ticket": "7", "risk": "high", "complexity": "small"}, high, graph)
        self.assertNotIn("factory:cheap", high.facts("7")["attrs"])
        low = MemorySystem({"7": {"stage": "triage", "attrs": ["risk:low"]}})
        gate.apply("intake", "cheap", {"ticket": "7"}, low, graph)
        self.assertIn("factory:cheap", low.facts("7")["attrs"])
        del graph["nodes"]["intake"]["outcomes"]["cheap"]["else"]
        with self.assertRaises(gate.Bad):
            gate.apply("intake", "cheap", {"ticket": "7"}, MemorySystem({"7": {"stage": "triage"}}), graph)

    def test_confirmed_bug_starts_implementation(self):
        """#161: investigate's `bug` outcome moves the ticket to in-dev and adds the implement label that wakes `implement`."""
        tr = MemorySystem({"7": {"stage": "investigating"}})
        gate.apply("investigate", "bug", {"ticket": "7", "diagnosis": "d", "tier": "mid"}, tr, NODES)
        facts = tr.facts("7")
        self.assertEqual(facts["stage"], "in-dev")
        self.assertIn("factory:implement-mid", facts["attrs"])

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
        # A label that an effect puts `on: pr` counts too: factory:needs-human waits on the PR.
        self.assertTrue(gate.guard_holds({"label": "factory:needs-human"}, {**f, "pr_labels": ["factory:needs-human"]}))
        self.assertTrue(gate.guard_holds({"label": ["risk:low", "factory:needs-human"]},
                                         {**f, "pr_labels": ["factory:needs-human"]}))

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
                tr = MemorySystem({"7": {**ok, **change}})
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
        merged = MemorySystem({"7": {"stage": "in-review", "pr_state": "merged"}})
        status, out = gate.decide("finisher", "7", merged, graph)
        self.assertEqual((out["outcome"], merged.get_stage(merged._t("7"))), ("done", "done"))
        split = MemorySystem({"8": {"stage": "split", "children": [{"state": "closed"}]}})
        split.children = lambda n: [{"state": "closed"}]
        self.assertEqual(gate.decide("finisher", "8", split, graph)[1]["outcome"], "children-done")
        idle = MemorySystem({"9": {"stage": "in-review"}})
        self.assertEqual(gate.decide("finisher", "9", idle, graph)[1]["outcome"], "waiting")
        del graph["nodes"]["finisher"]["outcomes"]["waiting"]
        status, out = gate.decide("finisher", "9", idle, graph)
        self.assertEqual((status, out["outcome"]), (gate.NO_GO, None))

    def test_reaching_a_final_stage_wakes_unblocked_dependents(self):
        """ADR-0021: automatic, no effect in the workflow: done re-adds `ready` to each fully unblocked dependent."""
        graph = copy.deepcopy(NODES)
        graph["nodes"]["intake"]["outcomes"]["closing"] = {"effects": [{"stage": "done"}]}
        tr = MemorySystem({"7": {"stage": "triage"}, "8": {"stage": "ready", "blockers_open": 1},
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
        tr = MemorySystem({"7": {"stage": "in-review", "head": "h1", "ci": "pending"}})
        facts = tr.facts
        tr.facts = lambda n: {**facts(n), "ci": next(states)}
        tr.ci_failure = lambda head: "lint: E501"
        naps = []
        out = gate.ci_state(tr, "7", wait_seconds=600, poll=30, sleep=naps.append)
        self.assertEqual((out["ci"], out["failing"], len(naps)), ("red", "lint: E501", 2))
        self.assertEqual(gate.ci_state(MemorySystem({"7": {"ci": "pending"}}), "7")["ci"], "pending")   # no wait

    def ask_graph(self):
        graph = copy.deepcopy(NODES)
        graph["nodes"]["intake"].update(agent="dev", role="developer")
        graph["nodes"]["intake"]["outcomes"]["ask"] = {"to": ["reviewer"], "effects": [{"comment": "text"}]}
        graph["nodes"]["reply-dev"] = {**graph["nodes"]["intake"], "outcomes": {"replied": {"effects": []}}}
        return graph

    def ask(self, tracker, to="reviewer", node="intake", hops=3):
        with patch.object(gate.config_mod, "load", return_value={"limits": {"mention_hops": hops}, "plugins": ["comments"]}):
            return gate.apply(node, "ask", {"ticket": "7", "to": to, "text": "Is X\nin scope?"}, tracker, self.ask_graph())

    def test_ask_posts_one_comment_with_the_marker_and_keeps_the_stage(self):
        tr = MemorySystem({"7": {"stage": "triage"}})
        self.assertEqual(self.ask(tr)[0], gate.GO)
        posted = [json.loads(v) for k, v in tr.tickets["7"]["log"] if k == "comment"]
        self.assertEqual(len(posted), 1)
        self.assertTrue(posted[0].startswith("[factory] **dev (developer):** @factory:reviewer Is X in scope?\n"))
        self.assertIn("<!-- factory:ask id=7-1 from=intake to=reply-reviewer hop=1 -->", posted[0])
        self.assertEqual(tr.facts("7")["stage"], "triage")

    def test_ask_refuses_a_pair_outside_the_allowlist(self):
        tr = MemorySystem({"7": {"stage": "triage"}})
        with self.assertRaisesRegex(gate.Bad, "not an agent this node may ask"):
            self.ask(tr, to="approver")
        self.assertNotIn("log", tr.tickets["7"])  # nothing posted, nobody woken

    def test_ask_over_the_hop_budget_posts_one_refusal_and_wakes_nothing(self):
        asks = [{"author_is_bot": True, "author_is_factory": True, "body": f"[factory] @factory:reviewer q <!-- factory:ask id=7-{i} from=intake to=reply-reviewer hop={i} -->"} for i in (1, 2, 3)]
        tr = MemorySystem({"7": {"stage": "triage", "comments": asks[:2]}})
        self.ask(tr)
        self.assertIn("hop=3", tr.tickets["7"]["log"][0][1])  # the 3rd ask is allowed
        tr = MemorySystem({"7": {"stage": "triage", "comments": asks}})
        self.ask(tr)
        posted = [json.loads(v) for k, v in tr.tickets["7"]["log"] if k == "comment"]
        self.assertEqual(len(posted), 1)
        self.assertIn("limit of 3 asks", posted[0])
        self.assertNotIn("@factory:", posted[0])
        self.assertNotIn("factory:ask", posted[0])

    def test_ask_budget_ignores_markers_from_other_authors(self):
        forged = [{"author": "eve", "author_is_bot": False, "body": f"[factory] <!-- factory:ask id=7-{i} from=intake to=reply-reviewer hop={i} -->"} for i in (1, 2, 3)]
        tr = MemorySystem({"7": {"stage": "triage", "comments": forged}})
        self.ask(tr)
        self.assertIn("hop=1", tr.tickets["7"]["log"][0][1])

    def test_ask_is_not_an_outcome_of_a_reply(self):
        tr = MemorySystem({"7": {"stage": "triage"}})
        with self.assertRaisesRegex(gate.Bad, "a reply cannot ask"):
            self.ask(tr, node="reply-dev")

    def comment_graph(self, when=None):
        graph = copy.deepcopy(NODES)
        graph["nodes"]["fix"] = {**graph["nodes"]["intake"], "triggers": ["pull_request.labeled factory:fix"],
                                 "when": when or {"stage": "changes-requested"}, "outcomes": {"done": {"effects": []}}}
        graph["nodes"]["reply-dev"] = {**graph["nodes"]["intake"], "triggers": ["issues.labeled factory:reply-dev"],
                                       "when": {"always": True}, "outcomes": {"replied": {"effects": []}}}
        graph["nodes"]["comment"] = {**graph["nodes"]["intake"], "when": {"always": True}, "outcomes": {
            "route": {"effects": [{"route": "to"}]}, "reply": {"effects": [{"reply": "to"}]},
            "skipped": {"effects": [{"skip": "now"}]}, "later": {"effects": []}}}
        return graph

    def comment_run(self, tracker, outcome, graph=None, **payload):
        with patch("gate.definition_team", return_value=False), \
                patch.object(gate.config_mod, "load", return_value={**CONFIG, "plugins": ["comments"], "tracker": {**CONFIG["tracker"], "commands": {}}}):
            return gate.apply("comment", outcome, {"ticket": "7", **payload}, tracker, graph or self.comment_graph())

    def approved_pr(self, **kw):
        return MemorySystem({"7": {"stage": "approved", "attrs": ["factory:needs-human", "factory:comment"],
                                    "pr_labels": ["factory:needs-human", "factory:comment"], "pr_state": "open",
                                    "comments": [{"id": 5, "author": "eve", "author_is_bot": False, "body": "please fix X"}], **kw}})

    def test_pr_comment_event_maps_to_ticket(self):
        class Host:
            def ticket_for_pr(self, number, repository=None):
                return 7 if number == 12 else None
        event = {"action": "created", "issue": {"number": 12, "pull_request": {"url": "u"}}, "comment": {"id": 1},
                 "repository": {"full_name": "o/r"}}
        self.assertEqual(gate.from_trigger(event, Host()), {"id": "issue_comment:7:created", "ticket": 7,
                                                            "event_name": "issue_comment", "action": "created"})
        self.assertEqual(gate.from_trigger({"action": "created", "issue": {"number": 12}})["ticket"], 12)  # a ticket comment

    def test_comment_route_moves_the_item_and_cleans_up(self):
        tr = self.approved_pr()
        self.assertEqual(self.comment_run(tr, "route", to="fix", reply="Sent to the fixer.")[0], gate.GO)
        facts = tr.facts("7")
        self.assertEqual(facts["stage"], "changes-requested")
        self.assertIn("factory:fix", facts["pr_labels"])
        for gone in ("factory:needs-human", "factory:comment"):
            self.assertNotIn(gone, facts["attrs"])
            self.assertNotIn(gone, facts["pr_labels"])
        posted = [json.loads(v) for k, v in tr.tickets["7"]["log"] if k == "comment"]
        self.assertEqual(len(posted), 1)
        self.assertTrue(posted[0].startswith("Sent to the fixer."))
        self.assertIn("<!-- factory:comment-seen 5 -->", posted[0])

    def test_comment_refuses_failed_when(self):
        tr = self.approved_pr()
        graph = self.comment_graph({"stage": "changes-requested", "ci": "green"})   # CI is "none"
        with self.assertRaisesRegex(gate.Bad, "its `when` fails"):
            self.comment_run(tr, "route", graph, to="fix")
        with self.assertRaisesRegex(gate.Bad, "unknown node"):
            self.comment_run(tr, "route", graph, to="nowhere")
        self.assertNotIn("log", tr.tickets["7"])
        self.assertEqual(tr.facts("7")["stage"], "approved")

    def test_comment_reply_wakes_the_agent_and_keeps_the_stage(self):
        tr = self.approved_pr()
        self.comment_run(tr, "reply", to="dev")
        facts = tr.facts("7")
        self.assertEqual(facts["stage"], "approved")
        self.assertIn("factory:reply-dev", facts["attrs"])
        self.assertIn("factory:needs-human", facts["attrs"])   # a question only: the person still decides
        self.assertNotIn("factory:comment", facts["attrs"])

    def test_comment_skipped_reacts_and_moves_the_cursor(self):
        tr = self.approved_pr()
        self.comment_run(tr, "skipped")
        log = [(k, json.loads(v)) for k, v in tr.tickets["7"]["log"]]
        self.assertIn(("react", "eyes"), log)
        self.assertEqual(tr.facts("7")["stage"], "approved")
        self.assertNotIn("factory:comment", tr.facts("7")["attrs"])

    def test_comment_cursor_skips_handled_comments(self):
        old = {"id": 5, "author": "eve", "author_is_bot": False, "body": "old"}
        seen = {"id": 6, "author": "bot", "author_is_bot": True, "author_is_factory": True,
                "body": "[factory] Done.\n<!-- factory:comment-seen 5 -->\n<!-- factory:from comment -->"}
        forged = {"id": 7, "author": "eve", "author_is_bot": False, "body": "x <!-- factory:comment-seen 99 -->"}
        new = {"id": 8, "author": "eve", "author_is_bot": False, "body": "new"}
        own = {"id": 9, "author": "bot", "author_is_bot": True, "author_is_factory": True, "body": "[factory] note\n<!-- factory:from dev -->"}
        pr = {"id": 10, "author": "eve", "author_is_bot": False, "body": "on the PR"}
        facts = {"comments": [old, seen, forged, new, own], "pr_comments": [pr]}
        self.assertEqual([(c["id"], c["on"]) for c in comment_node.new_comments(facts)], [(7, "ticket"), (8, "ticket"), (10, "pr")])

    def test_comment_cursor_ignores_other_bots(self):
        human = {"id": 1, "author": "eve", "author_is_bot": False, "body": "please"}
        other = {"id": 2, "author": "other[bot]", "author_is_bot": True, "author_is_factory": False, "body": "[factory] x <!-- factory:comment-seen 999 -->"}
        own = {"id": 3, "author": "air[bot]", "author_is_bot": True, "author_is_factory": True, "body": "[factory] ok <!-- factory:comment-seen 1 --> <!-- factory:from comment -->"}
        facts = {"comments": [human, other, own]}
        self.assertEqual([c["id"] for c in comment_node.new_comments(facts)], [2])

    def test_only_the_configured_bot_counts_as_the_factory(self):
        marks = {"cursor": "<!-- factory:comment-seen 50 -->",
                 "ask": "<!-- factory:ask id=7-1 from=intake to=reply-reviewer hop=1 -->"}
        authors = [("configured bot", "air[bot]", True, True), ("another [bot] account", "ci[bot]", True, False),
                   ("a person", "eve", False, False), ("a person writing [factory]", "eve", False, False)]
        for label, login, is_bot, is_factory in authors:
            with self.subTest(author=label):
                c = {"id": 1, "author": login, "author_is_bot": is_bot, "author_is_factory": is_factory,
                     "body": f"[factory] x {marks['cursor']} {marks['ask']}"}
                self.assertEqual(comment_node.cursor([c]), 50 if is_factory else 0)
                self.assertEqual([m["id"] for m in comment_node.new_comments({"comments": [c]})], [] if is_factory else [1])
                tr = MemorySystem({"7": {"stage": "triage", "comments": [dict(c) for _ in range(3)]}})
                self.ask(tr)
                # the 3 asks exhaust the budget only when the factory wrote them
                self.assertEqual("limit of 3 asks" in tr.tickets["7"]["log"][0][1], is_factory)

    def test_comment_leaves_comments_that_arrived_during_the_run(self):
        tr = self.approved_pr()
        with tempfile.TemporaryDirectory() as d, patch.object(comment_node, "input_path", lambda t: Path(d) / f"{t}.json"):
            comment_node.save_input("7", [{"id": 5}])
            tr.tickets["7"]["comments"].append({"id": 6, "author": "eve", "author_is_bot": False, "body": "late"})
            self.comment_run(tr, "route", to="fix")
        posted = [json.loads(v) for k, v in tr.tickets["7"]["log"] if k == "comment"]
        self.assertIn("<!-- factory:comment-seen 5 -->", posted[0])
        self.assertIn("factory:comment", tr.facts("7")["attrs"])  # woken again for comment 6

    def test_comment_route_stopped_by_the_spend_guard_keeps_the_input_pending(self):
        tr = self.approved_pr(agent_runs=8, run_cap=8)
        with patch.object(gate, "commands", return_value=("factory:fix",)):
            self.comment_run(tr, "route", to="fix", reply="Sent to the fixer.")
        facts = tr.facts("7")
        self.assertEqual(facts["stage"], "blocked")
        self.assertNotIn("factory:comment", facts["attrs"])
        posted = [json.loads(v) for k, v in tr.tickets["7"]["log"] if k == "comment"]
        self.assertEqual(len(posted), 1)
        self.assertNotIn("Sent to the fixer.", posted[0])
        self.assertNotIn("factory:comment-seen", posted[0])

    def test_mention_fact(self):
        """#590: a person may ask any agent; an agent only a workflow neighbour; a reply asks no one."""
        pairs = {frozenset(("dev", "reviewer"))}
        def holds(body, agent):
            facts = {"comments": [{"body": "older @factory:dev", "at": "1"}, {"body": body, "at": "2"}]}
            with patch("factory_plugins.comments.mentions.neighbours", return_value=pairs), patch("definition.load", return_value={}), \
                    patch("gate.plugin_registry", return_value=gate.plugins.registry({"plugins": ["comments"]})):
                return gate.guard_holds({"mention": agent}, facts)
        self.assertTrue(holds("@factory:dev why X?", "dev"))                                         # a person
        self.assertFalse(holds("@factory:dev-cheap why?", "dev"))                                    # another handle
        self.assertTrue(holds("@factory:dev why? <!-- factory:from reviewer -->", "dev"))           # a neighbour
        self.assertFalse(holds("@factory:approver ok? <!-- factory:from dev -->", "approver"))      # not a neighbour
        self.assertFalse(holds("@factory:reviewer see <!-- factory:from dev reply -->", "reviewer"))  # a reply
        self.assertFalse(holds("no mention", "dev"))
        review = {"reviews": [{"body": "B1 @factory:dev is this intended? <!-- factory:from reviewer -->",
                               "submitted_at": "3"}], "comments": []}
        with patch("factory_plugins.comments.mentions.neighbours", return_value=pairs), patch("definition.load", return_value={}), \
                patch("gate.plugin_registry", return_value=gate.plugins.registry({"plugins": ["comments"]})):
            self.assertTrue(gate.guard_holds({"mention": "dev"}, review))                          # asked in a review

    def test_label_group_holds_one_value(self):
        graph = copy.deepcopy(NODES)
        graph["nodes"]["intake"]["outcomes"]["risky"] = {"effects": [{"label": "risk:high"}, {"stage": "ready"}]}
        tr = MemorySystem({"7": {"stage": "triage", "attrs": ["risk:low", "complexity:small"]}})
        gate.apply("intake", "risky", {"ticket": "7"}, tr, graph)
        attrs = tr.facts("7")["attrs"]
        self.assertIn("risk:high", attrs)
        self.assertNotIn("risk:low", attrs)
        self.assertIn("complexity:small", attrs)   # another group stays

    def test_label_group_holds_one_value_on_pr(self):
        graph = copy.deepcopy(NODES)
        graph["nodes"]["intake"]["outcomes"]["risky"] = {
            "effects": [{"label": "risk:high", "on": "pr"}, {"stage": "ready"}]}
        tr = MemorySystem({"7": {"stage": "triage", "pr_labels": ["risk:low", "complexity:small"]}})
        gate.apply("intake", "risky", {"ticket": "7"}, tr, graph)
        labels = tr.facts("7")["pr_labels"]
        self.assertIn("risk:high", labels)
        self.assertNotIn("risk:low", labels)
        self.assertIn("complexity:small", labels)

    def test_lessons_comment_and_label(self):
        tr = MemorySystem({"7": {"stage": "triage"}})
        gate.apply("intake", "not-actionable", {"ticket": "7", "reason": "[factory] no", "lessons": [
            {"text": "The router  misses ADRs", "scope": "factory", "about": "router", "evidence": "#7"}]}, tr, NODES)
        log = tr.tickets["7"]["log"]
        lesson = [json.loads(v) for k, v in log if k == "comment" and "factory:lesson" in v]
        self.assertEqual(lesson, ["[factory] Lessons from `intake`\n<!-- factory:lesson -->\n"
                                  "- [factory] router: The router misses ADRs (evidence: #7)"])
        self.assertIn("factory:lesson", tr.facts("7")["attrs"])
        with self.assertRaises(gate.Bad):
            gate.apply("intake", "not-actionable", {"ticket": "7", "reason": "r", "lessons": ["just text"]},
                       MemorySystem({"7": {"stage": "triage"}}), NODES)

    def test_apply_sends_no_notification(self):
        """Notifications are workflow nodes: applying an outcome that enters `blocked` loads no sender."""
        gate.apply("implement", "blocked", {"ticket": "7", "reason": "[factory] blocked"},
                   MemorySystem({"7": {"stage": "in-dev"}}), NODES)
        self.assertNotIn("inbox", sys.modules)
        self.assertFalse(hasattr(gate, "notify") or hasattr(gate, "notification"))

    def test_dry_run_sends_nothing(self):
        tr = MemorySystem({7: {"stage": "ready", "since": "2026-11-01T00:00:00Z"}})
        tr.sweep_tickets = lambda stages: [7]
        gate.sweep_stale(tr, NODES, 2, datetime(2026, 11, 1, 12, tzinfo=timezone.utc), dry_run=True)
        gate.sweep(tr, NODES, dry_run=True)
        self.assertEqual(tr.tickets[7]["stage"], "ready")

    def test_plan_change_opens_deploy_ticket(self):
        class PlanTracker(MemorySystem):
            def config_change_merged(self, pr):
                self.deploy_ticket = {"title": "Deploy needed", "pr": pr}
        tr = PlanTracker({10: {"stage": "approved", "pr_state": "merged", "pr": 300}})
        tr.sweep_tickets = lambda stages: [10]
        facts = tr.facts
        tr.facts = lambda ticket: {**facts(ticket), "reviews": [], "fix_rounds": 0}
        gate.sweep(tr, NODES)
        self.assertEqual(tr.deploy_ticket, {"title": "Deploy needed", "pr": 300})

    def test_failed_plan_check_still_moves_the_parent(self):
        class PlanTracker(MemorySystem):
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
        class PlanTracker(MemorySystem):
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
        tracker = MemorySystem({"7": {
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


tests_for.__test__ = False  # a test-class factory, not a test (pytest would collect it)


GateMemory = tests_for(MemorySystem, 7)
GateTag = tests_for(TagTracker, "PROJ-7")


class YouTrackGateTest(unittest.TestCase):
    """One implement step on LME-1701-shaped stub data (fixtures/project-youtrack): the ticket in a stubbed YouTrack,
    its state a `field` store."""
    FIXTURE = Path(__file__).resolve().parent / "fixtures/project-youtrack"

    def setUp(self):
        import project
        from factory_plugins.github.gate import GitHubGateTracker
        from test_youtrack_tracker import FakeYouTrack
        from youtrack_tracker import YouTrackTicketStore
        self.addCleanup(cfg.set_team, cfg.team_root())  # importing the stub set a team of its own
        cfg.set_team(self.FIXTURE)
        self.fake = FakeYouTrack(json.loads((self.FIXTURE / "issue.json").read_text()), tags=("factory:implement-mid", "factory:needs-human"))
        descriptor, self.config, self.nodes = project.load(self.FIXTURE), cfg.load(), nodes.load()
        store = YouTrackTicketStore(self.fake, state_field="State")
        self.tracker = GitHubGateTracker("example/project", self.config["tracker"]["stages"], "factory[bot]",
                                         run=lambda *a, **k: "", project=descriptor, tickets=store)

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

        host = gh_gate.GitHubCodeHost("org/code", run)
        self.assertEqual(host.ticket_for_pr(4), "INFRA-9")
        self.assertEqual(calls, [["api", "repos/org/code/pulls/4"]])

    def _done(self, **facts):
        tr = MemorySystem({"7": {"stage": "changes-requested", "pr": 9, "head": "abc", "ci": "green",
                                   "round_complete": True, "focus_missing": ["full"], **facts}})
        with patch.object(gate, "local_head", return_value="abc"):
            gate.apply("fix", "done", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES)
        return tr.tickets["7"]

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

    def test_apply_puts_automation_focus_in_payload(self):
        class Rec(MemorySystem):
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
            tr = MemorySystem({"7": {"stage": stage, "head": "abc", "round_complete": True, "round_head": "abc", "ci": ci}})
            code, _ = gate.apply(node, "approved", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES)
            return code, tr.tickets["7"]["stage"]
        for node in ("join",):
            self.assertEqual(run(node, "none"), (10, "in-review"), node)  # no CI result yet: no transition
            self.assertEqual(run(node, "pending"), (10, "in-review"), node)  # a new push waits for its CI label
            self.assertEqual(run(node, "green"), (0, "approved"), node)
            self.assertEqual(run(node, "red"), (0, "changes-requested"), node)
        tr = MemorySystem({"7": {"stage": "in-review", "head": "abc", "round_complete": True, "round_head": "abc", "ci": "red"}})
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
        tracker = MemorySystem({7: {"stage": "in-review", "head": "trusted", "risk": "low", "ci": "green",
                                     "pr": 9, "pr_state": "open", "focus_missing": ["full"]}})
        payload = {"eventType": "pull_request.labeled", "data": {"action": "labeled", "number": 9,
                                                                      "head": {"sha": "untrusted"}}}
        path = self.file(payload)
        host = type("Host", (), {"ticket_for_pr": lambda self, number: 7})()
        with patch.object(gh_gate.GitHubCodeHost, "from_config", return_value=host), \
                patch.object(gh_gate.GitHubGateTracker, "from_config", return_value=tracker):
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
        with patch.object(gh_gate.GitHubGateTracker, "from_config", return_value=tracker), \
                patch.object(gh_gate.GitHubCodeHost, "from_config", return_value=host), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = gate.main(["sweep", *args])
        return code, json.loads(stdout.getvalue().splitlines()[0]), stderr.getvalue()

    def test_sweep_invalid_event_warns_and_runs_full_sweep(self):
        from gate_port import MemorySystem
        tracker = MemorySystem({1: {"stage": "in-review"}})
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
        from gate_port import MemorySystem
        tracker = MemorySystem({})
        tracker.sweep_tickets = lambda stages: []
        f = self.file({"action": "closed"})
        code, out, warning = self.run_sweep(["--event", str(f)], tracker)
        self.assertEqual(code, gate.BAD)
        self.assertIn("invalid sweep event", out["error"])

    def test_sweep_event_resolves_ticket_from_tracker_pr_head(self):
        from gate_port import MemorySystem
        tracker = MemorySystem({335: {"stage": "ready"}})
        tracker.sweep_tickets = lambda stages: self.fail("event sweep must not list all tickets")
        host = type("Host", (), {"ticket_for_pr": lambda self, number: 335})()
        f = self.file({"eventType": "pull_request.labeled", "data": {"action": "labeled", "number": 44,
                                                                         "title": "untrusted"}})
        code, out, _ = self.run_sweep(["--event", str(f), "--dry-run"], tracker, host=host)
        self.assertEqual(code, gate.GO)
        self.assertEqual([x["ticket"] for x in out["sweep"]], [335])

    def test_sweep_without_scope_is_rejected_except_stale(self):
        from gate_port import MemorySystem
        tracker = MemorySystem({})
        tracker.sweep_tickets = lambda stages: self.fail("unscoped sweep must be rejected")
        code, out, _ = self.run_sweep([], tracker)
        self.assertEqual(code, gate.BAD)
        self.assertIn("needs --issue or --event", out["error"])

    def test_sweep_reports_skipped_ticket_as_progress_without_changing_result(self):
        from gate_port import MemorySystem
        tracker = MemorySystem({1: {"stage": "ready", "blockers_open": 1}})
        tracker.sweep_tickets = lambda stages: [1]
        code, out, progress = self.run_sweep(["--issue", "1"], tracker)
        self.assertEqual((code, out["sweep"]), (gate.GO, []))
        self.assertEqual(json.loads(progress), {"ticket": 1, "stage": "ready", "outcome": "skipped"})

    def test_sweep_reports_ineligible_dependent_as_progress(self):
        from gate_port import MemorySystem
        tracker = MemorySystem({10: {"blocking": [11]}, 11: {"stage": "in-dev"}})
        tracker.sweep_tickets = lambda stages: []
        code, out, progress = self.run_sweep(["--issue", "10"], tracker)
        self.assertEqual((code, out["sweep"]), (gate.GO, []))
        self.assertEqual(json.loads(progress.splitlines()[-1]), {"ticket": 11, "stage": "in-dev", "outcome": "skipped"})

    def test_sweep_issue_refires_ready_dependent_of_closed_blocker(self):
        from gate_port import MemorySystem
        tracker = MemorySystem({10: {"blocking": [11]}, 11: {"stage": "ready", "blockers_open": 0}})
        tracker.sweep_tickets = lambda stages: []
        code, out, _ = self.run_sweep(["--issue", "10"], tracker)
        self.assertEqual(code, gate.GO)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in out["sweep"]], [(11, "unblocked")])
        self.assertEqual(tracker.tickets[11]["log"][0], ["label", '"factory:ready"'])

    def test_sweep_refires_body_linked_dependent_after_blocker_closes(self):
        from gate_port import MemorySystem
        tracker = MemorySystem({10: {"blocking": [11]}, 11: {"stage": "ready", "blockers_open": 0}})
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
        tr = MemorySystem({7: {"stage": "triage", "head": "abc", "title": "t"}})
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
        tr = MemorySystem({"7": {"stage": "in-review", "head": "abc", "ci": "red", "agent_runs": 8, "run_cap": 8}})
        code, out = gate.apply("join", "changes", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES)
        self.assertEqual((code, tr.tickets["7"]["stage"]), (0, "blocked"))
        self.assertNotIn(["label", '"factory:fix-mid"'], tr.tickets["7"].get("log", []))  # no fix run starts
        tr = MemorySystem({"7": {"stage": "in-review", "head": "abc", "ci": "red", "agent_runs": 7, "run_cap": 8}})
        gate.apply("join", "changes", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES)
        self.assertEqual(tr.tickets["7"]["stage"], "changes-requested")

    def test_fix_round_cap_blocks_the_join_instead_of_a_silent_refusal(self):
        tr = MemorySystem({"7": {"stage": "in-review", "head": "abc", "ci": "red", "fix_rounds": 3, "fix_cap": 3}})
        gate.apply("join", "changes", {"ticket": "7", "event": "e", "tier": "mid"}, tr, NODES)
        self.assertEqual(tr.tickets["7"]["stage"], "blocked")
        self.assertIn(["label", '"factory:needs-human"'], tr.tickets["7"]["log"])
        self.assertEqual(tr.tickets["7"].get("assignees", []), [])

    def test_needs_answers_assigns_nobody(self):
        tr = MemorySystem({"7": {"stage": "triage", "author": "ticket-author"}})
        gate.apply("intake", "grill", {"ticket": "7", "questions": "[factory] Which API?"}, tr, NODES)
        self.assertIn(["label", '"factory:needs-human"'], tr.tickets["7"]["log"])
        self.assertEqual(tr.tickets["7"].get("assignees", []), [])

    def test_label_removed_when_stage_changes(self):
        tr = MemorySystem({"7": {"stage": "blocked", "attrs": ["factory:needs-human"],
                                   "assignees": [CONFIG["notify"]["github_user"]]}})
        gate.apply("human", "ready", {"ticket": "7", "actor": "human"}, tr, NODES)
        self.assertNotIn("factory:needs-human", tr.tickets["7"]["attrs"])
        self.assertEqual(tr.tickets["7"].get("assignees", []), [])

    def test_blocked_ticket_marks_its_pr(self):
        effects = []
        class Capture(MemorySystem):
            def apply(self, effect):
                effects.append(effect.copy())
                return super().apply(effect)
        tr = Capture({"7": {"stage": "in-dev", "pr": 42}})
        gate.apply("implement", "blocked", {"ticket": "7", "reason": "[factory] blocked"}, tr, NODES)
        self.assertIn({"kind": "label", "ticket": "7", "value": "factory:needs-human", "on": "pr"}, effects)
        self.assertEqual([e for e in effects if e["kind"] == "assign"], [])

    def test_resume_clears_pr_mark(self):
        effects = []
        class Capture(MemorySystem):
            def apply(self, effect):
                effects.append(effect.copy())
                return super().apply(effect)
        tr = Capture({"7": {"stage": "blocked", "attrs": ["factory:needs-human"], "pr": 42}})
        gate.apply("human", "ready", {"ticket": "7", "actor": "human"}, tr, NODES)
        self.assertIn({"kind": "unlabel", "ticket": "7", "value": "factory:needs-human", "on": "pr"}, effects)
        self.assertIn({"kind": "unassign", "ticket": "7", "value": None, "on": "pr"}, effects)

    def test_a_stop_clears_every_command_label(self):
        # review of PR #198: a label left on the ticket makes the command after factory:ready a no-op
        seen = []
        class T(MemorySystem):
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

    def test_join_tier_from_complexity_label(self):
        facts = {"round_complete": True, "round_verdict": "changes", "attrs": ["complexity:large"]}
        self.assertEqual(gate.join_decision(facts), ("changes", {"tier": "strong"}))
        self.assertEqual(gate.join_decision({**facts, "attrs": []})[1], {"tier": "mid"})
        self.assertIsNone(gate.join_decision({**facts, "round_complete": False}))


class TriageReadyTest(unittest.TestCase):
    """`factory:triage` runs the router; `factory:ready` starts a developer (the `start` script node)."""

    def sweep(self, ticket, default_tier="mid"):
        tr = MemorySystem({7: ticket})
        tr.sweep_tickets = lambda stages: [7]
        return tr, gate.sweep(tr, NODES, default_tier=default_tier)[0]

    def test_triage_proceed_sets_ready_and_complexity(self):
        tr = MemorySystem({7: {"stage": "triage"}})
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
        tr = MemorySystem({7: {"stage": "triage"}})
        h = {"outcome": "split", "size_estimate": "~900",
             "slices": [{"title": "First"}, {"title": "Second"}]}
        self.assertEqual(gate.outcome_of(h), "split")
        code, _ = gate.apply("intake", "split", {"ticket": 7, **gate.intake_fields(h)}, tr, NODES)
        self.assertEqual((code, tr.tickets[7]["stage"]), (gate.GO, "split"))
        kids = json.loads(next(v for k, v in tr.tickets[7]["log"] if k == "create-issues"))
        self.assertEqual([c["title"] for c in kids], ["First", "Second"])  # GitHubGateTracker creates them `factory:ready`

    def test_stale_triage_is_refired(self):
        tr = MemorySystem({1: {"stage": "triage", "since": "2026-11-01T08:00:00Z"}})
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
        class Tr(MemorySystem):
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
        class Tr(MemorySystem):
            def sweep_tickets(self, stages):
                return [1]
        tr = Tr({1: {"stage": "ready", "since": self.OLD}})
        progress = []
        results, failed = gate.sweep_stale(tr, NODES, 2, self.NOW, progress=progress.append)
        self.assertFalse(failed)
        self.assertEqual(progress, results)

    def test_stale_sweep_reports_fresh_ticket_as_progress_without_changing_result(self):
        class Tr(MemorySystem):
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
        tr = MemorySystem({1: {"stage": "ready", "since": self.OLD}})
        tr.sweep_tickets = lambda stages: [1]
        self.assertEqual(len(gate.sweep_stale(tr, NODES, 2, datetime(2026, 11, 1, 12, tzinfo=timezone.utc), dry_run=True)[0]), 1)
        self.assertNotIn("log", tr.tickets[1])

    def test_idle_unmatched_ticket_flagged_by_recover(self):
        tr, res = self.sweep({3: {"stage": "in-review", "since": self.OLD, "issue_state": "open"}})
        self.assertEqual([r["recover"] for r in res], ["human"])
        self.assertIn(["label", '"factory:needs-human"'], tr.tickets[3]["log"])
        self.assertEqual(tr.tickets[3].get("assignees", []), [])

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
        tr = MemorySystem(tickets)
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
        tr = MemorySystem({7: {"stage": "changes-requested", "attrs": ["factory:update-cheap"]}})
        reason = "[factory] Blocked: merge conflict in factory/gate.py, engine/nodes.yaml"
        code, _ = gate.apply("update", "unresolvable", {"ticket": 7, "event": "e", "reason": reason}, tr, NODES)
        self.assertEqual((code, tr.tickets[7]["stage"]), (0, "blocked"))
        self.assertIn(["comment", json.dumps(reason)], tr.tickets[7]["log"])
        self.assertNotIn("factory:update-cheap", tr.tickets[7]["attrs"])

    def test_update_resolved_removes_label(self):
        tr = MemorySystem({7: {"stage": "changes-requested", "attrs": ["factory:update-cheap"]}})
        code, _ = gate.apply("update", "resolved", {"ticket": 7, "event": "e"}, tr, NODES)
        self.assertEqual((code, tr.tickets[7]["stage"]), (gate.GO, "in-review"))
        self.assertNotIn("factory:update-cheap", tr.tickets[7]["attrs"])


    def test_escalate_labels_next_tier_and_strong_blocks(self):
        for tier, label in (("cheap", "mid"), ("mid", "strong")):
            tr = MemorySystem({7: {"stage": "changes-requested"}})
            code, _ = gate.apply("update", "escalate", {"ticket": 7, "event": "e"}, tr, NODES, tier=tier)
            self.assertEqual(code, 0)
            self.assertIn(["label", json.dumps(f"factory:update-{label}")], tr.tickets[7]["log"])
        tr = MemorySystem({7: {"stage": "changes-requested"}})
        code, _ = gate.apply("update", "escalate", {"ticket": 7, "event": "e", "reason": "[factory] conflicts in a.py"},
                             tr, NODES, tier="strong")
        self.assertEqual((code, tr.tickets[7]["stage"]), (0, "blocked"))
        self.assertIn(["comment", json.dumps("[factory] conflicts in a.py")], tr.tickets[7]["log"])

    def test_deployed_update_spec_escalates_through_its_tiers(self):
        with tempfile.TemporaryDirectory() as d:
            spec = deploy.node_spec("update", NODES["nodes"]["update"], CONFIG, "cheap", None, NODES)
            (Path(d) / "s.json").write_text(json.dumps(spec))
            nodes = gate.spec_nodes(Path(d) / "s.json")
            tr = MemorySystem({7: {"stage": "changes-requested"}})
            code, _ = gate.apply("update", "escalate", {"ticket": 7, "event": "e"}, tr, nodes, tier="cheap")
            self.assertEqual(code, 0)
            self.assertIn(["label", json.dumps("factory:update-mid")], tr.tickets[7]["log"])

    def test_recover_flags_approved_conflict_and_names_conflict_without_ci(self):
        class Tr(MemorySystem):
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
        tr = MemorySystem({7: ticket})
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
        tr = MemorySystem({7: {**self.OPEN, **kw}})
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

    def test_merge_wait_comment_targets_pr(self):
        effects = []
        class Capture(MemorySystem):
            def apply(self, effect):
                effects.append(effect.copy())
                return super().apply(effect)
        tr = Capture({7: dict(self.OPEN)})
        tr.sweep_tickets = lambda stages: [7]
        gate.sweep(tr, NODES, auto_merge={"enabled": False, "max_risk": "high"})
        comment = next(e for e in effects if e["kind"] == "comment")
        self.assertEqual(comment["on"], "pr")

    def test_one_merge_per_sweep(self):
        tr = MemorySystem({7: dict(self.OPEN), 8: dict(self.OPEN)})
        tr.sweep_tickets = lambda stages: [7, 8]
        results, _ = gate.sweep(tr, NODES, auto_merge=self.ON)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in results if r["node"] == "merge"], [(7, "merged")])

    def test_apply_refuses_a_merge_whose_gate_failed(self):
        tr = MemorySystem({7: {**self.OPEN, "ci": "red"}})
        code, out = gate.apply("merge", "merged", {"ticket": 7}, tr, NODES)
        self.assertEqual((code, out["applied"]), (gate.NO_GO, False))


class MergeBlockersTest(unittest.TestCase):
    """`blockers: merge`: work starts on open blockers, the merge waits for them (default `start`: the work waits)."""
    APPROVED = {"stage": "approved", "head": "abc", "pr": 42, "pr_state": "open", "ci": "green", "risk": "low",
                "round_complete": True, "round_verdict": "approve", "round_head": "abc"}
    AUTO = {"enabled": True, "max_risk": "high"}
    MERGE = copy.deepcopy({**NODES, "blockers": "merge"})
    MERGE["nodes"]["start"]["when"] = {"stage": "ready"}  # a merge-mode workflow does not wait at the start

    def sweep(self, tickets, nodes, **kw):
        tr = MemorySystem(tickets)
        tr.sweep_tickets = lambda stages: [7]
        return tr, gate.sweep(tr, nodes, auto_merge=self.AUTO, **kw)[0]

    def test_ready_with_open_blocker_starts_in_merge_mode_and_waits_by_default(self):
        tr, res = self.sweep({7: {"stage": "ready", "blockers_open": 1}}, self.MERGE)
        self.assertEqual([(r["node"], r["outcome"]) for r in res], [("start", "started")])
        self.assertEqual(tr.tickets[7]["stage"], "in-dev")
        tr, res = self.sweep({7: {"stage": "ready", "blockers_open": 1}}, NODES)
        self.assertEqual((res, tr.tickets[7]["stage"]), ([], "ready"))

    def test_start_node_requiring_closed_blockers_still_waits_in_merge_mode(self):
        nodes = copy.deepcopy(self.MERGE)
        nodes["nodes"]["start"]["when"] = {"stage": "ready", "blockers": "closed"}
        tr, res = self.sweep({7: {"stage": "ready", "blockers_open": 1}}, nodes)
        self.assertEqual((res, tr.tickets[7]["stage"]), ([], "ready"))

    def test_approved_with_open_blocker_does_not_merge_until_it_closes(self):
        tr, res = self.sweep({7: {**self.APPROVED, "blockers_open": 1}}, self.MERGE)
        self.assertEqual((res, tr.tickets[7]["stage"]), ([], "approved"))
        tr.tickets[7]["blockers_open"] = 0  # the blocker closed
        res = gate.sweep(tr, self.MERGE, auto_merge=self.AUTO)[0]
        self.assertEqual([(r["node"], r["outcome"]) for r in res], [("merge", "merged")])

    def test_closing_a_blocker_wakes_the_merge_of_its_approved_dependent(self):
        tr = MemorySystem({10: {"blocking": [7]}, 7: {**self.APPROVED, "blockers_open": 0}})
        tr.sweep_tickets = lambda stages: []
        res, failed = gate.sweep(tr, self.MERGE, closed_ticket=10, auto_merge=self.AUTO)
        self.assertFalse(failed)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in res], [(7, "merged")])
        tr = MemorySystem({10: {"blocking": [7]}, 7: {**self.APPROVED, "blockers_open": 0}})
        tr.sweep_tickets = lambda stages: []
        self.assertEqual(gate.sweep(tr, NODES, closed_ticket=10, auto_merge=self.AUTO)[0], [])  # default: no wake

    def test_a_bad_mode_is_refused(self):
        with self.assertRaises(gate.Bad):
            gate.blockers_mode({"blockers": "never"})


class UnblockedSweepTest(unittest.TestCase):
    def sweep(self, tickets, closed=10):
        tr = MemorySystem(tickets)
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
        tr = MemorySystem({
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
        tr = MemorySystem({10: {"stage": "changes-requested", "pr_state": "merged"}})
        tr.sweep_tickets = lambda stages: [10]
        results, failed = gate.sweep(tr, NODES)
        self.assertFalse(failed)
        self.assertEqual([(r["node"], r["outcome"]) for r in results], [("finish", "merged")])
        self.assertEqual(tr.tickets[10]["stage"], "done")

    def test_sweep_finishes_person_merged_pr_from_blocked_and_drops_needs_human(self):
        tr = MemorySystem({10: {"stage": "blocked", "pr_state": "merged",
                                 "attrs": ["factory:needs-human"]}})
        tr.sweep_tickets = lambda stages: [10]
        results, failed = gate.sweep(tr, NODES)
        self.assertFalse(failed)
        self.assertEqual([(r["node"], r["outcome"]) for r in results], [("finish", "merged")])
        self.assertEqual(tr.tickets[10]["stage"], "done")
        self.assertNotIn("factory:needs-human", tr.tickets[10]["attrs"])

    def test_config_change_merge_updates_open_prs(self):
        class ConfigTracker(MemorySystem):
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
        class BrokenBlockingTracker(MemorySystem):
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
        tr = MemorySystem({11: {"stage": "ready", "blockers_open": 0}})
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
        tr = MemorySystem({11: {"stage": "ready", "blockers_open": 0}})
        tr.sweep_tickets = lambda stages: []
        tr.blocking = lambda ticket: [11, 11]

        results, failed = gate.sweep(tr, NODES, closed_ticket=10)

        self.assertFalse(failed)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in results], [(11, "unblocked")])


class RecalibratedBlocksTest(unittest.TestCase):
    def test_env_broken_parks_ticket_behind_main_red_ticket(self):
        tr = MemorySystem({"7": {"stage": "in-dev", "attrs": ["factory:implement-mid"]},
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
        tr = MemorySystem({"7": {"stage": "changes-requested", "pr": 3, "round_complete": True,
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
        tr = MemorySystem({"7": {"stage": "in-review", "reviews": [
            review("a", []), review("b", ["M1"]), review("c", ["B1", "M1"])]}})
        self.assertEqual(tr.facts("7")["fix_rounds"], 1)

    def test_grill_includes_proposed_checks(self):
        fields = gate.intake_fields({
            "handoff": "ROUTER-INTAKE", "ready": "no", "missing": ["a check says 'clean'"], "split": "none",
            "size_estimate": "~40",
            "proposed_checks": ["`uv run factory/tests/test_gate.py` passes", "no match: `rg TODO factory/`"]})
        self.assertIn("- `uv run factory/tests/test_gate.py` passes", fields["questions"])
        self.assertIn("- no match: `rg TODO factory/`", fields["questions"])
        tr = MemorySystem({"7": {"stage": "triage"}})
        gate.apply("intake", "grill", {"ticket": "7", **fields}, tr, NODES)
        self.assertEqual(tr.tickets["7"]["stage"], "needs-answers")


class MultiRepositoryTest(unittest.TestCase):
    """One ticket with a PR in each of two repositories (ADR-0017): the PR facts combine."""
    ON = {"enabled": True, "max_risk": "high"}

    def pr(self, repo, head, **kw):
        return {"repo": repo, "pr": len(repo), "head": head, "branch": "ez/7-x", "pr_state": "open", "ci": "green",
                "round_complete": True, "round_verdict": "approve", "round_head": head, "mergeable": True, **kw}

    def facts(self, *prs, stage="approved"):
        return MemorySystem({7: {"stage": stage, "risk": "low", "prs": list(prs)}}).facts(7)

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

    def test_merged_pr_ci_does_not_override_open_pr_ci(self):
        facts = self.facts(self.pr("a", "h1", pr_state="merged", ci="none"), self.pr("b", "h2"))
        self.assertEqual(facts["ci"], "green")

    def test_merged_pr_approval_does_not_complete_open_review_round(self):
        facts = self.facts(self.pr("a", "h1", pr_state="merged"), self.pr(
            "b", "h2", round_complete=False, round_verdict=None, round_head=None), stage="in-review")
        self.assertFalse(facts["round_complete"])

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
        tr = MemorySystem({7: {"stage": "approved", "risk": "low", "prs": [self.pr("a", "h1"), self.pr("b", "h2")]}})
        tr.sweep_tickets = lambda stages: [7]
        gate.sweep(tr, NODES, auto_merge=self.ON)
        merged = [json.loads(v) for k, v in tr.tickets[7]["log"] if k == "merge"]
        self.assertEqual(merged, [{"repo": "b", "head": "h2"}, {"repo": "a", "head": "h1"}])
        self.assertEqual(tr.tickets[7]["stage"], "approved")  # the primary's merge event runs `finish`
        gate.sweep(tr, NODES, auto_merge=self.ON)
        self.assertEqual(tr.tickets[7]["stage"], "done")

    def test_a_partly_merged_ticket_merges_only_the_rest_on_the_next_sweep(self):
        tr = MemorySystem({7: {"stage": "approved", "risk": "low",
                                "prs": [self.pr("a", "h1"), self.pr("b", "h2", pr_state="merged")]}})
        tr.sweep_tickets = lambda stages: [7]
        gate.sweep(tr, NODES, auto_merge=self.ON)
        merged = [json.loads(v) for k, v in tr.tickets[7]["log"] if k == "merge"]
        self.assertEqual(merged, [{"repo": "a", "head": "h1"}])

    def test_ticket_for_pr_finds_single_and_multi_repository_tickets(self):
        tr = MemorySystem({7: {"pr": 9}, 8: {"prs": [self.pr("a", "h1"), self.pr("bb", "h2")]}})
        self.assertEqual([tr.ticket_for_pr(9), tr.ticket_for_pr(9, "o/primary")], [7, 7])
        self.assertEqual([tr.ticket_for_pr(2, "bb"), tr.ticket_for_pr(2), tr.ticket_for_pr(2, "a")], [8, 8, None])

    def test_check_lists_checkouts_and_refuses_a_stale_head_in_any_repository(self):
        tr = MemorySystem({7: {"stage": "approved", "risk": "low",
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
        tr = MemorySystem({7: {"stage": "triage"}})
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
        repos = gh_ci_sync.project_repositories({"repositories": [
            {"name": "main", "url": "https://github.com/o/main", "role": "primary"},
            {"name": "app", "url": "https://github.com/o/app", "role": "project"},
            {"name": "web", "url": "https://github.com/o/web/", "role": "project"}]})
        self.assertEqual(repos, ["o/app", "o/web"])
        host = self.host()
        results, failed = gh_ci_sync.ci_sync(host, repos)
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
        gh_ci_sync.ci_sync(host, repos)
        self.assertEqual(len(host.edits), 3)  # a second pass writes nothing

    def test_sync_dry_run_writes_nothing_and_one_failing_repository_does_not_stop_the_others(self):
        host = self.host()
        results, failed = gh_ci_sync.ci_sync(host, ["o/missing", "o/app"], dry_run=True)
        self.assertTrue(failed)
        self.assertEqual((results[0]["repo"], results[1]["added"], host.edits), ("o/missing", [self.GREEN], []))


class StatusTests(unittest.TestCase):
    def review(self, body, head="a", verdict="changes", rnd=1, focus="behaviour"):
        return {"agent": True, "verdict": verdict, "commit_id": head, "focus": focus, "round": rnd, "body": body}

    def test_open_lists_b1_and_drops_it_once_a_later_review_resolves_it(self):
        first = self.review("[factory] x\n\n**B1** a.py:3 — crashes on empty input\nmore\n\n<!-- m -->")
        facts = {"reviews": [first], "stage": "changes-requested", "pr": 9, "head": "a", "ci": "green", "fix_rounds": 1, "fix_cap": 3}
        st = gate.status(facts)
        self.assertEqual(st["reviews"]["behaviour"]["open"], [{"id": "B1", "text": "a.py:3 — crashes on empty input"}])
        self.assertEqual((st["stage"], st["pr"], st["fix_rounds"], st["fix_cap"]), ("changes-requested", 9, 1, 3))
        kept = gate.status({**facts, "reviews": [first, self.review("B1 is not resolved yet.", "b", rnd=2)]})
        self.assertEqual([f["id"] for f in kept["reviews"]["behaviour"]["open"]], ["B1"])
        later = self.review("[factory] B1 is resolved.", "b", verdict="approve", rnd=2)
        st = gate.status({**facts, "reviews": [first, later]})
        self.assertEqual(st["reviews"]["behaviour"], {"round": 2, "verdict": "approve", "head": "b", "open": []})

    def test_status_is_short_and_names_failing_ci(self):
        body = "\n".join(f"**B{i}** f.py:{i} — " + "x" * 400 for i in range(1, 9))
        st = gate.status({"reviews": [self.review(body)], "ci": "red", "ci_failure": {"job": "lint", "step": "ruff", "log": "l"}})
        self.assertEqual(st["ci"], {"state": "red", "failing": ["lint"]})
        self.assertLess(len(json.dumps(st)), 2000)

    def test_status_stays_under_the_limit_with_many_open_findings(self):
        body = "\n".join(f"**B{i}** f.py:{i} — issue" for i in range(1, 101))
        st = gate.status({"reviews": [self.review(body)]})
        review = st["reviews"]["behaviour"]
        self.assertLess(len(json.dumps(st)), 2000)
        self.assertEqual(len(review["open"]) + review["omitted"], 100)

    def test_begin_of_a_fix_run_includes_status(self):
        tracker = MemorySystem({7: {"stage": "changes-requested", "head": "abc", "pr": 9, "pr_state": "open", "ci": "green",
                                     "round_complete": True, "fix_rounds": 0, "fix_cap": 3,
                                     "reviews": [self.review("**B1** a.py:1 — bad", "abc")]}})
        payload = {"eventType": "pull_request.labeled", "data": {"action": "labeled", "number": 9}}
        path = Path(tempfile.mkdtemp()) / "e.json"
        path.write_text(json.dumps(payload))
        host = type("Host", (), {"ticket_for_pr": lambda self, number: 7})()
        out = StringIO()
        with patch.object(gh_gate.GitHubCodeHost, "from_config", return_value=host), \
                patch.object(gh_gate.GitHubGateTracker, "from_config", return_value=tracker), redirect_stdout(out):
            gate.main(["check", "--node", "fix", "--event", str(path)])
        st = json.loads(out.getvalue())["status"]
        self.assertEqual(st["reviews"]["behaviour"]["open"][0]["id"], "B1")
        self.assertEqual(st["stage"], "changes-requested")


class DefinitionDeliveryTest(unittest.TestCase):
    """The delivery scenarios of the removed name-keyed tests, on the definition fixture (ADR-0021): each outcome of
    `fixtures/v2/workflow.yaml` does what its effects say, and the gate refuses it when its guards do not hold."""

    @classmethod
    def setUpClass(cls):
        cls.previous = cfg.team_root()
        cfg.set_team(Path(__file__).resolve().parent / "fixtures/v2")
        cls.graph, cls.config = nodes.load(), cfg.load()

    @classmethod
    def tearDownClass(cls):
        cfg.set_team(cls.previous)

    def apply(self, node, outcome, facts, **payload):
        tr = MemorySystem({"7": {"head": "abc", "ci": "green", "pr": 9, "pr_state": "open", **facts}})
        status, out = gate.apply(node, outcome, {"ticket": "7", "event": "e", **payload}, tr, self.graph)
        return tr, status, out

    def log(self, tr, kind):
        return [json.loads(v) for k, v in tr.tickets["7"].get("log", []) if k == kind]

    def test_implement_done_starts_the_review_its_risk_needs(self):
        for outcome, risk in (("done-low", "low"), ("done-high", "high")):
            with self.subTest(outcome=outcome):
                tr, status, _ = self.apply("implement-large", outcome, {"stage": "in-dev", "attrs": ["factory:implement-large"]})
                t = tr.tickets["7"]
                self.assertEqual((status, t["stage"]), (gate.GO, "in-review"))
                self.assertEqual(t["pr_labels"], [f"factory:review-{risk}"])  # the review waits for CI itself
                self.assertIn(f"risk:{risk}", t["attrs"])
                self.assertNotIn("factory:implement-large", t["attrs"])

    def test_fix_done_returns_to_review_and_removes_its_label(self):
        for stage in ("changes-requested", "in-review", "approved"):
            with self.subTest(stage=stage):
                tr, status, _ = self.apply("fix", "done-high", {"stage": stage, "attrs": ["factory:fix"], "pr_labels": ["factory:fix"]})
                t = tr.tickets["7"]
                self.assertEqual((status, t["stage"]), (gate.GO, "in-review"))
                self.assertIn("factory:review-high", t["pr_labels"])
                self.assertNotIn("factory:fix", t["attrs"])

    def test_fix_starts_only_below_the_round_cap_from_a_reviewable_stage(self):
        event = {"id": "e", "ticket": "7"}
        for facts, go in (({"stage": "changes-requested"}, True),
                          ({"stage": "changes-requested", "fix_rounds": 2}, True),
                          ({"stage": "changes-requested", "fix_rounds": 3}, False),  # the cap is 3
                          ({"stage": "in-dev"}, False)):
            with self.subTest(facts=facts):
                tr = MemorySystem({"7": {"head": "abc", "pr": 9, **facts}})
                code, out = gate.check("fix", event, tr, self.graph, self.config)
                self.assertEqual((code == gate.GO, out["merge_main"]), (go, False))

    def test_fix_done_is_refused_for_a_seen_event_or_after_the_stage_moved(self):
        tr, status, out = self.apply("fix", "done-low", {"stage": "changes-requested", "seen": ["e"]})
        self.assertEqual((status, out["reason"]), (gate.NO_GO, "duplicate event"))
        tr, status, out = self.apply("fix", "done-low", {"stage": "in-dev"})
        self.assertEqual((status, tr.tickets["7"]["stage"]), (gate.NO_GO, "in-dev"))

    def test_conflict_goes_to_the_update_node_and_resolved_goes_to_automerge(self):
        tr, status, _ = self.apply("merge", "conflict", {"stage": "approved", "conflict": True, "attrs": ["factory:merge"]})
        t = tr.tickets["7"]
        self.assertEqual((status, t["stage"]), (gate.GO, "changes-requested"))
        self.assertEqual(t["pr_labels"], ["factory:update"])
        self.assertNotIn("factory:merge", t["attrs"])
        tr, status, _ = self.apply("update", "resolved", {"stage": "changes-requested", "attrs": ["factory:update"]})
        self.assertEqual((status, tr.tickets["7"]["stage"], tr.tickets["7"]["pr_labels"]), (gate.GO, "approved", ["factory:automerge"]))
        tr, status, _ = self.apply("update", "resolved", {"stage": "approved"})  # not a conflict round
        self.assertEqual((status, tr.tickets["7"]["stage"]), (gate.NO_GO, "approved"))

    def test_merge_node_merges_an_approved_head_and_does_not_update_without_a_conflict(self):
        approved = {"stage": "approved", "reviews": [{"agent": True, "commit_id": "abc", "verdict": "approve"}]}
        tr = MemorySystem({"7": {"head": "abc", "ci": "green", "pr": 9, "pr_state": "open", **approved}})
        status, out = gate.decide("merge", "7", tr, self.graph)
        self.assertEqual((out["outcome"], self.log(tr, "merge")), ("merged", ["match-head"]))
        tr = MemorySystem({"7": {"head": "abc", "ci": "pending", "pr": 9, "pr_state": "open", **approved}})
        self.assertEqual(gate.decide("merge", "7", tr, self.graph)[1]["outcome"], "waiting")
        self.assertEqual(self.log(tr, "merge"), [])

    def test_automerge_waits_for_a_person_above_low_risk(self):
        tr, status, _ = self.apply("automerge", "merge", {"stage": "approved", "attrs": ["risk:low", "factory:automerge"]})
        t = tr.tickets["7"]
        self.assertEqual((status, t["stage"], t["pr_labels"]), (gate.GO, "approved", ["factory:merge"]))
        self.assertNotIn("factory:needs-human", t["attrs"])
        tr, status, _ = self.apply("automerge", "merge", {"stage": "approved", "attrs": ["risk:high", "factory:automerge"]},
                                   reason="[factory] merge PR 9")
        t = tr.tickets["7"]
        self.assertEqual((status, t["stage"], t.get("pr_labels", [])), (gate.GO, "approved", []))
        self.assertIn("factory:needs-human", t["attrs"])
        self.assertIn("[factory] merge PR 9", self.log(tr, "comment")[0])
        self.assertNotIn("factory:automerge", t["attrs"])

    def test_blocked_stops_the_ticket_with_a_comment_for_the_owner(self):
        tr, status, _ = self.apply("implement-small", "blocked", {"stage": "in-dev"}, reason="[factory] stuck")
        t = tr.tickets["7"]
        self.assertEqual((status, t["stage"]), (gate.GO, "blocked"))
        self.assertIn("[factory] stuck", self.log(tr, "comment")[0])
        self.assertEqual(t.get("assignees", []), [])

    def test_review_changes_asks_for_a_fix_and_a_moved_head_is_refused(self):
        tr, status, _ = self.apply("review-high", "changes", {"stage": "in-review"}, head="old")
        self.assertEqual((status, tr.tickets["7"]["stage"]), (gate.NO_GO, "in-review"))  # the head moved since the review began
        tr, status, _ = self.apply("review-high", "changes", {"stage": "in-review"}, findings=[{"id": "B1"}])
        t = tr.tickets["7"]
        self.assertEqual((status, t["stage"], t["pr_labels"]), (gate.GO, "changes-requested", ["factory:fix"]))
        self.assertEqual(self.log(tr, "review"), ["request-changes-at-head"])

    def test_finish_waits_until_the_pr_is_merged(self):
        open_pr = MemorySystem({"7": {"stage": "approved", "pr_state": "open"}})
        self.assertEqual(gate.decide("finish", "7", open_pr, self.graph)[1]["outcome"], "waiting")
        self.assertEqual(open_pr.tickets["7"]["stage"], "approved")
        merged = MemorySystem({"7": {"stage": "approved", "pr_state": "merged"}})
        self.assertEqual(gate.decide("finish", "7", merged, self.graph)[1]["outcome"], "done")
        self.assertEqual(merged.tickets["7"]["stage"], "done")



class ReviewLoopTest(unittest.TestCase):
    """#1048: a re-review may not hold a PR on findings the earlier round should have raised; the fix cap counts
    only rounds whose findings persist."""

    class Host:
        def __init__(self, changed):
            self.changed = changed

        def changed_since(self, ticket, old):
            return self.changed

    REQUEST = [{"kind": "review", "value": "request-changes-at-head", "ticket": "7"}]

    def facts(self, *bodies):
        reviews = [{"agent": True, "focus": "low", "commit_id": f"h{i}", "verdict": "changes",
                    "findings": ["B1"], "body": b} for i, b in enumerate(bodies)]
        return {"head": "h9", "reviews": reviews}

    def payload(self, *findings):
        return {"findings": [{"id": i, "file": f, "finding": "x", "severity": "blocking", "case": "c", **extra}
                             for i, f, extra in findings]}

    def scope(self, payload, facts, changed):
        return gate.rereview_scope(self.REQUEST, payload, facts, "low", self.Host(changed), "7")

    def test_rereview_new_blocking_on_unchanged_file_is_refused(self):
        why = self.scope(self.payload(("B2", "factory/a.py:3", {})), self.facts("**B1** x"), {"factory/b.py": None})
        self.assertIn("re-review scope: B2 (factory/a.py)", why)
        self.assertIn("unseen", why)

    def test_rereview_blocking_on_changed_file_or_open_id_or_unseen_is_accepted(self):
        facts = self.facts("**B1** x")
        for finding in [("B2", "factory/b.py:3", {}), ("B1", "factory/a.py:3", {}),
                        ("B3", "factory/a.py:3", {"unseen": "only reachable after the fix's new branch"})]:
            with self.subTest(finding=finding):
                self.assertIsNone(self.scope(self.payload(finding), facts, {"factory/b.py": None}))

    def test_rereview_scope_is_the_fix_hunks_when_the_line_is_known(self):
        facts, changed = self.facts("**B1** x"), {"factory/a.py": [(10, 20)]}
        self.assertIsNone(self.scope(self.payload(("B2", "factory/a.py:22", {})), facts, changed))  # margin
        self.assertIsNone(self.scope(self.payload(("B2", "factory/a.py", {})), facts, changed))  # no line: the file
        self.assertIn("B2", self.scope(self.payload(("B2", "factory/a.py:140", {})), facts, changed))

    def test_first_review_is_not_scoped(self):
        self.assertIsNone(self.scope(self.payload(("B1", "factory/a.py:3", {})), {"head": "h9", "reviews": []}, {}))

    def test_resolved_earlier_id_does_not_cover_a_new_finding(self):
        facts = self.facts("**B1** x", "B1 resolved\n\n**B2** y")
        facts["reviews"][1]["findings"] = ["B2"]
        self.assertIn("B1", self.scope(self.payload(("B1", "factory/a.py:3", {})), facts, {}))

    def test_uncomparable_patches_skip_the_check(self):
        self.assertIsNone(self.scope(self.payload(("B2", "factory/a.py:3", {})), self.facts("**B1** x"), None))

    def test_approve_is_not_scoped(self):
        approve = [{"kind": "review", "value": "approve-at-head", "ticket": "7"}]
        self.assertIsNone(gate.rereview_scope(approve, self.payload(("B2", "a.py", {})), self.facts("**B1** x"), "low",
                                              self.Host({}), "7"))

    @staticmethod
    def rounds(*ids):
        return [{"agent": True, "verdict": "changes", "commit_id": f"h{i}", "submitted_at": f"2026-11-08T0{i}:00:00Z",
                 "findings": list(f)} for i, f in enumerate(ids)]

    def test_fresh_findings_each_round_do_not_reach_the_cap(self):
        import gate_port
        self.assertEqual(gate_port.fix_round_count(self.rounds(["B1"], ["B2"], ["B3"], ["B4"])), 2)  # backstop: half

    def test_persisting_finding_counts_every_round(self):
        import gate_port
        self.assertEqual(gate_port.fix_round_count(self.rounds(["B1"], ["B1", "B2"], ["B2"])), 3)

if __name__ == "__main__":
    unittest.main()

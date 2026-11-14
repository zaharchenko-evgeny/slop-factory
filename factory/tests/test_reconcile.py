#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/reconcile.py over MemorySystem. Run: uv run factory/tests/test_reconcile.py"""
import copy
import json
import sys
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
cfg.set_team(Path(__file__).resolve().parent / "fixtures/slop")
import nodes as nodes_mod  # noqa: E402
import definition  # noqa: E402
import reconcile  # noqa: E402
import gate  # noqa: E402
from gate_port import MemorySystem  # noqa: E402
from tag import tagged  # noqa: E402
from factory_plugins.github import heartbeat  # noqa: E402

NODES, CONFIG = nodes_mod.load(), cfg.load()
STAGES, LIMITS = list(CONFIG["tracker"]["stages"]), CONFIG["limits"]
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
OLD, FRESH = "2026-11-03T11:00:00Z", "2026-11-03T11:50:00Z"


def logged(tracker, kind):
    return [v for k, v in tracker.tickets[1].get("log", []) if k == kind]


class FinishLostTest(unittest.TestCase):
    """GitHub closes a ticket on merge; reconcile finishes one whose `finish` run was lost."""

    def finish(self, ticket):
        tracker = MemorySystem({1: ticket})
        results = reconcile.finish_lost(tracker, definition.load(RecoverDefinitionTest.SLOP), [s for s in reconcile.LOST_FINISH if s in STAGES],
                                        tracker.sweep_tickets(STAGES, open_only=True), False, None)
        return tracker, results

    def test_closed_approved_ticket_with_merged_pr_is_finished(self):
        t, results = self.finish({"stage": "approved", "pr": 5, "pr_state": "merged", "issue_state": "closed", "since": OLD})
        self.assertEqual([(r["ticket"], r["reconcile"]) for r in results], [(1, "finish")])
        self.assertEqual(t.tickets[1]["stage"], "done")

    def test_disabled_merged_listeners_are_skipped(self):
        ticket = {"stage": "approved", "pr": 5, "pr_state": "merged", "issue_state": "closed", "since": OLD}
        base = definition.load(RecoverDefinitionTest.SLOP)
        nodes = base["workflow"]["nodes"]
        name, node = next((k, v) for k, v in nodes.items()
                          if any(isinstance(t, dict) and t.get("pr") == "merged" for t in v.get("triggers") or []))
        for label, active_too, stage in (("disabled listener first", True, "done"),
                                         ("only a disabled listener", False, "approved")):
            with self.subTest(label):
                defn = copy.deepcopy(base)
                new = {"disabled-" + name: {**copy.deepcopy(node), "enabled": False}}
                if active_too:
                    new[name] = defn["workflow"]["nodes"][name]
                rest = {k: v for k, v in defn["workflow"]["nodes"].items() if k != name}
                defn["workflow"]["nodes"] = {**new, **rest}
                tracker = MemorySystem({1: dict(ticket)})
                results = reconcile.finish_lost(tracker, defn, [s for s in reconcile.LOST_FINISH if s in STAGES],
                                                tracker.sweep_tickets(STAGES, open_only=True), False, None)
                self.assertEqual(tracker.tickets[1]["stage"], stage, results)
                self.assertFalse([r for r in results if "error" in r], results)

    def test_other_closed_tickets_are_left_alone(self):
        for ticket in ({"stage": "done", "pr": 5, "pr_state": "merged", "issue_state": "closed"},
                       {"stage": "approved", "pr": 5, "pr_state": "open", "issue_state": "closed"},
                       {"stage": "approved", "pr": 5, "pr_state": "merged"}):  # still open: the sweep's job
            with self.subTest(ticket=ticket):
                t, results = self.finish({**ticket, "since": OLD})
                self.assertEqual((results, t.tickets[1]["stage"]), ([], ticket["stage"]))


class WakeReadyTest(unittest.TestCase):
    def test_refires_only_unblocked_ready_tickets_that_waited(self):
        from datetime import datetime, timezone
        old, new = "2026-11-06T10:00:00Z", "2026-11-06T11:55:00Z"
        tr = MemorySystem({"1": {"stage": "ready", "stage_at": old}, "2": {"stage": "ready", "stage_at": old, "blockers_open": 1},
                            "3": {"stage": "ready", "stage_at": new}, "4": {"stage": "in-dev", "stage_at": old},
                            "5": {"stage": "ready", "stage_at": old}})
        had = {"1": [{"number": 9, "state": "closed"}], "2": [{"number": 8, "state": "open"}], "3": [{"number": 9, "state": "closed"}]}
        tr.blocked_by = lambda n: had.get(str(n), [])
        now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        got = reconcile.wake_ready(tr, 30, now=now)
        self.assertEqual([r["ticket"] for r in got], ["1"])   # 5 never had a blocker: left alone


def ticket_repositories_tracker(staged, stages=None):
    """A GitHub gate tracker over tickets in `o/r` (the tracker) and `o/b`, whose stage search finds `staged` ({repo:
    numbers}); the caller replaces the facts and the writes."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from factory_plugins.github.gate import GitHubGateTracker
    team = cfg._team
    from test_gate import TicketReposGh  # importing test_gate sets its own team; keep this module's
    cfg.set_team(team)
    return GitHubGateTracker("o/r", stages or CONFIG["tracker"]["stages"], "bot", run=TicketReposGh(staged=staged), ticket_repos=["o/r", "o/b"])


class WakeReadyTicketRepositoriesTest(unittest.TestCase):
    def test_wake_ready_scans_every_ticket_repository(self):
        old = "2026-11-06T10:00:00Z"
        tr = ticket_repositories_tracker({"o/r": [1], "o/b": [2, 3]})
        tr.stale_facts = lambda n: {"stage": "ready", "stage_at": old, "blockers_open": 1 if n == "o/b#3" else 0}
        tr.blocked_by = lambda n: [{"number": 9, "state": "closed"}]
        written = []
        tr.apply = written.append
        now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
        got = reconcile.wake_ready(tr, 30, now=now)
        self.assertEqual([r["ticket"] for r in got], [1, "o/b#2"])  # o/b#3 still has an open blocker
        self.assertEqual([(e["kind"], e["ticket"]) for e in written],
                         [("unlabel", 1), ("label", 1), ("unlabel", "o/b#2"), ("label", "o/b#2")])


class CiSyncTicketRepositoriesTest(unittest.TestCase):
    def test_ci_sync_covers_every_ticket_repository(self):
        from unittest.mock import patch
        from factory_plugins.github import ci_sync
        for tracker, want in [({"repositories": ["o/r", "o/b", "https://github.com/o/c.git"]}, ["o/r", "o/b", "o/c"]),
                              ({"repository": "o/r", "repositories": ["o/r", "o/b"]}, ["o/r", "o/b"]),
                              ({"repository": "o/r"}, ["o/r"])]:
            with self.subTest(tracker=tracker):
                base = {k: v for k, v in CONFIG["tracker"].items() if k not in ("repository", "repositories")}
                config = {**CONFIG, "tracker": {**base, **tracker}, "repositories": [
                    {"name": "app", "url": "https://github.com/o/app", "role": "project"},
                    {"name": "r", "url": "https://github.com/o/r", "role": "project"}]}
                with patch.object(ci_sync.config_mod, "load", return_value=config), \
                        patch.object(ci_sync.GitHubCodeHost, "from_config", return_value=object()), \
                        patch.object(ci_sync, "ci_sync", return_value=([], False)) as run:
                    ci_sync.run_ci_sync()
                self.assertEqual(run.call_args.args[1], ["o/app", *want])


class WakeApprovedTest(unittest.TestCase):
    """`blockers: merge`: an approved ticket whose last blocker closed gets its merge label re-fired."""
    NODES = {"blockers": "merge", "nodes": {"merge": {
        "role": "approver", "triggers": ["pull_request.labeled factory:automerge"],
        "when": {"stage": "approved", "blockers": "closed"}, "outcomes": {}}}}
    NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)

    def wake(self, nodes=None, blockers_open=0):
        tr = MemorySystem({1: {"stage": "approved", "blockers_open": blockers_open, "pr": 5, "pr_state": "open",
                                "label_at": {"factory:automerge": "2026-11-06T10:00:00Z"}}})
        tr.blocked_by = lambda n: [{"number": 9, "state": "closed"}]
        return tr, reconcile.wake_approved(tr, nodes or self.NODES, 30, now=self.NOW)

    def test_refires_the_merge_label_once_the_last_blocker_closed(self):
        tr, got = self.wake()
        self.assertEqual([(r["ticket"], r["node"], r["label"]) for r in got], [(1, "merge", "factory:automerge")])
        self.assertIn("factory:automerge", [v.strip('"') for k, v in tr.tickets[1]["log"] if k == "label"])

    def test_waits_for_an_open_blocker_and_ignores_the_default_mode(self):
        self.assertEqual(self.wake(blockers_open=1)[1], [])
        self.assertEqual(self.wake({**self.NODES, "blockers": "start"})[1], [])


class ApprovalGraphTest(unittest.TestCase):
    """`wake_approved` reads the team definition, not the engine node file."""
    SLOP = Path(__file__).resolve().parent / "fixtures/slop"

    def graph(self, blockers, held):
        defn = copy.deepcopy(definition.load(self.SLOP))
        if blockers:
            defn["workflow"]["blockers"] = blockers
        if held:
            defn["workflow"]["nodes"]["merge"]["when"] = {"stage": "approved", "blockers": "closed"}
        return reconcile.approval_graph(defn)

    def test_blockers_mode_and_merge_node_come_from_the_definition(self):
        for blockers, held, want in (("merge", True, [("merge", "factory:merge")]), ("merge", False, []),
                                     ("start", True, []), (None, True, [])):
            with self.subTest(blockers=blockers, held=held):
                graph = self.graph(blockers, held)
                self.assertEqual(graph["blockers"], blockers or "start")
                self.assertEqual([(n, label) for n, label, _ in reconcile.merge_labels(graph)],
                                 [("merge", "factory:merge")] if held else [])
                tr = MemorySystem({1: {"stage": "approved", "blockers_open": 0, "pr": 5, "pr_state": "open",
                                        "label_at": {"factory:merge": "2026-11-06T10:00:00Z"}}})
                tr.blocked_by = lambda n: [{"number": 9, "state": "closed"}]
                got = reconcile.wake_approved(tr, graph, 30, now=WakeApprovedTest.NOW)
                self.assertEqual([(r["node"], r["label"]) for r in got], want)


class RecoverDefinitionTest(unittest.TestCase):
    """ADR-0021 team (fixtures/slop): the lost wakes come from workflow.yaml."""
    SLOP = Path(__file__).resolve().parent / "fixtures/slop"

    @classmethod
    def setUpClass(cls):
        cls.previous = cfg._team
        cfg.set_team(cls.SLOP)
        import definition
        cls.defn, cls.config = definition.load(cls.SLOP), cfg.load()

    @classmethod
    def tearDownClass(cls):
        cfg.set_team(cls.previous)

    def recover(self, ticket):
        tr = MemorySystem({1: ticket})
        tracker = self.config["tracker"]
        results = reconcile.recover_definition(tr, self.defn, list(tracker["stages"]), tracker.get("stage_kinds", {}),
                                               self.config["limits"], now=NOW)
        return tr, results

    def pr(self, **kw):
        return {"stage": "in-review", "pr": 5, "pr_state": "open", "head": "abc123def4567", "since": OLD,
                "pr_labels": [], **kw}

    def late_review(self, **kw):  # #1263: the project's own review automation gives the second verdict
        import copy
        defn = copy.deepcopy(self.defn)
        defn["workflow"]["nodes"]["automerge"]["settings"] = {
            "external_review_hours": 2, "external_review_bot": "jetbrains-air[bot]",
            "external_review_label": "factory:project-review"}
        ticket = self.pr(**{"stage": "approved", "ci": "green", "attrs": ["risk:low"],
                            "pr_labels": ["factory:automerge", "factory:project-review"],
                            "label_at": {"factory:project-review": "2026-11-03T09:00:00Z"}, **kw})  # 3 hours ago
        tr = MemorySystem({1: ticket})
        tracker = self.config["tracker"]
        return ticket, tr, reconcile.recover_definition(tr, defn, list(tracker["stages"]), tracker.get("stage_kinds", {}),
                                                        self.config["limits"], now=NOW)

    def test_late_external_review_wakes_the_approver_once_per_head(self):
        ticket, t, results = self.late_review()
        self.assertEqual([(r["node"], r["reconcile"]) for r in results], [("automerge", "external-review")])
        self.assertEqual(logged(t, "label"), ['"factory:automerge"'])  # removed, then added again
        note = logged(t, "comment")[0]
        self.assertIn("head=abc123def456 external review late, re-woke factory:automerge", note)
        self.assertEqual(t.get_stage(t.tickets[1]), "approved")
        _, t, results = self.late_review(log=[("comment", note)])
        self.assertNotIn("external-review", [r["reconcile"] for r in results])  # once per head

    def test_bot_review_at_the_head_or_a_fresh_label_is_left_alone(self):
        bot = {"author": "jetbrains-air[bot]", "commit_id": "abc123def4567", "agent": False, "bot": True}
        self.assertEqual(self.late_review(reviews=[bot])[2], [])
        _, _, results = self.late_review(label_at={"factory:project-review": "2026-11-03T10:30:00Z"})  # 1.5 hours
        self.assertEqual(results, [])
        old_head = {**bot, "commit_id": "0ld"}  # a bot review of an earlier head does not count
        self.assertEqual([r["reconcile"] for r in self.late_review(reviews=[old_head])[2]], ["external-review"])

    def test_conflicting_in_review_pr_goes_to_fix(self):
        t, results = self.recover(self.pr(conflict=True))
        self.assertEqual([r["reconcile"] for r in results], ["conflict"])
        self.assertEqual(logged(t, "label"), ['"factory:fix"'])
        self.assertEqual(t.get_stage(t.tickets[1]), "changes-requested")

    def test_landed_review_marker_replays_the_reported_outcome_of_the_definition(self):
        """The review is named after its node; a split approval replays the outcome its marker names."""
        review = {"agent": True, "focus": "review-high", "verdict": "approve", "outcome": "approve-risky",
                  "commit_id": "abc123def4567", "round": 1}
        t, results = self.recover(self.pr(ci="green", attrs=["risk:high"], pr_labels=["factory:review-high"],
                                          label_at={"factory:review-high": OLD}, reviews=[review]))
        self.assertEqual([(r["reconcile"], r["node"], r["outcome"]) for r in results],
                         [("reapply", "review-high", "approve-risky")])
        self.assertEqual(t.get_stage(t.tickets[1]), "approved")
        self.assertNotIn("factory:review-high", t.facts(1)["pr_labels"])
        self.assertIn('"factory:needs-human"', logged(t, "label"))
        self.assertNotIn("factory:review-low", logged(t, "label"))

    def test_conflict_uses_the_fix_stage_of_the_definition(self):
        import copy
        defn = copy.deepcopy(self.defn)
        for n in defn["workflow"]["nodes"].values():
            for oc in (n.get("outcomes") or {}).values():
                for fx in (oc or {}).get("effects") or []:
                    if fx.get("stage") == "changes-requested":
                        fx["stage"] = "repair"
        tr = MemorySystem({1: self.pr(conflict=True)})
        tracker = self.config["tracker"]
        reconcile.recover_definition(tr, defn, list(tracker["stages"]), tracker.get("stage_kinds", {}),
                                     self.config["limits"], now=NOW)
        self.assertEqual(tr.get_stage(tr.tickets[1]), "repair")

    def test_conflict_at_the_fix_round_cap_or_fresh_is_left_alone(self):
        self.assertEqual(self.recover(self.pr(conflict=True, fix_rounds=3))[1], [])
        self.assertEqual(self.recover(self.pr(conflict=True, since=FRESH))[1], [])

    def test_ready_ticket_without_a_dispatch_outcome_refired_once_then_blocked(self):
        """#1257: a never-blocked ready ticket whose dispatch run left no outcome (Air failed it) starts again."""
        ticket = {"stage": "ready", "since": OLD, "stage_at": OLD, "label_at": {"factory:ready": OLD}}
        t, results = self.recover(ticket)
        self.assertEqual([(r["node"], r["reconcile"]) for r in results], [("dispatch", "refire")])
        self.assertEqual(logged(t, "label"), ['"factory:ready"'])  # removed, then added again
        miss = logged(t, "comment")[0]
        self.assertIn("stage=ready head= re-fired factory:ready (", miss)
        ticket["log"] = [("comment", miss)]
        t, results = self.recover(ticket)
        self.assertEqual(results[0]["reconcile"], "blocked")
        self.assertEqual(t.get_stage(t.tickets[1]), "blocked")

    def test_fresh_or_blocked_ready_ticket_is_left_alone(self):
        fresh = {"stage": "ready", "since": FRESH, "stage_at": FRESH, "label_at": {"factory:ready": FRESH}}
        self.assertEqual(self.recover(fresh)[1], [])
        waiting = {"stage": "ready", "since": OLD, "stage_at": OLD, "label_at": {"factory:ready": OLD}, "blockers_open": 1}
        self.assertEqual(self.recover(waiting)[1], [])

    def test_review_label_without_review_refired_once_then_blocked(self):
        ticket = self.pr(ci="green", attrs=["risk:low"], pr_labels=["factory:review-low", "factory:ci-green"],
                         label_at={"factory:review-low": OLD, "factory:ci-green": OLD})
        t, results = self.recover(ticket)
        self.assertEqual([(r["node"], r["reconcile"]) for r in results], [("review-low", "refire")])
        self.assertEqual(logged(t, "label"), ['"factory:review-low"'])  # removed, then added again
        miss = logged(t, "comment")[0]
        self.assertIn("head=abc123def456 re-fired factory:review-low (", miss)
        ticket["log"] = [("comment", miss)]
        t, results = self.recover(ticket)
        self.assertEqual(results[0]["reconcile"], "blocked")
        self.assertIn('"factory:needs-human"', logged(t, "label"))
        self.assertEqual(t.get_stage(t.tickets[1]), "blocked")

    def test_fresh_review_label_is_left_alone_and_pending_ci_does_not_hold_a_review(self):
        labels = {"pr_labels": ["factory:review-low"], "attrs": ["risk:low"], "label_at": {"factory:review-low": OLD}}
        self.assertEqual(self.recover(self.pr(ci="green", **{**labels, "label_at": {"factory:review-low": FRESH}}))[1], [])
        self.assertEqual([r["reconcile"] for r in self.recover(self.pr(ci="pending", **labels))[1]], ["refire"])  # a review never waits for CI

    def test_review_label_whose_when_never_holds_is_stuck_once(self):
        ticket = self.pr(ci="green", attrs=[], pr_labels=["factory:review-low"],  # no risk label: `when` never holds
                         label_at={"factory:review-low": "2026-11-03T09:00:00Z"})  # 3 hours: past 2 timeouts
        t, results = self.recover(ticket)
        self.assertEqual([(r["node"], r["reconcile"]) for r in results], [("review-low", "stuck")])
        self.assertIn('"factory:needs-human"', logged(t, "label"))
        self.assertEqual(t.get_stage(t.tickets[1]), "blocked")
        note = logged(t, "comment")[0]
        self.assertIn("review-low when.label: want risk:low", note)
        ticket.update(log=[("comment", note)], stage="in-review", attrs=[])
        self.assertEqual(self.recover(ticket)[1], [])  # one notice per stage and head

    def test_stuck_label_in_a_second_ticket_repository_is_reported(self):
        tr = ticket_repositories_tracker({"o/b": [7]}, self.config["tracker"]["stages"])
        ticket = self.pr(ci="green", attrs=[], pr_labels=["factory:review-low"],  # no risk label: `when` never holds
                         label_at={"factory:review-low": "2026-11-03T09:00:00Z"})  # 3 hours: past 2 timeouts
        written = []
        tr.stale_facts = lambda n: ticket
        tr.apply = written.append
        tracker = self.config["tracker"]
        results = reconcile.recover_definition(tr, self.defn, list(tracker["stages"]), tracker.get("stage_kinds", {}),
                                               self.config["limits"], now=NOW)
        self.assertEqual([(r["ticket"], r["node"], r["reconcile"]) for r in results], [("o/b#7", "review-low", "stuck")])
        self.assertEqual({e["ticket"] for e in written}, {"o/b#7"})  # needs-human, the comment and `blocked`: all in o/b
        self.assertEqual([e["value"] for e in written if e["kind"] == "stage"], ["blocked"])

    def test_label_no_copy_hears_is_stuck_at_once(self):
        factory_repo, team_repo = "zaharchenko-evgeny/slop-factory", "zaharchenko-evgeny/slop-eval-factory"
        tracker = self.config["tracker"]
        cases = [
            ("stage trigger", {"stage": "triage", "attrs": []}, {"stage": "triage"}, True, False),
            ("issue label trigger", {"label": "factory:custom"}, {"stage": "ready", "attrs": ["factory:custom"]}, True, False),
            ("PR label trigger", {"label": "factory:custom"}, {"stage": "ready", "pr_labels": ["factory:custom"]}, True, False),
            ("missing label", {"label": "factory:custom"}, {"stage": "ready", "attrs": [], "pr_labels": []}, False, False),
            ("other stage", {"stage": "triage"}, {"stage": "ready", "attrs": []}, False, False),
            ("another copy hears same trigger", {"stage": "triage", "attrs": []}, {"stage": "triage"}, False, True),
        ]
        for name, trigger, facts, stuck, another_listener in cases:
            with self.subTest(name=name):
                defn = copy.deepcopy(self.defn)
                defn["factory"]["tracker"]["repositories"] = [factory_repo, team_repo]
                defn["factory"]["environments"]["intake"]["repositories"] = [{"repo": factory_repo}]
                defn["workflow"]["nodes"]["intake"]["triggers"] = [trigger]
                if another_listener:
                    defn["workflow"]["nodes"]["another-intake"] = {
                        "agent": "router", "environment": "team", "triggers": [trigger]}
                ticket = {**facts, "since": None}
                written = []

                class Tracker:
                    def sweep_tickets(self, stages, open_only=False):
                        return [f"{team_repo}#7"]

                    def stale_facts(self, _):
                        return ticket

                    def apply(self, effect):
                        written.append(effect)

                results = reconcile.recover_definition(Tracker(), defn, list(tracker["stages"]),
                                                       tracker.get("stage_kinds", {}), self.config["limits"], now=NOW)
                if stuck:
                    self.assertEqual([(r["reconcile"], r["node"]) for r in results], [("stuck", "intake")])
                    self.assertEqual([e["value"] for e in written if e["kind"] == "label"], ["factory:needs-human"])
                    self.assertEqual([e["value"] for e in written if e["kind"] == "stage"], ["blocked"])
                    note = next(e["value"] for e in written if e["kind"] == "comment")
                    self.assertIn("slop-eval-factory", note)
                    self.assertIn("Open the ticket in a repository where intake listens", note)
                else:
                    self.assertEqual(results, [])
                    self.assertEqual(written, [])

    def test_stuck_label_that_is_young_or_behind_a_blocker_is_left_alone(self):
        labels = {"ci": "green", "attrs": [], "pr_labels": ["factory:review-low"]}
        young = (NOW - timedelta(minutes=45)).isoformat()
        self.assertEqual(self.recover(self.pr(**labels, label_at={"factory:review-low": young}))[1], [])
        self.assertEqual(self.recover(self.pr(**labels, blockers_open=1, label_at={"factory:review-low": "2026-11-03T09:00:00Z"}))[1], [])

    def missed_twice(self):
        ticket = self.pr(ci="green", attrs=["risk:low"], pr_labels=["factory:review-low"],
                         label_at={"factory:review-low": OLD})
        ticket["log"] = [("comment", json.dumps(tagged(f"{reconcile.PREFIX} stage=in-review head=abc123def456 "
                                                "re-fired factory:review-low (no review-low outcome)")))]
        return ticket

    def run_many(self, count):
        tr = MemorySystem({i: self.missed_twice() for i in range(1, count + 1)})
        tracker = self.config["tracker"]
        results = reconcile.recover_definition(tr, self.defn, list(tracker["stages"]), tracker.get("stage_kinds", {}),
                                               self.config["limits"], now=NOW)
        return tr, results

    def test_three_second_misses_in_one_run_refire_instead_of_blocking(self):
        tr, results = self.run_many(3)
        self.assertEqual({(r["reconcile"], r.get("systemic")) for r in results}, {("refire", True)})
        self.assertEqual({tr.get_stage(tr.tickets[i]) for i in (1, 2, 3)}, {"in-review"})

    def test_a_miss_recorded_in_the_old_one_line_form_counts_like_the_new_form(self):
        old = self.missed_twice()
        old["log"] = [("comment", json.dumps(f"[factory] {reconcile.PREFIX} stage=in-review head=abc123def456 "
                                             "re-fired factory:review-low (no review-low outcome)"))]
        tr = MemorySystem({1: old})
        tracker = self.config["tracker"]
        results = reconcile.recover_definition(tr, self.defn, list(tracker["stages"]), tracker.get("stage_kinds", {}),
                                               self.config["limits"], now=NOW)
        self.assertEqual([r.get("reconcile") for r in results], ["blocked"], results)

    def test_one_second_miss_still_blocks(self):
        tr, results = self.run_many(1)
        self.assertEqual([r.get("reconcile") for r in results], ["blocked"], results)
        self.assertEqual(tr.get_stage(tr.tickets[1]), "blocked")

    def test_systemic_misses_open_one_doctor_ticket_and_a_clean_run_closes_it(self):
        calls, open_titles = [], []

        def gh(args, token=None):
            calls.append(args)
            found = [{"number": 9, "title": t} for t in open_titles]
            if args[:2] == ["issue", "list"]:
                return json.dumps(found)
            if args[0] == "api" and "--paginate" in args:
                return json.dumps([found])
            return "{}" if args[:2] == ["issue", "view"] else ""
        reconcile.report_systemic(self.config, [{"ticket": 1, "node": "review-low"}], run=gh)
        create = next(c for c in calls if c[:2] == ["issue", "create"])
        self.assertEqual(create[create.index("--title") + 1], "Doctor: runs-without-outcome fails")
        open_titles.append("Doctor: runs-without-outcome fails")
        calls.clear()
        reconcile.report_systemic(self.config, [{"ticket": 1, "node": "review-low"}], run=gh)
        self.assertFalse([c for c in calls if c[:2] in (["issue", "create"], ["issue", "comment"])])
        reconcile.report_systemic(self.config, [], run=gh)
        self.assertEqual([c[:3] for c in calls if c[:2] == ["issue", "close"]], [["issue", "close", "9"]])

    def test_a_failed_end_on_this_head_blocks_once_instead_of_refiring(self):
        ticket = self.pr(ci="green", attrs=["risk:low"], pr_labels=["factory:review-low"],
                         label_at={"factory:review-low": OLD})
        failure = ("[factory] end failed: node=review-low head=abc123def456 outcome=approve-low\n\n```\n"
                   "RuntimeError: HTTP 403: Resource not accessible by integration\n```\n\nThe outcome did not land; x")
        ticket["log"] = [("comment", json.dumps(failure))]
        t, results = self.recover(ticket)
        self.assertEqual([r["reconcile"] for r in results], ["end-failed"])
        self.assertEqual(t.get_stage(t.tickets[1]), "blocked")
        note = logged(t, "comment")[-1]
        self.assertIn("HTTP 403", note)
        self.assertNotIn('"factory:review-low"', logged(t, "label"))  # not re-fired
        ticket.update(stage="in-review", log=[("comment", json.dumps(failure)), ("comment", note)])
        self.assertEqual(self.recover(ticket)[1], [])  # one notice per stage and head

    def test_a_failed_end_on_an_older_head_does_not_stop_the_refire(self):
        ticket = self.pr(ci="green", attrs=["risk:low"], pr_labels=["factory:review-low"],
                         label_at={"factory:review-low": OLD})
        ticket["log"] = [("comment", json.dumps("[factory] end failed: node=review-low head=000000000000 outcome=x\n\nerr"))]
        self.assertEqual([r["reconcile"] for r in self.recover(ticket)[1]], ["refire"])

    def test_needs_human_ticket_is_left_alone(self):
        t, results = self.recover(self.pr(conflict=True, attrs=["factory:needs-human"]))
        self.assertEqual(results, [])
        self.assertEqual(logged(t, "label"), [])

    def test_bad_ticket_is_reported_once_and_does_not_stop_recovery(self):
        class BadTicketTracker(MemorySystem):
            def stale_facts(self, ticket):
                if ticket == 1:
                    raise ValueError("ticket names unknown repositories: fixture")
                return super().stale_facts(ticket)

        tracker = BadTicketTracker({1: {"stage": "ready"},
                                    2: {**self.pr(ci="green", attrs=["risk:low"],
                                                  pr_labels=["factory:review-low"],
                                                  label_at={"factory:review-low": OLD})}})
        config = self.config["tracker"]
        results = reconcile.recover_definition(tracker, self.defn, list(config["stages"]),
                                               config.get("stage_kinds", {}), self.config["limits"], now=NOW)
        self.assertEqual([(r["ticket"], r.get("error"), r.get("reconcile")) for r in results],
                         [(1, "ValueError: ticket names unknown repositories: fixture", None),
                          (2, None, "refire")])
        self.assertEqual(sum("ticket facts failed" in value for kind, value in tracker.tickets[1]["log"]
                             if kind == "comment"), 1)
        reconcile.recover_definition(tracker, self.defn, list(config["stages"]), config.get("stage_kinds", {}),
                                     self.config["limits"], now=NOW)
        self.assertEqual(sum("ticket facts failed" in value for kind, value in tracker.tickets[1]["log"]
                             if kind == "comment"), 1)

    def test_bad_ticket_dry_run_does_not_post_a_comment(self):
        class BadTicketTracker(MemorySystem):
            def stale_facts(self, ticket):
                raise ValueError("ticket names unknown repositories: fixture")

        tracker = BadTicketTracker({1: {"stage": "ready"}})
        config = self.config["tracker"]
        results = reconcile.recover_definition(tracker, self.defn, list(config["stages"]),
                                               config.get("stage_kinds", {}), self.config["limits"],
                                               now=NOW, dry_run=True)
        self.assertIn("error", results[0])
        self.assertEqual(tracker.tickets[1].get("log", []), [])


class ReconcileMainBlockersTest(unittest.TestCase):
    """`reconcile_main` hands `wake_approved` the definition's graph, whatever the engine node file says."""
    SLOP = Path(__file__).resolve().parent / "fixtures/slop"

    @classmethod
    def setUpClass(cls):
        cls.previous = cfg.team_root()
        cfg.set_team(cls.SLOP)

    @classmethod
    def tearDownClass(cls):
        cfg.set_team(cls.previous)

    def run_main(self, blockers, engine_blockers):
        import io
        import tempfile
        defn = copy.deepcopy(definition.load(self.SLOP))
        defn["workflow"]["blockers"] = blockers
        defn["workflow"]["nodes"]["merge"]["when"] = {"stage": "approved", "blockers": "closed"}
        ticket = {"stage": "approved", "blockers_open": 0, "pr": 5, "pr_state": "open",
                  "label_at": {"factory:merge": "2026-11-06T10:00:00Z"}}
        engine = {"blockers": engine_blockers, "nodes": {}}
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(definition, "load", return_value=defn), \
                mock.patch.object(nodes_mod, "load", return_value=engine), \
                mock.patch.object(MemorySystem, "blocked_by", lambda self, n: [{"number": 9, "state": "closed"}],
                                  create=True), \
                mock.patch.object(reconcile, "recover_definition", return_value=[]), \
                mock.patch.object(reconcile, "finish_lost", return_value=[]), \
                mock.patch("sys.stdout", io.StringIO()) as out:
            spec = Path(tmp) / "tickets.json"
            spec.write_text(json.dumps({"1": ticket}))
            self.assertEqual(reconcile.reconcile_main(["--dry-run", f"memory:{spec}"]), 0)
        return [(r["node"], r["label"]) for r in json.loads(out.getvalue())["reconcile"]
                if r.get("outcome") == "refire" and r.get("node") == "merge"]

    def test_the_blockers_mode_comes_from_the_definition_not_the_engine(self):
        for blockers, engine, want in (("merge", "start", [("merge", "factory:merge")]),
                                       ("start", "merge", [])):
            with self.subTest(definition=blockers, engine=engine):
                self.assertEqual(self.run_main(blockers, engine), want)


class ReconcileHeartbeatTest(unittest.TestCase):
    CONFIG = {"tracker": {"repository": "o/map", "map_label": "factory:map"},
              "targets": {"air": {"bot_login": "factory[bot]"}},
              "repositories": [{"name": "map", "url": "https://github.com/o/map", "role": "primary"}],
              "environments": {"write": {"repositories": [], "tokens": {"map": "MAP_TOKEN"}}}}

    def test_pass_creates_once_then_edits_and_dry_run_writes_nothing(self):
        class Gh:
            def __init__(self):
                self.comments, self.calls = [], []

            def __call__(self, args, **kw):
                self.calls.append((args, kw))
                if args[:2] == ["issue", "list"]:
                    return json.dumps([{"number": 404}])
                if args[0] == "api" and "repos/o/map/issues/404/comments" in " ".join(args):
                    if "-f" not in args:
                        return "\n".join(json.dumps(c) for c in self.comments)
                    body = next(a[5:] for a in args if a.startswith("body="))
                    self.comments.append({"id": 7, "body": body, "user": {"login": "factory[bot]"},
                                          "updated_at": "2026-11-03T12:00:00Z"})
                    return ""
                if args[0] == "api" and "issues/comments/7" in args[3]:
                    self.comments[0]["body"] = next(a[5:] for a in args if a.startswith("body="))
                    return ""
                return ""

        gh = Gh()
        env = {"FACTORY_ENV": "write", "MAP_TOKEN": "test-token"}
        now = NOW
        first, failed = heartbeat.reconcile_pass(self.CONFIG, False, None, run=gh, environ=env, now=now)
        second, _ = heartbeat.reconcile_pass(self.CONFIG, False, None, run=gh, environ=env, now=now + timedelta(minutes=1))
        self.assertFalse(failed)
        self.assertEqual([first[0]["action"], second[0]["action"]], ["create", "edit"])
        writes = [(args, kw) for args, kw in gh.calls if args[0] == "api" and ("-X" in args or "-f" in args)]
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(kw.get("token") == "test-token" for _, kw in writes))

        dry = Gh()
        result, _ = heartbeat.reconcile_pass(self.CONFIG, True, None, run=dry, environ=env, now=now)
        self.assertEqual(result[0]["action"], "create")
        self.assertFalse(any(args[0] == "api" and ("-X" in args or "-f" in args) for args, _ in dry.calls))

    def test_only_the_configured_identity_and_heartbeat_marker_match(self):
        body = heartbeat.comment_body("2026-11-03T12:00:00Z")
        cases = (("configured bot", {"login": "factory[bot]", "type": "Bot"}, body, True),
                 ("configured identity user type", {"login": "factory[bot]", "type": "User"}, body, True),
                 ("other bot", {"login": "other[bot]", "type": "Bot"}, body, False),
                 ("human", {"login": "person", "type": "User"}, body, False),
                 ("missing author", None, body, False),
                 ("marker missing", {"login": "factory[bot]", "type": "Bot"}, body.replace(heartbeat.MARKER, ""), False),
                 ("prefix missing", {"login": "factory[bot]", "type": "Bot"}, body.removeprefix("[factory] "), False),
                 ("body missing", {"login": "factory[bot]", "type": "Bot"}, "", False))
        for name, user, comment, want in cases:
            with self.subTest(name=name):
                got = heartbeat.heartbeat_comment([{"id": 1, "user": user, "body": comment}], "factory[bot]")
                self.assertEqual(bool(got), want)

    def run_main(self, beat):
        """reconcile_main on the Slop fixture with no tickets; `beat` patches the heartbeat pass. (exit code, output)"""
        import io
        before = cfg.team_root()
        cfg.set_team(Path(__file__).resolve().parent / "fixtures/slop")
        try:
            with mock.patch.object(gate, "tracker_of", return_value=MemorySystem({})), \
                    mock.patch("factory_plugins.github.heartbeat.reconcile_pass", **beat) as patched, \
                    mock.patch.object(reconcile, "ticket_passes", return_value=False), \
                    mock.patch("sys.stdout", io.StringIO()) as out, mock.patch("sys.stderr", io.StringIO()):
                code = reconcile.reconcile_main(["--dry-run"])
        finally:
            cfg.set_team(before)
        return code, json.loads(out.getvalue()), patched

    def test_each_pass_ends_with_the_heartbeat(self):
        """#1234: reconcile runs the heartbeat pass with its --dry-run and reports it."""
        code, out, beat = self.run_main({"return_value": ([{"action": "create"}], False)})
        self.assertEqual(code, 0, out)
        self.assertTrue(beat.call_args.args[1])
        self.assertEqual(out["reconcile_heartbeat"], [{"action": "create"}])

    def test_a_failed_heartbeat_fails_the_pass(self):
        code, out, _ = self.run_main({"side_effect": RuntimeError("no map")})
        self.assertEqual(code, 1)
        self.assertIn("no map", out["reconcile_heartbeat"][0]["error"])

    def test_the_heartbeat_is_skipped_for_a_tracker_that_is_not_github(self):
        from factory_plugins.github import ci_sync
        self.assertEqual(ci_sync.reconcile_heartbeat({"tracker": {"adapter": "youtrack"}}, False, None), {})
        self.assertFalse(heartbeat.applies({"tracker": {"adapter": "youtrack"}}))
        self.assertTrue(heartbeat.applies({"tracker": {"repository": "o/r"}}))
        self.assertTrue(heartbeat.applies({"tracker": {"adapter": "github-issues", "repository": "o/r"}}))


class NoDefinitionTest(unittest.TestCase):
    def test_a_team_without_a_definition_reports_instead_of_reconciling(self):
        import io
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(cfg, "team_root", return_value=Path(tmp)), \
                mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(reconcile.reconcile_main([]), 2)
        self.assertIn("factory.yaml and workflow.yaml", err.getvalue())


class ReconcileFailureTest(unittest.TestCase):
    """#845: a reconcile crash is a Doctor ticket, not a silent `reconciled` run."""
    def setUp(self):
        self.previous = cfg._team
        cfg.set_team(RecoverDefinitionTest.SLOP)
        self.real = reconcile.reconcile_main
        self.calls = []

    def tearDown(self):
        reconcile.reconcile_main = self.real
        cfg.set_team(self.previous)

    def gh(self, args, token=None):
        self.calls.append(args)
        found = [{"number": 7, "title": "Doctor: reconcile fails"}]
        if args[:2] == ["issue", "list"]:
            return json.dumps(found)
        if args[0] == "api" and "--paginate" in args:
            return json.dumps([found])
        return "{}" if args[:2] == ["issue", "view"] else ""

    def test_crash_opens_the_ticket_and_a_clean_run_closes_it(self):
        def boom(argv):
            raise RuntimeError("gh: HTTP 502")
        reconcile.reconcile_main = boom
        with self.assertRaises(RuntimeError):
            reconcile.main([], run=self.gh)
        comment = next(c for c in self.calls if c[:2] == ["issue", "comment"])
        self.assertIn("RuntimeError: gh: HTTP 502", comment[comment.index("--body") + 1])
        self.calls.clear()
        reconcile.reconcile_main = lambda argv: 0
        self.assertEqual(reconcile.main([], run=self.gh), 0)
        self.assertEqual([c[:3] for c in self.calls if c[:2] == ["issue", "close"]], [["issue", "close", "7"]])


class GitHubOrphanCommentTargetTest(unittest.TestCase):
    """An `orphan-pr` comment lands on the PR it names, not on the ticket's newest open PR."""

    def test_comment_goes_to_the_named_pr_for_each_open_pr(self):
        from factory_plugins.github.gate import GitHubGateTracker
        calls = []

        def gh(args, **kw):
            if args[:2] == ["issue", "comment"]:
                calls.append(args[2])
                return ""
            path = next(a for a in args if a.startswith("repos/"))
            if path.startswith("repos/o/r/pulls?"):
                return "\n".join(json.dumps({"number": k, "state": "open", "head": {"ref": "ez/3-a", "repo": {"full_name": "o/r"}}})
                                 for k in (11, 12))
            if path.startswith("repos/o/r/pulls/"):
                return json.dumps({"number": int(path.rsplit("/", 1)[1]), "state": "open", "mergeable": True})
            return ""

        tracker = GitHubGateTracker("o/r", {}, "bot", run=gh)
        for number in (11, 12):
            tracker.apply({"ticket": 3, "kind": "comment", "value": f"[factory] orphan {number}", "on": "pr", "pr": number})
        self.assertEqual(calls, ["11", "12"])


if __name__ == "__main__":
    unittest.main()


class DoctorTicketRepositoryTest(unittest.TestCase):
    """offline-eval-factory#90: a YouTrack team's GitHub Issues environment files its doctor tickets there, not in the
    code host of another organization its token cannot see."""
    DEFN = {"factory": {"tracker": {"adapter": "youtrack", "project": "LME"}, "environments": {
        "team": {"repositories": [{"repo": "me/team-factory", "write": True, "tracker": {"adapter": "github-issues"}},
                                  {"repo": "Org/app", "tracker": "none"}]},
        "app": {"repositories": [{"repo": "Org/app", "write": True, "tracker": {"scope": {"field": "S", "value": "A"}}}]}}}}
    SETTINGS = {"tracker": {"adapter": "youtrack"}, "code_host": {"repository": "Org/app"}}

    def test_the_environment_home_wins_only_for_a_non_github_tracker(self):
        import doctor
        self.assertEqual(doctor.ticket_repository(self.SETTINGS, "team", self.DEFN), "me/team-factory")
        self.assertEqual(doctor.ticket_repository(self.SETTINGS, "app", self.DEFN), "Org/app")
        self.assertEqual(doctor.ticket_repository(self.SETTINGS, "", self.DEFN), "Org/app")
        github = {"tracker": {"adapter": "github-issues", "repository": "me/engine"}}
        self.assertEqual(doctor.ticket_repository(github, "team", self.DEFN), "me/engine")


class StateRulesSkipGitHubStoreTest(unittest.TestCase):
    """offline-eval-factory#90: the YouTrack State rules pass skips a GitHub Issues store instead of failing on it."""

    def test_a_github_store_gives_no_results_and_no_failure(self):
        from unittest import mock
        import gate
        from factory_plugins.youtrack import state
        tracker = type("T", (), {"tickets": object()})()
        config = {"tracker": {"adapter": "youtrack", "state_rules": {"Obsolete": "stop"}, "stages": {"triage": {}}}}
        with mock.patch.object(gate, "tracker_of", return_value=tracker):
            self.assertEqual(state.reconcile_pass(config, True, None), {"state_rules": ([], False)})

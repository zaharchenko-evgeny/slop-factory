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
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
import project_mode  # noqa: E402
cfg.set_team(project_mode.team(Path(__file__).resolve().parent / "fixtures/team"))
import nodes as nodes_mod  # noqa: E402
import reconcile  # noqa: E402
import gate  # noqa: E402
from gate_port import MemorySystem  # noqa: E402

NODES, CONFIG = nodes_mod.load(), cfg.load()
STAGES, LIMITS = list(CONFIG["tracker"]["stages"]), CONFIG["limits"]
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
OLD, FRESH = "2026-11-03T11:00:00Z", "2026-11-03T11:50:00Z"


def run(ticket, nodes=NODES, **kw):
    tracker = MemorySystem({1: ticket})
    stage_kinds = kw.pop("stage_kinds", CONFIG["tracker"].get("stage_kinds", {}))
    results, failed = reconcile.reconcile(tracker, nodes, STAGES, LIMITS,
                                          stage_kinds=stage_kinds, now=NOW, **kw)
    return tracker, results, failed


def logged(tracker, kind):
    return [v for k, v in tracker.tickets[1].get("log", []) if k == kind]


class ReconcileTest(unittest.TestCase):
    def test_missing_review_focus_refired(self):
        t, results, _ = run({"stage": "in-review", "pr": 5, "ci": "green", "since": OLD, "focus_missing": ["behaviour"]})
        self.assertEqual(results[0]["node"], "review")
        self.assertIn('"factory:review-behaviour"', logged(t, "label"))

    def test_landed_review_marker_reapplies_outcome_without_refiring_reviewer(self):
        ticket = {"stage": "in-review", "pr": 5, "pr_state": "open", "ci": "green", "since": OLD,
                  "head": "abc", "pr_labels": ["factory:review-behaviour"],
                  "log": [["label", '"factory:review-behaviour"']],
                  "reviews": [{"agent": True, "focus": "behaviour", "verdict": "changes",
                               "commit_id": "abc", "round": 1}]}
        t, results, failed = run(ticket)
        self.assertFalse(failed)
        self.assertEqual((results[0]["reconcile"], results[0]["node"], results[0]["outcome"]),
                         ("reapply", "review", "changes"))
        self.assertNotIn("factory:review-behaviour", t.facts(1)["pr_labels"])

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

    def test_pending_ci_does_not_wake_a_ci_label(self):
        graph = {"nodes": {"merge": {"role": "script", "when": {"stage": "approved"},
                                     "triggers": ["pull_request.labeled factory:ci-green"]}}}
        self.assertEqual(list(reconcile.wakes(graph, {"stage": "approved", "pr": 5, "ci": "pending"})), [])
        self.assertEqual([w[1] for w in reconcile.wakes(graph, {"stage": "approved", "pr": 5, "ci": "green"})],
                         ["factory:ci-green"])

    def test_conflicting_unapproved_pr_goes_to_fix(self):
        t, results, _ = run({"stage": "in-review", "pr": 5, "conflict": True, "since": OLD,
                             "round_complete": False})
        self.assertEqual(results[0]["reconcile"], "conflict")
        self.assertEqual(t.tickets[1]["stage"], "changes-requested")
        self.assertIn('"factory:fix-cheap"', logged(t, "label"))

    def test_stale_fix_label_on_conflicting_pr_refired(self):
        t, results, _ = run({"stage": "changes-requested", "pr": 5, "conflict": True, "since": OLD,
                             "round_complete": False, "command": "factory:fix-cheap",
                             "command_at": FRESH, "commit_at": OLD, "attrs": ["factory:fix-cheap"]})
        self.assertEqual((results[0]["node"], results[0]["reconcile"]), ("fix", "refire"))
        self.assertIn('"factory:fix-cheap"', logged(t, "label"))

    def test_conflicting_pr_without_command_does_not_fail(self):
        t, results, failed = run({"stage": "changes-requested", "pr": 5, "conflict": True,
                                  "since": OLD, "command": None})
        self.assertFalse(failed)
        self.assertEqual(results, [])
        self.assertEqual(t.tickets[1]["stage"], "changes-requested")

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

    def test_waiting_stage_reminded_not_refired(self):
        ticket = {"stage": "blocked", "since": "2026-11-02T11:00:00Z", "stage_at": "2026-11-02T11:00:00Z",
                  "assignees": ["alice"], "command": "factory:implement-mid"}
        t, results, _ = run(ticket)
        self.assertEqual([r["reconcile"] for r in results], ["remind"])
        self.assertEqual(logged(t, "comment")[0],
                         '"[factory] reconcile: stage=blocked entry=2026-11-02T11:00:00Z waiting-reminder: @alice, this ticket is still waiting for you."')
        _, results, _ = run({**ticket, "log": [["comment", logged(t, "comment")[0]]]})
        self.assertEqual(results, [])
        _, results, _ = run({**ticket, "stage": "needs-answers", "assignees": []})
        self.assertEqual(results, [])

    def test_waiting_stage_reminds_assignee_marked_needs_human(self):
        ticket = {"stage": "blocked", "since": "2026-11-02T11:00:00Z",
                  "stage_at": "2026-11-02T11:00:00Z", "assignees": ["alice"],
                  "attrs": ["factory:needs-human"]}
        t, results, _ = run(ticket)
        self.assertEqual([r["reconcile"] for r in results], ["remind"])
        self.assertIn("@alice, this ticket is still waiting for you.", logged(t, "comment")[0])

    def test_custom_stage_kind_from_config(self):
        kinds = {**CONFIG["tracker"]["stage_kinds"], "investigating": "active"}
        nodes = copy.deepcopy(NODES)
        del nodes["nodes"]["investigate"]  # the custom node alone owns this stage
        nodes["nodes"]["custom"] = {"role": "script", "triggers": ["issues.labeled factory:custom"],
                                    "when": {"stage": "investigating"}, "outcomes": {"x": {}}, "timeout_minutes": 5}
        t, results, _ = run({"stage": "investigating", "since": OLD}, nodes, stage_kinds=kinds)
        self.assertEqual(results[0]["node"], "custom")
        self.assertIn('"factory:custom"', logged(t, "label"))

    def test_lists_only_open_tickets_in_non_final_stages(self):
        asked = []

        class Counting(MemorySystem):
            def sweep_tickets(self, stages, open_only=False):
                asked.append((sorted(stages), open_only))
                return super().sweep_tickets(stages, open_only)

            def stale_facts(self, ticket):
                read.append(ticket)
                return super().stale_facts(ticket)

        read = []
        tracker = Counting({1: {"stage": "in-dev", "since": OLD, "command": "factory:implement-mid"},
                            2: {"stage": "done", "since": OLD},
                            3: {"stage": "blocked", "since": OLD, "issue_state": "closed"}})
        reconcile.reconcile(tracker, NODES, STAGES, LIMITS, stage_kinds=CONFIG["tracker"]["stage_kinds"], now=NOW)
        finals = {s for s, k in CONFIG["tracker"]["stage_kinds"].items() if k == "final"}
        self.assertEqual(asked[0], (sorted(s for s in STAGES if s not in finals), True))
        self.assertEqual(read, [1])  # no facts reads for a done or closed ticket

    def test_closed_approved_ticket_with_merged_pr_is_finished(self):
        t, results, failed = run({"stage": "approved", "pr": 5, "pr_state": "merged", "issue_state": "closed",
                                  "since": OLD})
        self.assertFalse(failed)
        self.assertEqual((results[0]["ticket"], results[0]["reconcile"]), (1, "finish"))
        self.assertEqual(t.tickets[1]["stage"], "done")

    def test_closed_done_ticket_is_left_alone(self):
        t, results, _ = run({"stage": "done", "pr": 5, "pr_state": "merged", "issue_state": "closed", "since": OLD})
        self.assertEqual((results, t.tickets[1]["stage"]), ([], "done"))

    def test_closed_approved_ticket_with_open_pr_is_left_alone(self):
        t, results, _ = run({"stage": "approved", "pr": 5, "pr_state": "open", "issue_state": "closed", "since": OLD})
        self.assertEqual((results, t.tickets[1]["stage"]), ([], "approved"))

    def test_open_main_red_with_green_main_reports_stale_red(self):
        tracker = MemorySystem({1: {"stage": "ready", "since": FRESH, "title": "Main is red: agnix"},
                                 2: {"stage": "ready", "since": FRESH, "title": "Other ticket"}})
        tracker.green_main = True
        results, _ = reconcile.reconcile(tracker, NODES, STAGES, LIMITS, stage_kinds=CONFIG["tracker"]["stage_kinds"], now=NOW)
        self.assertEqual([(r["ticket"], r["reconcile"]) for r in results], [(1, "stale-red")])
        tracker.green_main = False
        results, _ = reconcile.reconcile(tracker, NODES, STAGES, LIMITS, stage_kinds=CONFIG["tracker"]["stage_kinds"], now=NOW)
        self.assertEqual(results, [])

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

    def test_each_scheduled_pass_runs_the_ci_label_sync(self):
        import contextlib
        import io
        import json
        from unittest.mock import patch
        out, synced = io.StringIO(), {"repo": "o/app", "pr": 1, "ci": "green"}
        with patch("factory_plugins.github.gate.GitHubGateTracker.from_config", return_value=MemorySystem({})), \
                patch("factory_plugins.github.ci_sync.run_ci_sync", return_value=([synced], False)) as sync, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reconcile.main(["--dry-run"]), 0)
        self.assertTrue(sync.call_args.args[0])  # the pass's --dry-run
        self.assertEqual(json.loads(out.getvalue())["ci_sync"], [synced])



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
    from test_gate import TicketReposGh
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
                config = {**CONFIG, "tracker": {**CONFIG["tracker"], **tracker}, "repositories": [
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

    def test_review_label_without_green_ci_or_fresh_is_left_alone(self):
        labels = {"pr_labels": ["factory:review-low"], "attrs": ["risk:low"], "label_at": {"factory:review-low": OLD}}
        self.assertEqual(self.recover(self.pr(ci="pending", **labels))[1], [])
        self.assertEqual(self.recover(self.pr(ci="green", **{**labels, "label_at": {"factory:review-low": FRESH}}))[1], [])

    def test_review_label_whose_when_never_holds_is_stuck_once(self):
        ticket = self.pr(ci="none", attrs=["risk:low"], pr_labels=["factory:review-low"],
                         label_at={"factory:review-low": "2026-11-03T09:00:00Z"})  # 3 hours: past 2 timeouts
        t, results = self.recover(ticket)
        self.assertEqual([(r["node"], r["reconcile"]) for r in results], [("review-low", "stuck")])
        self.assertIn('"factory:needs-human"', logged(t, "label"))
        self.assertEqual(t.get_stage(t.tickets[1]), "blocked")
        note = logged(t, "comment")[0]
        self.assertIn("review-low when.ci: want green, have none", note)
        ticket.update(log=[("comment", note)], stage="in-review", attrs=["risk:low"])
        self.assertEqual(self.recover(ticket)[1], [])  # one notice per stage and head

    def test_stuck_label_in_a_second_ticket_repository_is_reported(self):
        tr = ticket_repositories_tracker({"o/b": [7]}, self.config["tracker"]["stages"])
        ticket = self.pr(ci="none", attrs=["risk:low"], pr_labels=["factory:review-low"],
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

    def test_stuck_label_that_is_young_or_behind_a_blocker_is_left_alone(self):
        labels = {"ci": "none", "attrs": ["risk:low"], "pr_labels": ["factory:review-low"]}
        young = (NOW - timedelta(minutes=45)).isoformat()
        self.assertEqual(self.recover(self.pr(**labels, label_at={"factory:review-low": young}))[1], [])
        self.assertEqual(self.recover(self.pr(**labels, blockers_open=1, label_at={"factory:review-low": "2026-11-03T09:00:00Z"}))[1], [])

    def missed_twice(self):
        ticket = self.pr(ci="green", attrs=["risk:low"], pr_labels=["factory:review-low"],
                         label_at={"factory:review-low": OLD})
        ticket["log"] = [("comment", json.dumps(f"{reconcile.PREFIX} stage=in-review head=abc123def456 "
                                                "re-fired factory:review-low (no review-low outcome)"))]
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

    def test_one_second_miss_still_blocks(self):
        tr, results = self.run_many(1)
        self.assertEqual([r.get("reconcile") for r in results], ["blocked"], results)
        self.assertEqual(tr.get_stage(tr.tickets[1]), "blocked")

    def test_systemic_misses_open_one_doctor_ticket_and_a_clean_run_closes_it(self):
        calls, open_titles = [], []

        def gh(args, token=None):
            calls.append(args)
            return json.dumps([{"number": 9, "title": t} for t in open_titles]) if args[:2] == ["issue", "list"] else ""
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
        return json.dumps([{"number": 7, "title": "Doctor: reconcile fails"}]) if args[:2] == ["issue", "list"] else ""

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


if __name__ == "__main__":
    unittest.main()

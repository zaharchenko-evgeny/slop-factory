"""`ui_engine_eligibility_readonly`: trigger evidence by `local.poll`, guards by `gate.matches` and `gate.holds`,
and no write or execution path."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import snapshot_support as s  # noqa: E402
import gate  # noqa: E402
from factory_plugins.local import surface as local  # noqa: E402
from ui import eligibility, snapshot  # noqa: E402


def node_of(g, name):
    return next(n for n in g["nodes"] if n["id"] == name)


class Eligibility(unittest.TestCase):
    def snap(self, tickets, items=(), **kw):
        tracker = s.FakeTracker({t: {"facts": f, "events": []} for t, f in tickets.items()}, labeled_items=items, **kw)
        with s.team("slop") as (config, nodes):
            snap = snapshot.build(config, nodes, [s.source(tracker)], s.NOW)
        return snap, tracker

    def node(self, snap, ticket, name):
        card = next(t for t in snap["tickets"] if t["id"] == str(ticket))
        return next(n for n in card["nodes"] if n["node"] == name)

    def test_trigger_evidence_and_guards_decide_eligibility(self):
        ready = s.facts(stage="ready", blockers_open=0)
        snap, _ = self.snap({5: ready, 6: s.facts(stage="ready", blockers_open=2), 7: ready},
                            [s.item(5, "factory:ready"), s.item(6, "factory:ready")])
        yes, blocked, nolabel = (self.node(snap, n, "dispatch") for n in (5, 6, 7))
        self.assertEqual((yes["eligible"], yes["trigger"]["status"], yes["guards"]["status"]), ("yes", "match", "pass"))
        self.assertEqual((blocked["eligible"], blocked["trigger"]["status"], blocked["guards"]["status"]), ("no", "match", "fail"))
        self.assertIn("blockers", blocked["guards"]["reason"])  # the non-match reason is shown
        self.assertEqual((nolabel["eligible"], nolabel["trigger"]["status"]), ("no", "no-match"))
        self.assertIn("no issues.labeled event", nolabel["trigger"]["triggers"][0]["reason"])

    def test_missing_evidence_is_unknown_not_a_guess(self):
        snap, _ = self.snap({1: s.facts(stage="ready"), 3: s.facts(stage="blocked"),
                             2: s.facts(stage="in-review", ci="green", attrs=["risk:low"])})
        for ticket, name, why in ((1, "human", "manual trigger"), (1, "reconcile", "schedule trigger"),
                                  (3, "ticket-reply", "event-only trigger")):
            with self.subTest(why):
                got = self.node(snap, ticket, name)
                self.assertEqual((got["eligible"], got["trigger"]["status"]), ("unknown", "unknown"))
        review = self.node(snap, 2, "review-low")  # a polled trigger without an event is a no-match, not a guess
        self.assertEqual((review["trigger"]["status"], review["guards"]["status"]), ("no-match", "pass"))

    def test_a_guard_key_that_needs_event_evidence_is_unknown(self):
        node = {"id": "n", "when": {"stage": "ready", "actor": "human", "focus": "x", "due": True}}
        got = eligibility.guards(node, s.facts(stage="ready"))
        self.assertEqual({k["key"]: k["status"] for k in got["keys"]}, {"stage": "pass", "actor": "unknown", "focus": "unknown", "due": "unknown"})
        self.assertEqual(got["status"], "unknown")
        self.assertEqual(eligibility.guards(node, s.facts(stage="triage"))["status"], "fail")  # a known failure wins
        partial = s.facts()
        del partial["blockers_open"]
        self.assertEqual(eligibility.guards({"id": "n", "when": {"blockers": "closed"}}, partial)["status"], "unknown")
        self.assertEqual(eligibility.guards({"id": "n", "when": {"stage": "ready"}}, s.facts(), {"id": "e", "actor": "human"})["status"], "pass")

    def test_poll_failure_makes_polled_triggers_unknown(self):
        snap, _ = self.snap({5: s.facts(stage="ready")}, [s.item(5, "factory:ready")], poll_fails=True)
        start = self.node(snap, 5, "dispatch")
        self.assertEqual((start["trigger"]["status"], start["eligible"]), ("unknown", "unknown"))
        self.assertIn("poll failed", start["trigger"]["triggers"][0]["reason"])

    def test_execution_is_unknown_and_never_running(self):
        snap, _ = self.snap({5: s.facts(stage="ready")}, [s.item(5, "factory:ready")])
        for card in snap["tickets"]:
            self.assertEqual(card["execution"]["status"], "unknown")
            for n in card["nodes"]:
                self.assertEqual(n["execution"]["status"], "unknown")
        self.assertNotIn("running", repr(snap["tickets"]).lower().replace("not running", "").replace("does not mean running", ""))
        with s.team("slop") as (config, nodes):
            tracker = s.FakeTracker({5: {"facts": s.facts(), "events": []}}, labeled_items=[s.item(5, "factory:ready")])
            src = s.source(tracker)
            seen = snapshot.build(config, nodes, [src], s.NOW, execution={(src.identity, 5, "dispatch"): "running"})
        self.assertEqual(next(n for n in seen["tickets"][0]["nodes"] if n["node"] == "dispatch")["execution"]["status"], "running")  # only on evidence

    def test_outcome_guards_use_gate_guard_holds(self):
        with s.team("slop") as (config, nodes):
            from ui import graph
            g = graph.build(nodes, config)
            merge = node_of(g, "merge")
            got = {o["name"]: o for o in eligibility.outcomes(merge, s.facts(stage="approved"))}
            self.assertEqual(set(got), {"merged", "conflict", "updated-low", "updated-high", "ci-red", "waiting"})
            self.assertEqual(eligibility.outcomes(merge, s.facts(stage="triage"))[0]["status"], "blocked")


class ReadOnly(unittest.TestCase):
    """A refresh reaches the tracker only through reads, and never an execution path."""

    def test_snapshot_never_writes_or_executes(self):
        tracker = s.FakeTracker({5: {"facts": s.facts(), "events": []}}, labeled_items=[s.item(5, "factory:ready")])
        boom = lambda name: mock.Mock(side_effect=AssertionError(f"{name} was called"))  # noqa: E731
        with s.team("slop") as (config, nodes), \
                mock.patch.object(gate, "sweep", boom("gate.sweep")), mock.patch.object(gate, "check", boom("gate.check")), \
                mock.patch.object(local, "run_event", boom("local.run_event")), mock.patch.object(local, "main", boom("local.main")), \
                mock.patch.object(local, "poll", wraps=local.poll) as poll, \
                mock.patch.object(gate, "matches", wraps=gate.matches) as matches, \
                mock.patch.object(gate, "holds", wraps=gate.holds) as holds, \
                mock.patch("subprocess.run", boom("subprocess")):
            snapshot.build(config, nodes, [s.source(tracker)], s.NOW)
        poll.assert_called_once()  # trigger evidence is local.poll
        self.assertTrue(matches.called and holds.called)  # guards are gate.matches and gate.holds
        self.assertTrue(set(tracker.calls) <= {"sweep_tickets", "facts", "read", "find:labeled", "find:prs", "find:closed"}, tracker.calls)

    def test_write_methods_of_the_fake_would_fail_the_test(self):
        with self.assertRaises(AssertionError):
            s.FakeTracker().apply({})

    def test_module_does_not_reference_execution_paths(self):
        root = Path(snapshot.__file__).parent
        text = "\n".join(p.read_text() for p in root.glob("*.py"))
        for call in ("gate.sweep(", "gate.check(", "run_event(", "gate.apply(", ".apply(", "begin("):
            self.assertNotIn(call, text, call)


if __name__ == "__main__":
    unittest.main()

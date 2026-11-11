"""The contract every tracker or vcs system keeps (ADR-0026, factory/ports/system.py), as a test mixin.

A subclass of `SystemContract` and `unittest.TestCase` gives `make()`: a fresh system holding ticket 7 at stage
`in-review` with the labels `risk:low` and `factory:fix-mid`, and one open pull request (5) in repository `primary`
at head `HEAD` that carries `factory:fix-mid` too. The suite then checks:
- records: what the gate writes through `system.apply` (its one write path) reads back through `system.read`;
- idempotency: for every `(kind, target)` the system declares in `effects`, a first apply of its case writes and a
  second returns False (a retry converges). `CASES` holds one case per pair; a system adds `SETUP` for the state a
  case needs, `NO_WRITE` for a pair that never writes, and `EXEMPT` (with the reason) for one that cannot keep it;
- the guarantees of a state store (ADR-0028): compare-and-set on the stage, an `actor` on every ticket event, the
  author flags on every comment, `find` for `stage`, `children` and `blocking`, and `event(payload)` with the canonical
  trigger names. The lease is not here: its effect adds its test (#1271).
A system that does not keep a guarantee yet lists the test in `XFAIL` with the follow-up issue; the test then must
fail, so the list shrinks when the system is fixed.
"""
import effects
from ports.system import Query, Subject


def eff(kind, value=None, **kw):
    return {"ticket": 7, "kind": kind, "value": value, **kw}


# (kind, target) -> (effects applied first, the effect applied twice)
CASES = {
    ("stage", "ticket"): ([], eff("stage", "approved")),
    ("seen", "ticket"): ([], eff("seen", "event-1")),
    ("close", "ticket"): ([], eff("close")),
    ("join", "ticket"): ([], eff("join", "all-approve", tier="mid")),
    ("label", "ticket"): ([], eff("label", "x")),
    ("unlabel", "ticket"): ([], eff("unlabel", "risk:low")),
    ("label", "pr"): ([], eff("label", "factory:needs-human", on="pr")),
    ("unlabel", "pr"): ([], eff("unlabel", "factory:fix-*", on="pr")),
    ("set-field", "ticket"): ([], eff("set-field", {"risk": "high"})),
    ("field", "ticket"): ([], eff("field", {"name": "Effort", "value": "S"})),
    ("assign", "ticket"): ([], eff("assign", "alice")),
    ("unassign", "ticket"): ([eff("assign", "alice")], eff("unassign")),
    ("assign", "pr"): ([], eff("assign", "alice", on="pr")),
    ("unassign", "pr"): ([eff("assign", "alice", on="pr")], eff("unassign", on="pr")),
    ("comment", "ticket"): ([], eff("comment", "hi", run_id="r")),
    ("comment", "pr"): ([], eff("comment", "hi", on="pr", run_id="r")),
    ("pr-notes", "pr"): ([], eff("pr-notes", "the notes")),
    ("pr-notes-sync", "pr"): ([], eff("pr-notes-sync")),
    ("react", "ticket"): ([], eff("react", "+1", comment=1)),
    ("react", "pr"): ([], eff("react", "+1", comment=1, on="pr")),
    ("review", "pr"): ([], eff("review", "approve-at-head", focus="full", summary="ok", run_id="r")),
    ("request-reviewers", "pr"): ([], eff("request-reviewers", None)),
    ("ready-for-review", "pr"): ([], eff("ready-for-review")),
    ("create-issues", "ticket"): ([], eff("create-issues", [{"title": "A", "body": "scope A"}])),
    ("park-behind-main-red", "ticket"): ([], eff("park-behind-main-red", "quality-gates")),
    ("merge", "pr"): ([], eff("merge", "match-head", head="h1")),
    ("update-branch", "pr"): ([], eff("update-branch", None, head="h1")),
    (effects.ANY, "ticket"): ([], eff("any-other-effect", "v")),
}


class SystemContract:
    HEAD = "h1"
    SETUP = {}  # (kind, target) -> callable(system): the state its case needs beyond `make()`
    NO_WRITE = {}  # (kind, target) -> why its first apply writes nothing either
    EXEMPT = {}  # (kind, target) -> why a second apply may still write (a gap to close)
    XFAIL = {}  # test name -> the follow-up issue ("#N: why") of a store guarantee this system does not keep yet

    def run(self, result=None):
        if self._testMethodName in self.XFAIL:  # unittest reports it as an expected failure
            self.__unittest_expecting_failure__ = True
        return super().run(result)

    def make(self):
        raise NotImplementedError

    # --- records written by the gate's write path read back
    def test_a_review_posted_at_head_reads_back_pinned_to_it(self):
        s = self.make()
        self.assertTrue(s.apply(eff("review", "approve-at-head", focus="full", summary="ok", run_id="r")))
        pr = s.read(Subject("pr", 7, "primary"))
        self.assertEqual(pr.head, self.HEAD)
        mine = [r for r in pr.reviews if r.get("agent") and r.get("focus") == "full"]
        self.assertEqual([(r["commit_id"], r["verdict"]) for r in mine], [(self.HEAD, "approve")])

    def test_a_label_added_shows_in_attrs(self):
        s = self.make()
        self.assertTrue(s.apply(eff("label", "x")))
        self.assertIn("x", s.read(Subject("ticket", 7)).attrs)

    def test_a_label_removed_leaves_attrs(self):
        s = self.make()
        self.assertTrue(s.apply(eff("unlabel", "risk:low")))
        self.assertNotIn("risk:low", s.read(Subject("ticket", 7)).attrs)

    def test_a_pr_label_added_shows_on_the_pr(self):
        s = self.make()
        self.assertTrue(s.apply(eff("label", "factory:needs-human", on="pr")))
        self.assertIn("factory:needs-human", s.read(Subject("pr", 7, "primary")).labels)

    def test_a_comment_reads_back_on_the_ticket(self):
        s = self.make()
        self.assertTrue(s.apply(eff("comment", "hello there", run_id="r")))
        self.assertTrue(any("hello there" in str(c.get("body")) for c in s.read(Subject("ticket", 7)).comments))

    def test_a_scoped_read_is_a_read(self):
        """A scope the system does not split by yet (one top-level tracker) reads the same record."""
        s = self.make()
        self.assertEqual(s.read(Subject("ticket", 7, scope={"label": "env:a"})).attrs, s.read(Subject("ticket", 7)).attrs)

    # --- every declared effect converges
    def test_every_declared_effect_is_idempotent(self):
        declared = sorted(effects.registry(self.make()))
        self.assertTrue(declared, "the system declares no effects")
        for key in declared:
            with self.subTest(effect=key):
                if key in self.EXEMPT:
                    continue
                self.assertIn(key, CASES, f"no idempotency case for {key}")
                before, e = CASES[key]
                s = self.make()
                if key in self.SETUP:
                    self.SETUP[key](s)
                for b in before:
                    s.apply(dict(b))
                first = s.apply(dict(e))
                self.assertEqual(bool(first), key not in self.NO_WRITE, f"first apply of {key}")
                self.assertFalse(s.apply(dict(e)), f"second apply of {key} wrote again")

    def test_exemptions_name_declared_effects(self):
        declared = set(effects.registry(self.make()))
        self.assertLessEqual(set(self.EXEMPT) | set(self.NO_WRITE) | set(self.SETUP), declared)

    # --- the guarantees of a state store (ADR-0028)
    def test_a_stage_effect_that_expects_another_stage_writes_nothing(self):
        s = self.make()
        self.assertFalse(s.apply(eff("stage", "approved", expect="in-dev")))
        self.assertEqual(s.find(Query("stage", values=("in-review",))), [7])

    def test_a_stage_effect_that_expects_the_current_stage_moves_it(self):
        s = self.make()
        self.assertTrue(s.apply(eff("stage", "approved", expect="in-review")))
        self.assertEqual(s.find(Query("stage", values=("approved",))), [7])
        self.assertEqual(s.find(Query("stage", values=("in-review",))), [])

    def test_a_ticket_event_names_its_actor(self):
        s = self.make()
        self.assertTrue(s.apply(eff("label", "x")))
        self.assertTrue(s.apply(eff("unlabel", "x")))
        events = s.read(Subject("ticket", 7)).events
        self.assertEqual([e["kind"] for e in events if e.get("value") == "x"], ["labeled", "unlabeled"])
        self.assertTrue(events)
        for e in events:
            self.assertTrue(e.get("actor"), f"event without an actor: {e}")

    def test_a_comment_carries_the_author_flags(self):
        s = self.make()
        self.assertTrue(s.apply(eff("comment", "hello there", run_id="r")))
        mine = [c for c in s.read(Subject("ticket", 7)).comments if "hello there" in str(c.get("body"))]
        self.assertEqual(len(mine), 1)
        for c in s.read(Subject("ticket", 7)).comments:
            for flag in ("author_is_bot", "author_is_factory"):
                self.assertIsInstance(c.get(flag), bool, f"comment without {flag}: {c}")
        self.assertEqual((mine[0]["author_is_bot"], mine[0]["author_is_factory"]), (True, True))

    def test_find_stage_lists_the_tickets_in_a_stage(self):
        s = self.make()
        for values, want in ((("in-review",), [7]), (("in-review", "done"), [7]), (("done",), []), ((), [])):
            with self.subTest(values=values):
                self.assertEqual(s.find(Query("stage", values=values)), want)

    def test_find_stage_open_only_leaves_out_a_closed_ticket(self):
        s = self.make()
        self.assertTrue(s.apply(eff("close")))
        self.assertEqual(s.find(Query("stage", values=("in-review",), open_only=True)), [])

    def relate(self, s):
        """Give the system `{"children": (parent, [its sub-tickets]), "blocking": (blocker, [the tickets it blocks]),
        "other": (ticket, [its sub-tickets])}`: one ticket with sub-tickets and one it blocks, and an unrelated ticket
        (with sub-tickets of its own where the system can hold them), so `find` has to filter."""
        raise NotImplementedError

    @staticmethod
    def numbers(found):
        return sorted((x["number"] if isinstance(x, dict) else x) for x in found)

    def test_find_children_lists_the_sub_tickets(self):
        s = self.make()
        rel = self.relate(s)
        (parent, kids), (other, other_kids) = rel["children"], rel["other"]
        self.assertTrue(kids and set(kids).isdisjoint(other_kids))
        self.assertEqual(self.numbers(s.find(Query("children", of=parent))), sorted(kids))
        self.assertEqual(self.numbers(s.find(Query("children", of=other))), sorted(other_kids))
        self.assertEqual(self.numbers(s.find(Query("children", of=kids[0]))), [])

    def test_find_blocking_lists_the_tickets_a_ticket_blocks(self):
        s = self.make()
        blocker, blocked = self.relate(s)["blocking"]
        self.assertTrue(blocked)
        self.assertEqual(self.numbers(s.find(Query("blocking", of=blocker))), sorted(blocked))
        self.assertEqual(self.numbers(s.find(Query("blocking", of=blocked[0]))), [])

    def test_an_unknown_query_is_refused(self):
        with self.assertRaises(ValueError):
            self.make().find(Query("no-such-query"))

    def test_event_keeps_the_canonical_trigger_name(self):
        s = self.make()
        for name in ("issues.labeled", "issues.unlabeled", "issues.closed", "issue_comment.created",
                     "pull_request.synchronize", "pull_request_review.submitted"):
            with self.subTest(name=name):
                e = s.event({"ticket": 7, "event_name": name})
                self.assertEqual((e.ticket, e.kind), (7, name))

    def test_event_without_a_ticket_is_none(self):
        self.assertIsNone(self.make().event({"event_name": "issues.labeled"}))

    def test_expected_failures_name_a_test_and_its_follow_up_issue(self):
        for name, why in self.XFAIL.items():
            with self.subTest(test=name):
                self.assertTrue(callable(getattr(self, name, None)), f"{name} is no test of the contract")
                self.assertRegex(why, r"^#\d+: ")

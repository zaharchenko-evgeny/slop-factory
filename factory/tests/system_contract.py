"""The contract every tracker or vcs system keeps (ADR-0026, factory/ports/system.py), as a test mixin.

A subclass of `SystemContract` and `unittest.TestCase` gives `make()`: a fresh system holding ticket 7 at stage
`in-review` with the labels `risk:low` and `factory:fix-mid`, and one open pull request (5) in repository `primary`
at head `HEAD` that carries `factory:fix-mid` too. The suite then checks:
- records: what the gate writes through `system.apply` (its one write path) reads back through `system.read`;
- idempotency: for every `(kind, target)` the system declares in `effects`, a first apply of its case writes and a
  second returns False (a retry converges). `CASES` holds one case per pair; a system adds `SETUP` for the state a
  case needs, `NO_WRITE` for a pair that never writes, and `EXEMPT` (with the reason) for one that cannot keep it.
"""
import effects
from ports.system import Subject


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

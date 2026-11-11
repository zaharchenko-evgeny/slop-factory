#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The `gh` calls of each gate step over the stubbed GitHub (#1067). BASELINE holds the counts measured before
the facts moved to `derive` and the effects to the registry; a step may need fewer calls, never more.
Run: uv run factory/tests/test_gh_calls.py (FACTORY_GH_CALLS=print prints the counts)."""
import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_gate_github as G  # noqa: E402
from test_gate_github import BOT, C, M, REPO, STAGES, FakeGh, GitHubGateTracker, TwoRepositoryGh, eff, review  # noqa: E402
import gate  # noqa: E402
import nodes  # noqa: E402

# step -> gh calls that reached the runner (after CountedRun's memo), measured at 5b8d9a3 + #1065 + #1066;
# #1207: CI also reads the head's commit statuses, one more call per facts read; #1261: the join approval adds ready-for-review and the project-review label (sweep in-review 48 -> 51)
BASELINE = {
    "facts in-review": 15,
    "facts approved (behind)": 14,
    "facts needs-answers": 13,
    "facts split": 14,
    "facts two repositories": 22,
    "check fix": 13,
    "apply stage": 2,
    "apply stage done": 3,
    "apply label pr": 3,
    "apply unlabel pr": 4,
    "apply label issue": 2,
    "apply set-field": 2,
    "apply assign pr": 3,
    "apply comment": 2,
    "apply comment pr": 4,
    "apply review": 17,
    "apply join": 18,
    "apply merge": 9,
    "apply merge two repositories": 17,
    "outcome reviewer approve": 28,
    "outcome fix done": 21,
    "sweep in-review": 51,
    "sweep approved dry": 17,
}


def counted(gh, **kw):
    """(tracker, calls): the tracker over `gh`, and the list of the calls that reach it."""
    calls = []
    return GitHubGateTracker(REPO, STAGES, BOT, fix_cap=3, run=lambda args, **k: calls.append(args) or gh(args, **k),
                             **kw), calls


def two_repos(gh):
    return counted(gh, repositories={"primary": REPO, "other": TwoRepositoryGh.OTHER})


def in_review():
    return FakeGh(reviews=[review(BOT, M, commit="h0", at="2026-01-02T01:00:00Z"),
                           review("alice", "", commit="h0", state="CHANGES_REQUESTED", at="2026-01-02T02:00:00Z"),
                           review(BOT, M, at="2026-01-02T03:00:00Z")])


def facts(gh):
    t, calls = counted(gh)
    t.facts(7)
    return calls


def check(gh, node="fix"):
    t, calls = counted(gh)
    gate.check(node, {"id": "e", "ticket": 7}, t, nodes.load(), G.CONFIG)
    return calls


def apply(gh, *effects, tracker=counted):
    t, calls = tracker(gh)
    for e in effects:
        t.apply(e)
    return calls


def outcome(gh, node, name, payload):
    t, calls = counted(gh)
    gate.apply(node, name, {"ticket": 7, **payload}, t, nodes.load())
    return calls


def sweep(gh, dry=False):
    t, calls = counted(gh)
    with contextlib.redirect_stderr(io.StringIO()):
        gate.sweep(t, nodes.load(), dry)
    return calls


def approved():
    gh = FakeGh(labels=["factory:approved", "risk:low"], reviews=[review(BOT, M)])
    gh.behind_by = 0
    return gh


STEPS = {
    "facts in-review": lambda: facts(in_review()),
    "facts approved (behind)": lambda: facts(approved()),
    "facts needs-answers": lambda: facts(FakeGh(labels=["factory:needs-answers"], comments=[
        {"body": "why?", "created_at": "2026-01-01T00:00:00Z", "user": {"login": BOT, "type": "Bot"}},
        {"body": "because", "created_at": "2026-01-03T00:00:00Z", "user": {"login": "eve", "type": "User"}}])),
    "facts split": lambda: facts(FakeGh(labels=["factory:split"], sub=["a", "b"])),
    "facts two repositories": lambda: (lambda t: (t[0].facts(7), t[1])[1])(
        two_repos(TwoRepositoryGh(reviews=[review(BOT, M)], other_reviews=[review(BOT, M, commit="h9")]))),
    "check fix": lambda: check(FakeGh(labels=["factory:changes-requested", "risk:low"], reviews=[review(BOT, C)])),
    "apply stage": lambda: apply(FakeGh(), eff("stage", "approved")),
    "apply stage done": lambda: apply(FakeGh(labels=["factory:merged"]), eff("stage", "done")),
    "apply label pr": lambda: apply(FakeGh(), eff("label", "factory:needs-human", on="pr")),
    "apply unlabel pr": lambda: apply(FakeGh(labels=["factory:in-review", "factory:fix-mid"]), eff("unlabel", "factory:fix-*", on="pr")),
    "apply label issue": lambda: apply(FakeGh(), eff("label", "x")),
    "apply set-field": lambda: apply(FakeGh(), eff("set-field", {"risk": "high"})),
    "apply assign pr": lambda: apply(FakeGh(), eff("assign", "alice", on="pr")),
    "apply comment": lambda: apply(FakeGh(), eff("comment", "hi", run_id="r")),
    "apply comment pr": lambda: apply(FakeGh(), eff("comment", "hi", on="pr", run_id="r")),
    "apply review": lambda: apply(FakeGh(), eff("review", "approve", focus="full", summary="ok", run_id="r")),
    "apply join": lambda: apply(FakeGh(reviews=[review(BOT, C)]), eff("join", "all-approve", tier="mid")),
    "apply merge": lambda: apply(approved(), eff("merge", "match-head", head="h1")),
    "apply merge two repositories": lambda: apply(TwoRepositoryGh(), eff("merge", "match-head", heads={"primary": "h1", "other": "h9"}),
                                                  tracker=two_repos),
    "outcome reviewer approve": lambda: outcome(FakeGh(), "review", "approve",
                                                {"focus": "full", "summary": "ok", "findings": [], "run_id": "r"}),
    "outcome fix done": lambda: outcome(FakeGh(labels=["factory:changes-requested", "risk:low", "factory:fix-mid"],
                                               reviews=[review(BOT, C)]), "fix", "done", {"run_id": "r"}),
    "sweep in-review": lambda: sweep(in_review()),
    "sweep approved dry": lambda: sweep(approved(), dry=True),
}


class GhCallsTest(unittest.TestCase):
    def test_no_step_needs_more_gh_calls_than_the_baseline(self):
        got = {}
        with patch.object(G.M_GH.time, "sleep"), patch.object(GitHubGateTracker, "RECENT_MERGE_SECONDS", 0):
            for name, step in STEPS.items():
                try:
                    got[name] = len(step())
                except Exception as e:  # noqa: BLE001  a step that fails is reported, not counted
                    got[name] = f"{type(e).__name__}: {e}"
        if os.environ.get("FACTORY_GH_CALLS") == "print":
            print("\n".join(f"    {k!r}: {v}," for k, v in got.items()), file=sys.stderr)
        self.assertEqual(set(got), set(BASELINE), "a step without a baseline, or a baseline without a step")
        for name, n in got.items():
            with self.subTest(name):
                self.assertIsInstance(n, int, n)
                self.assertLessEqual(n, BASELINE[name])


if __name__ == "__main__":
    unittest.main()

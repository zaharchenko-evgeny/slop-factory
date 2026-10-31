#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/gate_github.py with a stubbed gh. Run: uv run factory/tests/test_gate_github.py"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import gate  # noqa: E402
import nodes  # noqa: E402
from gate_github import GitHubGateTracker  # noqa: E402

CONFIG = yaml.safe_load((ROOT / "factory.yaml").read_text())
STAGES, BOT, REPO = CONFIG["tracker"]["stages"], CONFIG["targets"]["air"]["bot_login"], "o/r"
M = "<!-- factory:review focus=full round=1 verdict=approve -->"


class FakeGh:
    """The gh calls of the adapter over one ticket (7) with a PR on ez/7-x; records writes."""

    def __init__(self, labels=("factory:in-review", "risk:low"), reviews=(), comments=(), checks=("success",), sub=()):
        self.labels, self.reviews, self.comments = list(labels), list(reviews), list(comments)
        self.checks, self.sub, self.writes, self.merged = list(checks), list(sub), [], False
        self.blocked = [{"state": "open", "id": 1}, {"state": "closed", "id": 2}]
        self.open_issues = []

    def __call__(self, args):
        a = next((x for x in args if x.startswith("repos/")), " ".join(args))  # the API path
        a = a + " POST" if "POST" in args else a
        assert args[:2] != ["api", "user"], "gh api user must never be called"
        if "check-runs" in a:
            return "\n".join(json.dumps({"conclusion": c}) for c in self.checks)
        if a.endswith("pulls?state=all&per_page=100"):
            head = lambda ref, repo: {"ref": ref, "repo": {"full_name": repo} if repo else None}  # noqa: E731
            return "\n".join(json.dumps(p) for p in (
                {"number": 5, "state": "open", "head": head("ez/7-x", REPO)},
                {"number": 6, "state": "open", "head": head("ez/70-other", REPO)},
                {"number": 8, "state": "open", "head": head("ez/7-fork", "evil/r")},
                {"number": 9, "state": "open", "head": head("ez/7-gone", None)}))
        if a.endswith("/pulls/5"):
            return json.dumps({"number": 5, "merged": self.merged, "mergeable": True, "head": {"sha": "h1", "ref": "ez/7-x"},
                               "requested_reviewers": [{"login": "alice"}]})
        if a.endswith("/pulls/5/reviews"):
            return "\n".join(json.dumps(r) for r in self.reviews)
        if a.endswith(("issues/7", "issues/5")):
            return json.dumps({"labels": [{"name": x} for x in self.labels]})
        if a.endswith("issues/7/dependencies/blocked_by"):
            return "\n".join(json.dumps(b) for b in self.blocked)
        if a.endswith("issues/7/comments"):
            return "\n".join(json.dumps({"body": c}) for c in self.comments)
        if a.endswith("issues?state=open&per_page=100"):
            return "\n".join(json.dumps(i) for i in self.open_issues)
        if a.endswith("issues/7/sub_issues"):
            return "\n".join(json.dumps({"title": t}) for t in self.sub)
        self.writes.append(args)
        if args[1] == "edit":
            it = iter(args[5:])
            for flag, v in zip(it, it):
                if flag == "--add-label":
                    self.labels.append(v)
                else:
                    self.labels.remove(v)
        return json.dumps({"id": 999}) if a.endswith("/issues POST") else ""


def review(author, body, commit="h1", state="COMMENTED"):
    return {"user": {"login": author}, "body": body, "commit_id": commit, "state": state}


def tr(gh):
    return GitHubGateTracker(REPO, STAGES, BOT, fix_cap=3, run=gh)


def eff(kind, value, **kw):
    return {"ticket": 7, "kind": kind, "value": value, **kw}


class FactsTest(unittest.TestCase):
    def test_facts(self):
        gh = FakeGh(reviews=[review(BOT, M)])
        f = tr(gh).facts(7)
        self.assertEqual((f["stage"], f["attrs"], f["blockers_open"]), ("in-review", ["risk:low"], 1))
        self.assertEqual((f["pr"], f["head"], f["mergeable"], f["ci"]), (5, "h1", True, "green"))  # branch, not ez/70-
        self.assertEqual(f["requested_reviewers"], ["alice"])
        self.assertEqual((f["reviews"][0]["author"], f["reviews"][0]["state"], f["reviews"][0]["commit_id"]), (BOT, "COMMENTED", "h1"))
        self.assertEqual((f["reviews"][0]["focus"], f["reviews"][0]["round"], f["seen"]), ("full", 1, []))
        self.assertEqual((f["focus_missing"], f["round_complete"], f["round_head"]), ([], True, "h1"))

    def test_human_marker_is_not_an_agent_review(self):
        f = tr(FakeGh(reviews=[review("mallory", M)])).facts(7)
        self.assertFalse(f["reviews"][0]["agent"])
        self.assertEqual((f["focus_missing"], f["round_complete"]), (["full"], False))

    def test_ci_and_old_head(self):
        self.assertEqual(tr(FakeGh(checks=["success", "failure"])).facts(7)["ci"], "red")
        self.assertEqual(tr(FakeGh(checks=["success", None])).facts(7)["ci"], "pending")
        self.assertEqual(tr(FakeGh(checks=[])).facts(7)["ci"], "none")
        f = tr(FakeGh(reviews=[review(BOT, M, commit="old")])).facts(7)
        self.assertEqual((f["focus_missing"], f["round_head"]), (["full"], "old"))

    def test_no_stage_label(self):
        self.assertEqual(tr(FakeGh(labels=["x"])).facts(7)["stage"], "none")


class EffectsTest(unittest.TestCase):
    def twice(self, gh, e):
        t = tr(gh)
        return t.apply(e), len(gh.writes), t.apply(e), len(gh.writes)

    def test_stage(self):
        gh = FakeGh()
        self.assertEqual(self.twice(gh, eff("stage", "approved")), (True, 1, False, 1))
        self.assertEqual(sorted(gh.labels), ["factory:approved", "risk:low"])

    def test_label_unlabel(self):
        gh = FakeGh()
        self.assertEqual(self.twice(gh, eff("label", "factory:fix-mid")), (True, 1, False, 1))
        self.assertEqual(self.twice(gh, eff("unlabel", "factory:fix-mid")), (True, 2, False, 2))

    def test_set_field(self):
        gh = FakeGh()
        self.assertEqual(self.twice(gh, eff("set-field", {"risk": "high"})), (True, 1, False, 1))
        self.assertEqual(sorted(gh.labels), ["factory:in-review", "risk:high"])

    def test_comment(self):
        gh = FakeGh()
        self.assertEqual(self.twice(gh, eff("comment", "hi"))[:2], (True, 1))
        gh = FakeGh(comments=["[factory] hi"])
        self.assertFalse(tr(gh).apply(eff("comment", "hi")))
        self.assertEqual(gh.writes, [])

    def test_review_pinned_with_marker(self):
        gh = FakeGh()
        self.assertTrue(tr(gh).apply(eff("review", "approve-at-head", focus="full", round=2)))
        w = gh.writes[0]
        self.assertIn("commit_id=h1", w)
        self.assertIn("event=COMMENT", w)
        self.assertIn("body=[factory] <!-- factory:review focus=full round=2 verdict=approve -->", w)
        gh = FakeGh(reviews=[review(BOT, M)])  # already reviewed at head: skip
        self.assertFalse(tr(gh).apply(eff("review", "request-changes-at-head", focus="full")))
        gh = FakeGh(reviews=[review("mallory", M)])  # a human's marker does not skip it
        self.assertTrue(tr(gh).apply(eff("review", "request-changes-at-head", focus="full")))
        self.assertIn("event=COMMENT", gh.writes[0])
        self.assertIn("verdict=changes", gh.writes[0][-1])

    def test_verdict_from_marker_or_state(self):
        c = "<!-- factory:review focus=full round=1 verdict=changes -->"
        rv = tr(FakeGh(reviews=[review(BOT, c), review("alice", "", state="CHANGES_REQUESTED"),
                                review("bob", "", state="APPROVED"), review("eve", "")])).facts(7)["reviews"]
        self.assertEqual([x["verdict"] for x in rv], ["changes", "changes", "approve", None])

    def test_label_on_pr(self):
        gh = FakeGh()
        self.assertTrue(tr(gh).apply(eff("label", "factory:fix-mid", on="pr")))
        self.assertEqual(gh.writes[0][:4], ["pr", "edit", "5", "-R"])
        self.assertIn("--add-label", gh.writes[0])
        gh = FakeGh()
        tr(gh).apply(eff("label", "factory:fix-mid"))
        self.assertEqual(gh.writes[0][:3], ["issue", "edit", "7"])

    def test_nodes_yaml_on_pr_reaches_the_effect(self):
        n = nodes.load()["nodes"]["join"]["outcomes"]["changes"]["effects"][0]
        self.assertEqual(gate.resolve(n, {"tier": "mid"}, 7)["on"], "pr")

    def test_comment_trailer(self):
        gh = FakeGh()
        tr(gh).apply(eff("comment", "hi", run_id="r1"))
        self.assertTrue(gh.writes[0][-1].endswith("Factory-Run: r1"))

    def test_create_issues(self):
        gh = FakeGh(sub=["old"])
        self.assertTrue(tr(gh).apply(eff("create-issues", [{"title": "new", "body": "b"}, "old"])))
        self.assertEqual(len(gh.writes), 3)  # create, sub-issue link, blocked-by link
        self.assertIn("issue_id=999", gh.writes[2])
        self.assertFalse(tr(FakeGh(sub=["old"])).apply(eff("create-issues", ["old"])))

    def test_create_issues_retry_converges(self):
        gh = FakeGh()
        gh.open_issues = [{"id": 999, "title": "A", "body": "x\n\n<!-- factory:child of=7 -->"}]  # created by a run that died before linking
        self.assertTrue(tr(gh).apply(eff("create-issues", ["A"])))
        self.assertEqual([w[2] for w in gh.writes], ["POST", "POST"])
        self.assertTrue(all("issues/7/" in w[3] for w in gh.writes))  # linked, not created again

    def test_create_issues_does_not_adopt_foreign_issue(self):
        gh = FakeGh()
        gh.open_issues = [{"id": 555, "title": "A", "body": "<!-- factory:child of=10 -->"}, {"id": 556, "title": "A", "body": ""}]
        self.assertTrue(tr(gh).apply(eff("create-issues", ["A"])))
        self.assertIn("title=A", gh.writes[0])  # a new issue, carrying this ticket's marker
        self.assertIn("factory:child of=7", gh.writes[0][-3])
        self.assertIn("labels[]=factory:draft", gh.writes[0])

    def test_fix_rounds_grow_per_reviewed_head(self):
        self.assertEqual(tr(FakeGh(reviews=[review(BOT, M)])).facts(7)["fix_rounds"], 0)
        self.assertEqual(tr(FakeGh(reviews=[review(BOT, M, commit="old")])).facts(7)["fix_rounds"], 1)
        gh = FakeGh(reviews=[review(BOT, M, commit="old")])
        tr(gh).apply(eff("review", "approve-at-head", focus="full"))
        self.assertIn("round=2", gh.writes[0][-1])

    def test_fork_pr_is_ignored(self):
        self.assertEqual(tr(FakeGh()).facts(7)["pr"], 5)

    def test_merge(self):
        gh = FakeGh()
        self.assertTrue(tr(gh).apply(eff("merge", "match-head")))
        self.assertEqual(gh.writes[0][:3], ["pr", "merge", "5"])
        self.assertEqual(gh.writes[0][-2:], ["--match-head-commit", "h1"])
        gh.merged = True
        self.assertFalse(tr(gh).apply(eff("merge", "match-head")))

    def test_seen_is_a_no_op(self):
        gh = FakeGh()
        self.assertFalse(tr(gh).apply(eff("seen", "e1")))
        self.assertEqual(gh.writes, [])


class GateTest(unittest.TestCase):
    def test_duplicate_event_caught_by_stage(self):
        gh, nd = FakeGh(labels=["factory:in-dev"]), nodes.load()
        payload = {"ticket": 7, "event": "e1"}
        self.assertEqual(gate.apply("implement", "done", payload, tr(gh), nd)[0], gate.GO)
        code, out = gate.apply("implement", "done", payload, tr(gh), nd)
        self.assertEqual((code, out["applied"]), (gate.NO_GO, False))
        self.assertEqual(len(gh.writes), 1)

    def test_bad_values_and_bad_tracker(self):
        facts = {"fix_rounds": 3, "fix_cap": 3, "ci": "green"}
        self.assertFalse(gate.holds({"fix-rounds": "below-cap"}, facts, {}))
        self.assertTrue(gate.holds({"fix-rounds": "at-cap"}, facts, {}))
        with self.assertRaises(gate.Bad):
            gate.holds({"ci": "greeen"}, facts, {})
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            f.write("{")
            f.flush()
            self.assertEqual(gate.main(["check", "--node", "intake", "--event", f.name, "--tracker", f"memory:{f.name}"]), 2)

    def test_cli_with_stub_gh(self):
        with tempfile.TemporaryDirectory() as d:
            stub = Path(d, "gh")
            stub.write_text('#!/bin/sh\ncase "$*" in\n*issues/7) echo \'{"labels":[{"name":"factory:ready"}]}\';;\n'
                            '*blocked_by) ;;\n*pulls*) ;;\nesac\n')
            stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
            ev = Path(d, "ev.json")
            ev.write_text('{"id":"e1","ticket":7}')
            p = subprocess.run([sys.executable, str(ROOT / "factory/gate.py"), "check", "--node", "intake", "--event", str(ev),
                                "--tracker", "github"], capture_output=True, text=True, env={**os.environ, "FACTORY_GH": str(stub)})
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertEqual(json.loads(p.stdout)["context"]["stage"], "ready")


if __name__ == "__main__":
    unittest.main()

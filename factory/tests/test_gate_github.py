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
import config as cfg  # noqa: E402
cfg.set_team(Path(__file__).resolve().parents[2])
import gate  # noqa: E402
import nodes  # noqa: E402
from gate_github import GitHubGateTracker  # noqa: E402

CONFIG = cfg.load()
STAGES, BOT, REPO = CONFIG["tracker"]["stages"], CONFIG["targets"]["air"]["bot_login"], "o/r"
M = "<!-- factory:review focus=full round=1 verdict=approve -->"


class FakeGh:
    """The gh calls of the adapter over one ticket (7) with a PR on ez/7-x; records writes."""

    def __init__(self, labels=("factory:in-review", "risk:low"), reviews=(), comments=(), checks=("success",), sub=(), requested=()):
        self.requested = list(requested)
        self.roles = {"alice": "write", "bob": "admin", "eve": "read"}
        self.labels, self.reviews, self.comments = list(labels), list(reviews), list(comments)
        self.checks, self.sub, self.writes, self.merged = list(checks), list(sub), [], False
        self.blocked = [{"state": "open", "id": 1}, {"state": "closed", "id": 2}]
        self.open_issues, self.state, self.sub_state = [], "open", ["open"] * 9

    def __call__(self, args):
        a = next((x for x in args if x.startswith("repos/")), " ".join(args))  # the API path
        a = a + " POST" if "POST" in args else a
        assert args[:2] != ["api", "user"], "gh api user must never be called"
        if "actions/runs?head_sha=" in a:  # one workflow per entry of `checks`; None = still running
            return "\n".join(json.dumps({"workflow_id": k, "event": "pull_request", "run_number": 1,
                                          "status": "completed" if c else "in_progress", "conclusion": c})
                             for k, c in enumerate(self.checks))
        if a.endswith("pulls?state=all&per_page=100"):
            head = lambda ref, repo: {"ref": ref, "repo": {"full_name": repo} if repo else None}  # noqa: E731
            return "\n".join(json.dumps(p) for p in (
                {"number": 5, "state": "open", "head": head("ez/7-x", REPO)},
                {"number": 6, "state": "open", "head": head("ez/70-other", REPO)},
                {"number": 8, "state": "open", "head": head("ez/7-fork", "evil/r")},
                {"number": 9, "state": "open", "head": head("ez/7-gone", None)}))
        if "issues?state=" in a and "labels=" in a:
            want = a.split("labels=")[1].split("&")[0]
            on = want in self.labels and (self.state == "open" or "state=all" in a)
            return json.dumps({"number": 7}) if on else ""
        if a.endswith("/pulls/5"):
            return json.dumps({"number": 5, "merged": self.merged, "state": "closed" if self.merged else "open", "mergeable": True, "head": {"sha": "h1", "ref": "ez/7-x"},
                               "requested_reviewers": [{"login": u} for u in self.requested]})
        if a.endswith("/commits/h1"):
            return json.dumps({"commit": {"committer": {"date": getattr(self, "head_date", "2026-01-02T00:00:00Z")}}})
        if "/collaborators/" in a:
            who = a.split("/collaborators/")[1].split("/")[0]
            if who not in self.roles:
                raise RuntimeError("404")
            return json.dumps({"role_name": self.roles[who]})
        if a.endswith("/parent"):
            if getattr(self, "parent_of", None) is None:
                raise RuntimeError("HTTP 404: Not Found")
            return json.dumps({"number": self.parent_of})
        if a.endswith("issues/7/events"):
            return "\n".join([json.dumps({"event": "labeled", "label": {"name": n}, "created_at": t})
                              for n, t in getattr(self, "label_events", [])] +
                             [json.dumps({"event": "labeled", "label": {"name": "factory:implement-cheap"}})
                              for _ in range(getattr(self, "impl_runs", 0))])
        if a.endswith("issues/5/events"):
            return "\n".join(json.dumps({"event": "labeled", "label": {"name": "factory:fix-mid"}, "created_at": t})
                             for t in getattr(self, "fix_events", []))
        if a.endswith("/pulls/5/reviews"):
            return "\n".join(json.dumps(r) for r in self.reviews)
        if a.endswith(("issues/7", "issues/5")):
            return json.dumps({"labels": [{"name": x} for x in self.labels], "state": self.state})
        if a.endswith("issues/7/dependencies/blocking"):
            return "\n".join(json.dumps(i) for i in getattr(self, "blocking_issues", []))
        if a.endswith("issues/7/dependencies/blocked_by"):
            return "\n".join(json.dumps(b) for b in self.blocked)
        if a.endswith("issues/7/comments"):
            return "\n".join(json.dumps({"body": c}) for c in self.comments)
        if a.endswith("issues?state=open&per_page=100"):
            return "\n".join(json.dumps(i) for i in self.open_issues)
        if a.endswith("issues/7/sub_issues"):
            reasons, labels = getattr(self, "sub_reason", ["completed"] * 9), getattr(self, "sub_labels", [[]] * 9)
            return "\n".join(json.dumps({"number": i + 1, "title": t, "state": st, "state_reason": r if st == "closed" else None,
                                         "labels": [{"name": x} for x in ls]})
                             for i, (t, st, r, ls) in enumerate(zip(self.sub, self.sub_state, reasons, labels)))
        self.writes.append(args)
        if args[1] == "close":
            self.state = "closed"
        if args[1] == "edit":
            it = iter(args[5:])
            for flag, v in zip(it, it):
                if flag == "--add-label":
                    self.labels.append(v)
                else:
                    self.labels.remove(v)
        return json.dumps({"id": 999, "number": 999}) if a.endswith("/issues POST") else ""


def review(author, body, commit="h1", state="COMMENTED", assoc="OWNER", at="2026-01-02T01:00:00Z"):
    return {"user": {"login": author, "type": "Bot" if author.endswith("[bot]") else "User"}, "body": body,
            "commit_id": commit, "state": state, "author_association": assoc, "submitted_at": at}


def tr(gh):
    return GitHubGateTracker(REPO, STAGES, BOT, fix_cap=3, run=gh)


def eff(kind, value, **kw):
    return {"ticket": 7, "kind": kind, "value": value, **kw}


class FactsTest(unittest.TestCase):
    def test_facts(self):
        gh = FakeGh(reviews=[review(BOT, M)], requested=["alice"])
        f = tr(gh).facts(7)
        self.assertEqual((f["stage"], f["attrs"], f["blockers_open"]), ("in-review", ["risk:low"], 1))
        self.assertEqual((f["pr"], f["head"], f["mergeable"], f["ci"]), (5, "h1", True, "green"))  # branch, not ez/70-
        self.assertEqual(f["requested_reviewers"], ["alice"])
        self.assertEqual((f["reviews"][0]["author"], f["reviews"][0]["state"], f["reviews"][0]["commit_id"]), (BOT, "COMMENTED", "h1"))
        self.assertEqual((f["reviews"][0]["focus"], f["reviews"][0]["round"], f["seen"]), ("full", 1, []))
        self.assertEqual((f["focus_missing"], f["round_complete"], f["round_head"]), ([], False, "h1"))  # alice is still requested
        f = tr(FakeGh(reviews=[review(BOT, M)])).facts(7)
        self.assertEqual((f["round_complete"], f["round_verdict"]), (True, "approve"))

    def test_human_marker_is_not_an_agent_review(self):
        f = tr(FakeGh(reviews=[review("mallory", M)])).facts(7)
        self.assertFalse(f["reviews"][0]["agent"])
        self.assertEqual((f["focus_missing"], f["round_complete"]), (["full"], False))

    def test_ci_and_old_head(self):
        self.assertEqual(tr(FakeGh(checks=["success", "failure"])).facts(7)["ci"], "red")
        self.assertEqual(tr(FakeGh(checks=["success", None])).facts(7)["ci"], "pending")
        self.assertEqual(tr(FakeGh(checks=[])).facts(7)["ci"], "none")

    def test_ci_rerun_replaces_a_failure(self):
        class Rerun(FakeGh):
            def __call__(self, args):
                if any("actions/runs?head_sha=" in x for x in args):
                    return "\n".join(json.dumps({"workflow_id": 1, "event": "pull_request", "run_number": 1,
                                                  "run_attempt": n, "status": "completed", "conclusion": c})
                                      for n, c in ((1, "failure"), (2, "success")))
                return FakeGh.__call__(self, args)
        self.assertEqual(tr(Rerun()).facts(7)["ci"], "green")
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
        self.assertIn("body=[factory]\n\n<!-- factory:review focus=full round=2 verdict=approve -->", w)
        gh = FakeGh(reviews=[review(BOT, M)])  # already reviewed at head: skip
        self.assertFalse(tr(gh).apply(eff("review", "request-changes-at-head", focus="full")))
        gh = FakeGh(reviews=[review("mallory", M)])  # a human's marker does not skip it
        self.assertTrue(tr(gh).apply(eff("review", "request-changes-at-head", focus="full")))
        self.assertIn("event=COMMENT", gh.writes[0])
        self.assertIn("verdict=changes", gh.writes[0][-1])

    def test_changes_review_body_carries_findings(self):
        gh = FakeGh()
        found = [{"id": "M1", "file": "a.py:2", "finding": "name it", "severity": "minor"},
                 {"id": "B1", "file": "b.py:9", "finding": "crashes on empty", "severity": "blocking"}]
        self.assertTrue(tr(gh).apply(eff("review", "request-changes-at-head", focus="full", round=1,
                                         summary="Close.", findings=found, run_id="r9")))
        body = gh.writes[0][-1].removeprefix("body=")
        self.assertEqual(body.split("\n\n")[0], "[factory] Close.")
        self.assertLess(body.index("**B1** b.py:9 — crashes on empty"), body.index("**M1** a.py:2 — name it"))
        self.assertTrue(body.endswith("Factory-Run: r9"))
        gh = FakeGh(reviews=[review(BOT, body)])  # the longer body still parses
        self.assertEqual(tr(gh).facts(7)["reviews"][0]["verdict"], "changes")

    def test_review_apply_adds_no_fix_label(self):
        out = nodes.load()["nodes"]["review"]["outcomes"]
        for o in out.values():
            self.assertNotIn("join", [k for e in o["effects"] for k in e])
            self.assertFalse(any("fix" in str(v) for e in o["effects"] for v in e.values()))

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

    def test_create_issues_bodies_risk_and_order(self):
        gh = FakeGh(labels=("factory:split", "risk:high"))
        kids = [{"title": "A", "body": "scope A"}, {"title": "B", "body": "scope B"}]
        self.assertTrue(tr(gh).apply(eff("create-issues", kids)))
        creates = [w for w in gh.writes if "labels[]=factory:draft" in w]
        self.assertEqual(len(creates), 2)
        for w, k in zip(creates, kids):
            self.assertIn(f"body={k['body']}\n\n<!-- factory:child of=7 -->", w)
            self.assertIn("labels[]=risk:high", w)
        chain = [w for w in gh.writes if "/dependencies/blocked_by" in " ".join(w) and "issues/999/" in " ".join(w)]
        self.assertEqual(len(chain), 1)  # B (the second 999) is blocked by A

    def test_create_issues_retry_converges(self):
        gh = FakeGh()
        gh.open_issues = [{"id": 999, "number": 999, "title": "A", "body": "\n\n<!-- factory:child of=7 -->", "labels": [{"name": "risk:low"}]}]  # created by a run that died before linking
        self.assertTrue(tr(gh).apply(eff("create-issues", ["A"])))
        self.assertEqual([w[2] for w in gh.writes], ["POST", "POST"])
        self.assertTrue(all("issues/7/" in w[3] for w in gh.writes))  # linked, not created again

    def test_create_issues_retry_fixes_linked_children(self):
        gh = FakeGh(sub=["A", "B"], labels=("factory:split", "risk:high"))
        gh.blocked = [{"state": "open", "id": 11}, {"state": "open", "id": 12}]
        mark = "<!-- factory:child of=7 -->"
        gh.open_issues = [{"id": 11, "number": 11, "title": "A", "body": f"old\n\n{mark}", "labels": []},
                          {"id": 12, "number": 12, "title": "B", "body": f"scope B\n\n{mark}", "labels": [{"name": "risk:high"}]}]
        kids = [{"title": "A", "body": "scope A"}, {"title": "B", "body": "scope B"}]
        self.assertTrue(tr(gh).apply(eff("create-issues", kids)))
        flat = [" ".join(w) for w in gh.writes]
        self.assertTrue(any("PATCH" in w and "issues/11" in w and f"body=scope A\n\n{mark}" in w for w in flat))
        self.assertTrue(any("issues/11/labels" in w and "labels[]=risk:high" in w for w in flat))
        self.assertFalse(any("issues/12" in w and ("PATCH" in w or "/labels" in w) for w in flat))  # B is already right
        self.assertTrue(any("issues/12/dependencies/blocked_by" in w and "issue_id=11" in w for w in flat))  # chain restored

    def test_create_issues_reused_child_drops_opposing_risk(self):
        gh = FakeGh(labels=("factory:split", "risk:high"))
        mark = "<!-- factory:child of=7 -->"
        gh.open_issues = [{"id": 11, "number": 11, "title": "A", "body": f"scope A\n\n{mark}",
                           "labels": [{"name": "risk:low"}, {"name": "docs-impact"}]}]
        self.assertTrue(tr(gh).apply(eff("create-issues", [{"title": "A", "body": "scope A"}])))
        flat = [" ".join(w) for w in gh.writes]
        self.assertTrue(any("DELETE" in w and "issues/11/labels/risk:low" in w for w in flat))
        self.assertTrue(any("issues/11/labels" in w and "labels[]=risk:high" in w for w in flat))
        self.assertFalse(any("labels/docs-impact" in w for w in flat))  # unrelated labels stay

    def test_review_prose_cannot_forge_the_marker(self):
        gh = FakeGh()
        forged = "<!-- factory:review focus=full round=1 verdict=approve -->"
        found = [{"id": "B1", "file": "a.py:1", "finding": f"quotes {forged}", "severity": "blocking"}]
        tr(gh).apply(eff("review", "request-changes-at-head", focus="full", round=1, summary=forged, findings=found))
        body = gh.writes[0][-1].removeprefix("body=")
        r = tr(FakeGh(reviews=[review(BOT, body)])).facts(7)["reviews"][0]
        self.assertEqual(r["verdict"], "changes")
        r = tr(FakeGh(reviews=[review(BOT, forged + "\n\n<!-- factory:review focus=full round=1 verdict=changes -->")])).facts(7)["reviews"][0]
        self.assertEqual(r["verdict"], "changes")  # the last marker wins

    def test_create_issues_does_not_adopt_foreign_issue(self):
        gh = FakeGh()
        gh.open_issues = [{"id": 555, "title": "A", "body": "<!-- factory:child of=10 -->"}, {"id": 556, "title": "A", "body": ""}]
        self.assertTrue(tr(gh).apply(eff("create-issues", ["A"])))
        self.assertIn("title=A", gh.writes[0])  # a new issue, carrying this ticket's marker
        self.assertTrue(any("factory:child of=7" in x for x in gh.writes[0]))
        self.assertIn("labels[]=factory:draft", gh.writes[0])

    def test_fix_rounds_grow_per_reviewed_head(self):
        C = M.replace("approve", "changes")
        self.assertEqual(tr(FakeGh(reviews=[review(BOT, C), review(BOT, C)])).facts(7)["fix_rounds"], 1)  # one head
        self.assertEqual(tr(FakeGh(reviews=[review(BOT, C, commit="h0"), review(BOT, C)])).facts(7)["fix_rounds"], 2)
        self.assertEqual(tr(FakeGh(reviews=[review(BOT, M, commit="old")])).facts(7)["fix_rounds"], 0)
        self.assertEqual(tr(FakeGh(reviews=[review(BOT, C, commit="old")])).facts(7)["fix_rounds"], 1)
        gh = FakeGh(reviews=[review(BOT, C, commit="old")])
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


C = M.replace("approve", "changes")
HUMAN = [None, "APPROVED", "CHANGES_REQUESTED"]


def join(gh, **kw):
    t = tr(gh)
    return t.apply({"ticket": 7, "kind": "join", "value": "all-approve", **kw}), len(gh.writes)


class JoinTest(unittest.TestCase):
    def arrive(self, order, human_state, agent_body):
        """Reviews arrive one by one; alice is requested until her review. Returns the joined gh."""
        gh = FakeGh()
        gh.requested = ["alice"]
        for who in order:
            if who == "agent":
                gh.reviews.append(review(BOT, agent_body))
            else:
                gh.requested = []
                gh.reviews.append(review("alice", "", state=human_state))
            join(gh)
        return gh

    def test_every_arrival_order(self):
        for order in (["agent", "human"], ["human", "agent"]):
            for human, agent, stage, fix in (
                    ("APPROVED", M, "approved", False), ("CHANGES_REQUESTED", M, "changes-requested", True),
                    ("APPROVED", C, "changes-requested", True), ("CHANGES_REQUESTED", C, "changes-requested", True)):
                with self.subTest(order=order, human=human, agent=agent[-16:]):
                    gh = self.arrive(order, human, agent)
                    self.assertEqual(STAGES[stage] in gh.labels, True)
                    self.assertEqual(sum(x.startswith("factory:fix-") for x in gh.labels), int(fix))
                    self.assertEqual(len(gh.writes), 2 if fix else 1)  # nothing written before the last review

    def test_open_round_does_nothing(self):
        gh = FakeGh(reviews=[review(BOT, M)], requested=["alice"])
        self.assertEqual(join(gh), (False, 0))
        gh = FakeGh(reviews=[review(BOT, M)], checks=["success", None])
        self.assertEqual(join(gh), (False, 0))

    def test_red_ci_is_a_fix(self):
        gh = FakeGh(reviews=[review(BOT, M)], checks=["failure"])
        self.assertTrue(join(gh)[0])
        self.assertIn("factory:fix-mid", gh.labels)
        self.assertEqual(gh.writes[0][:3], ["pr", "edit", "5"])

    def test_simultaneous_last_reviews_one_label(self):
        gh = FakeGh(reviews=[review(BOT, C)])
        join(gh, tier="cheap")
        n = len(gh.writes)
        self.assertEqual(join(gh, tier="cheap"), (False, n))  # the second join finds the stage set
        self.assertEqual([x for x in gh.labels if x.startswith("factory:fix-")], ["factory:fix-cheap"])

    def test_unmarked_reviews(self):
        gh = FakeGh(reviews=[review(BOT, M), review("bob", "", state="CHANGES_REQUESTED")])
        self.assertEqual(tr(gh).facts(7)["round_verdict"], "changes")  # a person blocks, even unrequested
        gh = FakeGh(reviews=[review(BOT, M), review("eve", "", state="CHANGES_REQUESTED")])
        self.assertEqual(tr(gh).facts(7)["round_verdict"], "approve")  # no write access
        bot = review("other[bot]", "", state="CHANGES_REQUESTED", assoc="NONE")
        gh = FakeGh(reviews=[review(BOT, M), bot, review(BOT, "no marker", state="CHANGES_REQUESTED")])
        f = tr(gh).facts(7)
        self.assertEqual((f["round_verdict"], f["ignored_reviews"]), ("approve", ["other[bot]", BOT]))
        t = GitHubGateTracker(REPO, STAGES, BOT, run=gh, trusted_bots=["other[bot]"])
        self.assertEqual(t.facts(7)["round_verdict"], "changes")

    def test_late_human_review_counts_in_the_next_round(self):
        gh = FakeGh(reviews=[review(BOT, C)])
        join(gh)  # round 1 ends: fix label, changes-requested
        self.assertIn("factory:changes-requested", gh.labels)
        gh.labels = ["factory:in-review", "risk:low"]  # the fix pushed; head is still h1 in the stub, the review names h0
        gh.reviews = [review(BOT, C, commit="h0", at="2026-01-02T01:00:00Z"),  # round 1 closes at 01:00
                      review("alice", "", commit="h0", state="CHANGES_REQUESTED", at="2026-01-02T02:00:00Z"),  # during the fix
                      review(BOT, M, at="2026-01-02T03:00:00Z")]  # round 2: the agent approves h1
        self.assertEqual(tr(gh).facts(7)["round_verdict"], "changes")  # the late review counts in round 2
        gh.fix_events = ["2026-01-02T01:30:00Z"]  # round 1's join started the fix at 01:30; alice came at 02:00
        gh.head_date = "2026-01-02T02:50:00Z"  # the fix pushed this head after its label
        self.assertEqual(tr(gh).facts(7)["round_verdict"], "changes")  # sent during the fix: counts in round 2
        gh.fix_events = ["2026-01-02T02:30:00Z"]  # the join ran after alice: the fix was given her request
        f = tr(gh).facts(7)
        self.assertEqual((f["round_complete"], f["waiting_reviewers"]), (False, ["alice"]))  # she decides again

    def test_fix_done_removes_its_command_label_so_round_2_can_add_it_again(self):
        gh = FakeGh(labels=["factory:changes-requested", "risk:low", "factory:fix-mid"])  # the stub shares labels
        self.assertTrue(tr(gh).apply({"ticket": 7, "kind": "unlabel", "value": "factory:fix-*", "on": "pr"}))
        self.assertIn("--remove-label", gh.writes[0])
        self.assertIn("factory:fix-mid", gh.writes[0])

    def test_fix_done_asks_again_who_requested_changes(self):
        # review 3 of #183: without this, a request counted by round 1 is dropped and nobody asks alice again
        gh = FakeGh(reviews=[review(BOT, M), review("alice", "", commit="h0", state="CHANGES_REQUESTED"),
                             review("bob", "", state="APPROVED")])
        gh.requested = []
        self.assertTrue(tr(gh).apply({"ticket": 7, "kind": "request-reviewers", "value": "changes"}))
        post = [w for w in gh.writes if "requested_reviewers" in " ".join(w)]
        self.assertEqual(len(post), 1)
        self.assertIn("reviewers[]=alice", post[0])
        self.assertNotIn("reviewers[]=bob", post[0])
        gh.requested = ["alice"]
        self.assertFalse(tr(gh).apply({"ticket": 7, "kind": "request-reviewers", "value": "changes"}))  # already asked
        self.assertFalse(tr(gh).facts(7)["round_complete"])  # round 2 waits for her

    def test_sweep_asks_waiting_reviewers_again_after_a_comment(self):
        # final review of #183, B1: a comment after the re-request drops her from requested_reviewers
        gh = FakeGh(reviews=[review(BOT, M, commit="h0", at="2026-01-02T01:00:00Z"),
                             review("alice", "", commit="h0", state="CHANGES_REQUESTED", at="2026-01-02T02:00:00Z"),
                             review(BOT, M, at="2026-01-02T04:00:00Z"),
                             review("alice", "looking", state="COMMENTED", at="2026-01-02T05:00:00Z")])
        gh.fix_events, gh.requested = ["2026-01-02T02:05:00Z"], []
        gh.head_date = "2026-01-02T02:50:00Z"  # the fix pushed this head after its label
        res, _ = gate.sweep(tr(gh), nodes.load())
        self.assertEqual([(r["node"], r["outcome"]) for r in res], [("join", "waiting")])
        self.assertTrue(any("reviewers[]=alice" in " ".join(w) for w in gh.writes))
        self.assertIn("factory:in-review", gh.labels)  # nothing moved

    def test_trusted_bot_handled_request_does_not_wait(self):
        # final review of #183, B2: a bot cannot be re-requested, so its handled request is resolved
        bot = review("rev[bot]", "", commit="h0", state="CHANGES_REQUESTED", assoc="NONE", at="2026-01-02T02:00:00Z")
        gh = FakeGh(reviews=[bot, review(BOT, M, at="2026-01-02T04:00:00Z")])
        gh.fix_events = ["2026-01-02T02:05:00Z"]
        gh.head_date = "2026-01-02T02:50:00Z"  # the fix pushed this head after its label
        t = GitHubGateTracker(REPO, STAGES, BOT, run=gh, trusted_bots=["rev[bot]"])
        f = t.facts(7)
        self.assertEqual((f["round_complete"], f["waiting_reviewers"], f["round_verdict"]), (True, [], "approve"))

    def test_agent_runs_count_since_the_last_ready(self):
        class G(FakeGh):
            def __call__(self, args):
                if any(x.endswith("issues/7/events") for x in args):
                    ev = lambda name, t: json.dumps({"event": "labeled", "label": {"name": name}, "created_at": t})
                    return "\n".join([ev("factory:implement-cheap", "2026-01-01T00:00:00Z")] * 8
                                      + [ev("factory:ready", "2026-01-02T00:00:00Z"), ev("factory:implement-mid", "2026-01-02T01:00:00Z")])
                return FakeGh.__call__(self, args)
        f = GitHubGateTracker(REPO, STAGES, BOT, run=G(), run_cap=8).facts(7)
        self.assertEqual(f["agent_runs"], 1)  # a person's ready starts a fresh budget

    def test_join_at_the_run_cap_blocks_instead_of_starting_a_fix(self):
        # review of PR #198, B1: the reviewer's own join adds the fix label, so the cap must hold there too
        gh = FakeGh(reviews=[review(BOT, C)])
        t = GitHubGateTracker(REPO, STAGES, BOT, run=gh, run_cap=0)
        t.apply({"ticket": 7, "kind": "join", "value": "all-approve", "tier": "mid"})
        self.assertIn("factory:blocked", gh.labels)
        self.assertFalse(any("factory:fix-" in x for x in gh.labels))

    def test_head_already_handed_to_a_fix_waits_for_its_push(self):
        # review of PR #198, B2: between the fix's apply and the platform's push the old head must not re-join,
        # and the fix node itself must still run
        gh = FakeGh(reviews=[review(BOT, C)])  # head h1 is committed 2026-01-02T00:00:00Z in the stub
        gh.fix_events = ["2026-01-02T05:00:00Z"]
        f = tr(gh).facts(7)
        self.assertTrue(f["handed_to_fix"])
        self.assertIsNone(gate.join_decision(f))
        self.assertEqual(join(gh), (False, 0))
        gh.labels = ["factory:changes-requested", "risk:low"]
        self.assertEqual(gate.check("fix", {"id": "e", "ticket": 7}, tr(gh), nodes.load(), CONFIG)[0], gate.GO)

    def test_fix_that_pushed_nothing_blocks(self):
        gh = FakeGh(labels=["factory:changes-requested", "risk:low"], reviews=[review(BOT, C)])
        gh.fix_events = ["2026-01-02T05:00:00Z"]
        orig = gate.local_head
        try:
            gate.local_head = lambda: "h1"  # the run's checkout still at the PR head: no new commit
            gate.apply("fix", "done", {"ticket": 7, "event": "e", "tier": "mid"}, tr(gh), nodes.load())
            self.assertIn("factory:blocked", gh.labels)
            gh.labels = ["factory:changes-requested", "risk:low"]
            gate.local_head = lambda: "h2"  # a new commit, pushed by the platform after the run
            gate.apply("fix", "done", {"ticket": 7, "event": "e2", "tier": "mid"}, tr(gh), nodes.load())
            self.assertIn("factory:in-review", gh.labels)
        finally:
            gate.local_head = orig

    def test_fix_rounds_count_heads_not_reviewers(self):
        two = [review(BOT, C.replace("focus=full", "focus=behaviour")), review(BOT, C.replace("focus=full", "focus=structure-security"))]
        self.assertEqual(tr(FakeGh(reviews=two)).facts(7)["fix_rounds"], 1)  # two reviewers, one round

    def test_ready_resets_fix_rounds_and_the_fix_handoff(self):
        class G(FakeGh):
            def __call__(self, args):
                if any(x.endswith("issues/7/events") for x in args):
                    return json.dumps({"event": "labeled", "label": {"name": "factory:ready"}, "created_at": "2026-01-03T00:00:00Z"})
                return FakeGh.__call__(self, args)
        gh = G(reviews=[review(BOT, C, at="2026-01-02T01:00:00Z")])  # change request before the resume
        gh.fix_events = ["2026-01-02T05:00:00Z"]  # its fix round, before the resume
        f = tr(gh).facts(7)
        self.assertEqual((f["fix_rounds"], f["handed_to_fix"]), (0, False))  # a fresh start after factory:ready
        self.assertEqual((f["round_complete"], f["round_verdict"]), (True, "changes"))  # the join decides again

    def test_change_request_counted_by_round_1_does_not_block_round_2(self):
        # B2 of #183: the bot reviews first, the human second; round 1 counts her request, round 2 must not loop on it
        gh = FakeGh(reviews=[review(BOT, M, commit="h0", at="2026-01-02T01:00:00Z"),
                             review("alice", "", commit="h0", state="CHANGES_REQUESTED", at="2026-01-02T02:00:00Z"),
                             review(BOT, M, at="2026-01-02T04:00:00Z")])
        gh.fix_events = ["2026-01-02T02:05:00Z"]  # round 1's join started the fix after her request
        gh.head_date = "2026-01-02T02:50:00Z"  # the fix pushed this head after its label
        f = tr(gh).facts(7)
        self.assertEqual((f["round_complete"], f["waiting_reviewers"]), (False, ["alice"]))  # waits, no loop
        self.assertTrue(join(gh)[0])  # the join asks her again and moves nothing
        self.assertTrue(any("reviewers[]=alice" in " ".join(w) for w in gh.writes))
        self.assertNotIn("factory:approved", gh.labels)
        gh.reviews.append(review("alice", "still looking", state="COMMENTED", at="2026-01-02T05:00:00Z"))
        self.assertFalse(tr(gh).facts(7)["round_complete"])  # a comment is no new decision
        gh.reviews.append(review("alice", "", state="APPROVED", at="2026-01-02T06:00:00Z"))
        self.assertEqual(tr(gh).facts(7)["round_verdict"], "approve")
        gh.requested = ["alice"]  # re-requested: she blocks until she reviews again
        self.assertFalse(tr(gh).facts(7)["round_complete"])

    def test_commented_review_leaves_a_required_human_pending(self):
        gh = FakeGh(reviews=[review(BOT, M), review("alice", "thanks", state="COMMENTED")], requested=["alice"])
        self.assertEqual(tr(gh).facts(7)["round_complete"], False)
        gh.requested = []  # she reviewed: GitHub dropped the request
        gh.reviews.append(review("alice", "", state="APPROVED"))
        self.assertEqual(tr(gh).facts(7)["round_verdict"], "approve")

    def test_comment_completes_the_human_part_and_does_not_approve(self):
        # GitHub drops the person from requested_reviewers on any review: a comment ends their part, as no approval
        gh = FakeGh(reviews=[review(BOT, M), review("alice", "thanks", state="COMMENTED")], requested=[])
        f = tr(gh).facts(7)
        self.assertEqual((f["round_complete"], f["round_verdict"]), (True, "approve"))  # the agent approval decides
        gh.reviews[0] = review(BOT, C)
        self.assertEqual(tr(gh).facts(7)["round_verdict"], "changes")  # the comment never approves

    def test_comment_after_changes_requested_still_blocks_until_approval(self):
        gh = FakeGh(reviews=[review(BOT, M), review("alice", "", state="CHANGES_REQUESTED", at="2026-01-02T02:00:00Z"),
                             review("alice", "ok?", state="COMMENTED", at="2026-01-02T03:00:00Z")])
        self.assertEqual(tr(gh).facts(7)["round_verdict"], "changes")  # a comment does not dismiss the request
        gh.reviews.append(review("alice", "", state="APPROVED", at="2026-01-02T04:00:00Z"))
        self.assertEqual(tr(gh).facts(7)["round_verdict"], "approve")

    def test_earlier_round_change_request_waits_for_its_author(self):
        old = "2026-01-01T00:00:00Z"  # before the head's push (2026-01-02): an earlier round had it
        gh = FakeGh(reviews=[review("alice", "", commit="h0", state="CHANGES_REQUESTED", at=old), review(BOT, M)])
        f = tr(gh).facts(7)
        self.assertEqual((f["round_complete"], f["waiting_reviewers"]), (False, ["alice"]))  # never approves
        gh.reviews.append(review("alice", "", commit="h0", state="CHANGES_REQUESTED", at="2026-01-02T03:00:00Z"))
        self.assertEqual(tr(gh).facts(7)["round_verdict"], "changes")  # a new request counts in this round

    def test_human_approval_of_round_1_does_not_block_round_2(self):
        gh = FakeGh(reviews=[review("alice", "", commit="h0", state="APPROVED", at="2026-01-01T00:00:00Z"), review(BOT, M)])
        self.assertEqual(tr(gh).facts(7)["round_verdict"], "approve")

    def test_permission_lookup_failure_is_not_no_access(self):
        gh = FakeGh(reviews=[review(BOT, M), review("zed", "", state="CHANGES_REQUESTED")])
        gh.roles = {}
        self.assertEqual(tr(gh).facts(7)["round_verdict"], "approve")  # 404: not a collaborator
        orig = gh.__call__
        class Boom(FakeGh):
            def __call__(self, args):
                if any("/collaborators/" in x for x in args):
                    raise RuntimeError("HTTP 403 rate limit")
                return FakeGh.__call__(self, args)
        b = Boom(reviews=gh.reviews)
        with self.assertRaises(RuntimeError):
            tr(b).facts(7)

    def test_fix_does_not_start_while_round_open(self):
        f = tr(FakeGh(reviews=[review(BOT, C)], requested=["alice"])).facts(7)
        self.assertFalse(gate.holds({"round": "complete"}, f, {}))


class SweepTest(unittest.TestCase):
    def run_sweep(self, gh, dry=False):
        return gate.sweep(tr(gh), nodes.load(), dry)

    def test_merged_pr_walks_approved_to_done_in_one_run(self):
        gh = FakeGh(labels=["factory:approved", "risk:low"], reviews=[review(BOT, M)])
        gh.merged, gh.state = True, "closed"  # a closed issue in approved is still found
        res, failed = self.run_sweep(gh)
        self.assertEqual(([(r["ticket"], r["outcome"]) for r in res], failed), ([(7, "merged")], False))
        self.assertEqual(sorted(gh.labels), ["factory:done", "risk:low"])
        comments = [w for w in gh.writes if w[:2] == ["issue", "comment"]]
        self.assertEqual(len(comments), 1)
        self.assertIn("Retro: PR #5 merged; fix rounds 0; reviews 1 (1 approve, 0 changes)", comments[0][-1])
        self.assertTrue(comments[0][-1].endswith("Factory-Run: finish:retro"))
        n = len(gh.writes)
        self.assertEqual(self.run_sweep(gh), ([], False))  # no new facts: nothing written
        self.assertEqual(len(gh.writes), n)

    def test_done_closes_an_open_issue(self):
        gh = FakeGh(labels=["factory:approved", "risk:low"])
        gh.merged = True
        self.run_sweep(gh)
        self.assertEqual(gh.state, "closed")
        self.assertIn(["issue", "close", "7", "-R", REPO], gh.writes)

    def test_dry_run_writes_nothing_and_open_pr_waits(self):
        gh = FakeGh(labels=["factory:approved"])
        self.assertEqual(self.run_sweep(gh)[0], [])  # PR still open
        gh.merged = True
        res, _ = self.run_sweep(gh, dry=True)
        self.assertEqual([(r["ticket"], r["outcome"]) for r in res], [(7, "merged")])
        self.assertEqual(gh.writes, [])

    def test_split_waits_for_children(self):
        gh = FakeGh(labels=["factory:split"], sub=["a", "b"])
        gh.sub_state = ["closed", "open"]
        self.assertEqual(self.run_sweep(gh)[0], [])
        gh.sub_state = ["closed", "closed"]
        self.assertEqual([r["outcome"] for r in self.run_sweep(gh)[0]], ["children-done"])
        self.assertEqual(sorted(gh.labels), ["factory:done"])

    def test_child_closed_as_not_planned_blocks_the_parent(self):
        gh = FakeGh(labels=["factory:split"], sub=["a", "b"])
        gh.sub_state, gh.sub_reason = ["closed", "closed"], ["completed", "not_planned"]
        res, _ = self.run_sweep(gh)
        self.assertEqual([r["outcome"] for r in res], ["child-blocked"])
        self.assertEqual(gh.labels, ["factory:blocked"])
        self.assertIn("#2 (closed as not planned)", [w for w in gh.writes if w[:2] == ["issue", "comment"]][0][-1])

    def test_blocked_child_blocks_the_parent(self):
        gh = FakeGh(labels=["factory:split"], sub=["a", "b"])
        gh.sub_labels = [[], ["factory:blocked"]]
        res, _ = self.run_sweep(gh)
        self.assertEqual([r["outcome"] for r in res], ["child-blocked"])
        self.assertEqual(gh.labels, ["factory:blocked"])
        self.assertIn("#2 (blocked)", [w for w in gh.writes if w[:2] == ["issue", "comment"]][0][-1])

    def test_children_closed_as_completed_finish_the_parent(self):
        gh = FakeGh(labels=["factory:split"], sub=["a", "b"])
        gh.sub_state, gh.sub_reason = ["closed", "closed"], ["completed", "completed"]
        self.assertEqual([r["outcome"] for r in self.run_sweep(gh)[0]], ["children-done"])
        self.assertEqual(gh.labels, ["factory:done"])

    def test_blocking_lists_native_dependents(self):
        gh = FakeGh()
        gh.blocking_issues = [{"number": 12, "state": "open"}]
        self.assertEqual(tr(gh).blocking(7), gh.blocking_issues)

    def test_in_review_complete_round_runs_join_and_disabled_outcomes_refuse(self):
        gh = FakeGh(reviews=[review(BOT, M)])
        res, _ = self.run_sweep(gh)
        self.assertEqual([(r["node"], r["outcome"]) for r in res], [("join", "approved")])
        self.assertEqual(sorted(gh.labels), ["factory:approved", "risk:low"])
        with self.assertRaises(gate.Bad):
            gate.apply("sweep", "conflict", {"ticket": 7}, tr(gh), nodes.load())

    def test_pr_state_condition(self):
        self.assertTrue(gate.holds({"pr-state": "merged"}, {"pr_state": "merged"}, {}))
        with self.assertRaises(gate.Bad):
            gate.holds({"pr-state": "x"}, {"pr_state": "merged"}, {})


class ResumeTest(unittest.TestCase):
    def test_stage_is_the_latest_stage_label_added(self):
        gh = FakeGh(labels=["factory:in-review", "factory:ready", "risk:low"])
        gh.label_events = [("factory:in-review", "2026-01-01T00:00:00Z"), ("factory:ready", "2026-01-02T00:00:00Z")]
        self.assertEqual(tr(gh).facts(7)["stage"], "ready")
        gh.label_events.reverse()
        gh.label_events = [("factory:ready", "2026-01-01T00:00:00Z"), ("factory:in-review", "2026-01-02T00:00:00Z")]
        self.assertEqual(tr(gh).facts(7)["stage"], "in-review")

    def test_tied_label_timestamps_follow_event_order(self):
        tie = "2026-01-01T00:00:00Z"
        gh = FakeGh(labels=["factory:in-review", "factory:ready", "risk:low"])
        gh.label_events = [("factory:in-review", tie), ("factory:ready", tie)]
        self.assertEqual(tr(gh).facts(7)["stage"], "ready")
        gh.label_events = [("factory:ready", tie), ("factory:in-review", tie)]
        self.assertEqual(tr(gh).facts(7)["stage"], "in-review")

    def test_ready_added_in_review_resumes_through_intake_and_leaves_one_stage_label(self):
        gh = FakeGh(labels=["factory:in-review", "factory:ready", "risk:low"])
        gh.blocked = []
        gh.label_events = [("factory:in-review", "2026-01-01T00:00:00Z"), ("factory:ready", "2026-01-02T00:00:00Z")]
        t, n = tr(gh), nodes.load()
        code, out = gate.check("intake", {"id": "e1", "ticket": 7}, t, n, CONFIG)
        self.assertEqual(code, 0, out)
        code, out = gate.apply("intake", "proceed", {"ticket": 7, "complexity": "small", "tier": "cheap", "risk": "low"}, t, n)
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(x for x in gh.labels if x in STAGES.values()), ["factory:in-dev"])
        self.assertIn("factory:implement-cheap", gh.labels)


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


class StaleFactsTest(unittest.TestCase):
    def test_since_command_and_recover_comments(self):
        gh = FakeGh(labels=("factory:in-review", "factory:ci-green", "risk:low"),
                    comments=("[factory] recover: stage=in-review re-fired factory:ci-green", "[factory] other"))
        base = gh.__call__

        def run(args):
            a = next((x for x in args if x.startswith("repos/")), "")
            ev = lambda *items: "\n".join(json.dumps({"event": "labeled", "label": {"name": n}, "created_at": t}) for n, t in items)  # noqa: E731
            if a.endswith("issues/7/events"):
                return ev(("factory:in-review", "2026-01-02T03:00:00Z"))
            if a.endswith("issues/5/events"):
                return ev(("factory:ci-green", "2026-01-02T05:00:00Z"))
            if a.endswith("issues/7/comments"):
                return "\n".join(json.dumps({"body": c, "created_at": "2026-01-02T04:00:00Z"}) for c in gh.comments)
            return base(args)

        f = tr(run).stale_facts(7)
        self.assertEqual((f["since"], f["ci_label"], f["command"]), ("2026-01-02T05:00:00Z", "green", None))
        self.assertEqual(len(f["recover_comments"]), 1)
        self.assertEqual(gate.recovered(f, "in-review"), 1)

    def test_ci_label_of_an_earlier_head_is_ignored(self):
        t = tr(FakeGh(labels=("factory:in-review", "factory:ci-green", "risk:low")))
        t._ci = lambda sha: "none"  # the new head has no CI result yet, the label is left from the old head
        self.assertIsNone(t.stale_facts(7)["ci_label"])

if __name__ == "__main__":
    unittest.main()

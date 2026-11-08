#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The system contract (factory/tests/system_contract.py) for the in-memory systems and for GitHub over a stubbed gh
that keeps what was written. Run: uv run factory/tests/test_system_contract.py"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_gate_github as G  # noqa: E402  (sets the team the GitHub tracker reads)
from test_gate_github import BOT, M, REPO, STAGES, FakeGh, GitHubGateTracker, review  # noqa: E402
from system_contract import SystemContract  # noqa: E402
import derive  # noqa: E402
import gate_github  # noqa: E402
from gate_port import MemorySystem, TagTracker  # noqa: E402

LABELS = ["factory:in-review", "risk:low", "factory:fix-mid"]


class MemorySystemContractTest(SystemContract, unittest.TestCase):
    def make(self):
        return MemorySystem({7: {"stage": "in-review", "attrs": ["risk:low", "factory:fix-mid"], "pr": 5, "head": "h1",
                                 "pr_labels": ["factory:fix-mid"], "assignees": []}})


class TagTrackerContractTest(SystemContract, unittest.TestCase):
    def make(self):
        return TagTracker({7: {"attrs": ["stage:in-review", "risk:low", "factory:fix-mid"], "pr": 5, "head": "h1",
                               "pr_labels": ["factory:fix-mid"], "assignees": []}})


def flag(args, name):
    """The value of `-f <name>=...` (or `--<name> ...`) in a gh call."""
    for i, x in enumerate(args[:-1]):
        if x in ("-f", "-F") and args[i + 1].startswith(f"{name}="):
            return args[i + 1].split("=", 1)[1]
        if x == f"--{name}":
            return args[i + 1]
    return None


class ContractGh(FakeGh):
    """FakeGh that keeps what the tracker writes, so a second read sees it: reviews and comments posted, the PR
    description, requested reviewers, the merge, created issues, sub-issue and blocked-by links, labels on them."""

    def __init__(self, **kw):
        super().__init__(labels=LABELS, **kw)
        self.pr_comments, self.pr_body = [], ""

    def __call__(self, args, timeout=None):
        a = next((x for x in args if x.startswith("repos/")), "")
        post = args[:3] == ["api", "-X", "POST"]
        at = "2026-01-03T00:00:00Z"
        if post and a.endswith("/pulls/5/reviews"):
            self.writes.append(args)
            self.reviews.append(review(BOT, flag(args, "body"), commit=flag(args, "commit_id"), at=at))
            return ""
        if post and a.endswith("/pulls/5/requested_reviewers"):
            self.writes.append(args)
            self.requested += [x.split("=", 1)[1] for x in args if x.startswith("reviewers[]=")]
            return ""
        if args[:2] == ["issue", "comment"]:
            self.writes.append(args)
            c = {"body": flag(args, "body"), "user": {"login": BOT, "type": "Bot"}, "created_at": at}
            (self.comments if args[2] == "7" else self.pr_comments).append(c)
            return ""
        if a.endswith("issues/5/comments") and "-f" not in args:
            return G.recorded(a, self.pr_comments)
        if args[:2] == ["pr", "edit"] and "--body" in args:
            self.writes.append(args)
            self.pr_body = flag(args, "body")
            return ""
        if args[:2] == ["pr", "merge"]:
            self.writes.append(args)
            self.merged = True
            return ""
        if post and a == f"repos/{REPO}/issues":
            self.writes.append(args)
            n = 900 + len(self.open_issues)
            self.open_issues.append({"id": n, "number": n, "title": flag(args, "title"), "body": flag(args, "body"),
                                     "labels": [{"name": x.split("=", 1)[1]} for x in args if x.startswith("labels[]=")]})
            return json.dumps(self.open_issues[-1])
        if post and a.endswith("/issues/7/sub_issues"):
            self.writes.append(args)
            n = int(flag(args, "sub_issue_id"))
            self.sub.append(next(i["title"] for i in self.open_issues if i["id"] == n))
            return ""
        if post and a.endswith("/issues/7/dependencies/blocked_by"):
            self.writes.append(args)
            self.blocked.append({"state": "open", "id": int(flag(args, "issue_id"))})
            return ""
        if post and a.endswith("/labels") and "/issues/9" in a:
            self.writes.append(args)
            n = int(a.split("/issues/")[1].split("/")[0])
            issue = next(i for i in self.open_issues if i["number"] == n)
            issue["labels"] += [{"name": x.split("=", 1)[1]} for x in args if x.startswith("labels[]=")]
            return ""
        out = super().__call__(args, timeout)
        if a.endswith("/pulls/5") and out:
            return json.dumps({**json.loads(out), "body": self.pr_body})
        return out


class GitHubContractTest(SystemContract, unittest.TestCase):
    SETUP = {
        ("join", "ticket"): lambda s: s.gh.reviews.append(review(BOT, M)),  # the round at the head is complete
        ("request-reviewers", "pr"): lambda s: s.gh.reviews.append(review("alice", "", state="CHANGES_REQUESTED")),
        ("pr-notes-sync", "pr"): lambda s: s.gh.comments.append(
            {"body": f"{gate_github.NOTES_PARKED}\n\n{derive.notes_block({'for': 'fixer', 'hint': 'h'})}",
             "user": {"login": BOT, "type": "Bot"}, "created_at": "2026-01-02T00:00:00Z"}),
    }
    NO_WRITE = {("seen", "ticket"): "GitHub keeps no seen list: a duplicate event fails its node's conditions"}
    EXEMPT = {
        ("react", "ticket"): "the reactions API keeps one per user and answers the existing one: the handler reports it",
        ("react", "pr"): "as react on the ticket",
        ("update-branch", "pr"): "a single repository's update is not read first: GitHub refuses the second PUT, "
                                 "its expected_head_sha having moved",
    }

    def setUp(self):
        for p in (patch.object(G.M_GH.time, "sleep"), patch.object(GitHubGateTracker, "RECENT_MERGE_SECONDS", 0)):
            p.start()
            self.addCleanup(p.stop)

    def make(self):
        gh = ContractGh()
        t = GitHubGateTracker(REPO, STAGES, BOT, fix_cap=3, run=gh)
        t.gh = gh
        return t


if __name__ == "__main__":
    unittest.main()

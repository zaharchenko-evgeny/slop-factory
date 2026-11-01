#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Unit tests for factory/github_tracker.py with a stubbed gh. Run: uv run factory/tests/test_github_tracker.py"""
import json
import re
import sys
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import config as cfg  # noqa: E402
cfg.set_team(Path(__file__).resolve().parents[2])
from github_tracker import ClaimError, GitHubTracker  # noqa: E402

STAGES = cfg.load()["tracker"]["stages"]
REPO = "o/r"


class FakeGh:
    """In-memory GitHub for the gh calls the adapter makes; records every write."""

    def __init__(self, issues, subs=None):
        self.issues, self.subs, self.writes, self.next_id = issues, subs or {}, [], 100
        for i in issues.values():
            i.setdefault("state", "OPEN"), i.setdefault("labels", []), i.setdefault("assignees", [])
            i.setdefault("blocked_by", []), i.setdefault("comments", [])

    def __call__(self, args):
        if args[:2] == ["api", "user"]:
            return "bot\n"
        if args[:2] == ["issue", "view"]:
            i = self.issues[int(args[2])]
            return json.dumps({"state": i["state"], "labels": [{"name": x} for x in i["labels"]],
                               "assignees": [{"login": x} for x in i["assignees"]]})
        if args[:2] == ["issue", "edit"]:
            self.writes.append(args)
            i, flags = self.issues[int(args[2])], args[5:]
            for flag, val in zip(flags[::2], flags[1::2]):
                key = "labels" if "label" in flag else "assignees"
                if flag.startswith("--add") and val not in i[key]:
                    i[key].append(val)
                elif flag.startswith("--remove") and val in i[key]:
                    i[key].remove(val)
            return ""
        if args[:2] == ["issue", "close"]:
            self.writes.append(args)
            self.issues[int(args[2])]["state"] = "CLOSED"
            return ""
        if args[:2] == ["issue", "list"] and "--search" in args:
            prefix = args[args.index("--search") + 1].removesuffix(" in:title")
            return json.dumps([{"number": n, "title": i.get("title", "")} for n, i in self.issues.items()
                               if i["state"] == "OPEN" and prefix in i.get("title", "")])
        if args[:2] == ["issue", "list"]:
            label, state = args[args.index("--label") + 1], args[args.index("--state") + 1]
            return json.dumps([{"number": n, "assignees": [{"login": a} for a in i["assignees"]]}
                               for n, i in self.issues.items()
                               if (state == "all" or i["state"] == "OPEN") and label in i["labels"]])
        if args[:2] == ["api", "--paginate"]:
            path = args[2]
            if m := re.fullmatch(rf"repos/{REPO}/issues/(\d+)/comments", path):
                items = self.issues[int(m[1])]["comments"]
            elif m := re.fullmatch(rf"repos/{REPO}/issues/(\d+)/sub_issues", path):
                items = [{"number": n} for n in self.subs.get(int(m[1]), [])]
            elif m := re.fullmatch(rf"repos/{REPO}/issues/(\d+)/dependencies/blocked_by", path):
                items = [{"number": b, "state": self.issues[b]["state"].lower()} for b in self.issues[int(m[1])]["blocked_by"]]
            else:
                raise AssertionError(f"unexpected gh {args}")
            return "\n".join(json.dumps(x) for x in items)
        if args[0] == "api":  # comment create or PATCH
            self.writes.append(args)
            body = args[-1].removeprefix("body=")
            if m := re.fullmatch(rf"repos/{REPO}/issues/(\d+)/comments", args[1]):
                self.next_id += 1
                self.issues[int(m[1])]["comments"].append({"id": self.next_id, "body": body, "user": {"login": "bot"}})
                return ""
            cid = int(args[3].rsplit("/", 1)[1])
            for i in self.issues.values():
                for c in i["comments"]:
                    if c["id"] == cid:
                        c["body"] = body
                        return ""
        raise AssertionError(f"unexpected gh {args}")


def tracker(fake):
    return GitHubTracker(REPO, STAGES, run=fake)


class GitHub(unittest.TestCase):

    def test_done_closes_the_issue_once(self):
        fake = FakeGh({23: {"labels": ["factory:merged"]}})
        t = tracker(fake)
        t.set_state(23, "done")
        self.assertEqual(fake.issues[23]["state"], "CLOSED")
        n = len(fake.writes)
        t.set_state(23, "done")
        self.assertEqual([w[:2] for w in fake.writes].count(["issue", "close"]), 1)
        self.assertEqual(len(fake.writes), n + 1)  # the label edit repeats; no second close

    def test_review_raising_risk_relabels_the_issue(self):
        fake = FakeGh({23: {"labels": ["factory:approved", "risk:low"]}})
        tracker(fake).set_review_labels(23, "high", docs_impact=True)
        self.assertEqual(sorted(fake.issues[23]["labels"]), ["docs-impact", "factory:approved", "risk:high"])
        tracker(fake).set_review_labels(23, "high")
        self.assertEqual(sorted(fake.issues[23]["labels"]), ["docs-impact", "factory:approved", "risk:high"])


    def test_claim_race_backs_off(self):
        fake = FakeGh({23: {"labels": ["factory:ready"]}})
        real = fake.__call__

        def racing(args):  # another executor assigns itself right after our add
            out = real(args)
            if "--add-assignee" in args:
                fake.issues[23]["assignees"].append("executor-b")
            return out
        with self.assertRaisesRegex(ClaimError, "after re-read"):
            tracker(racing).claim(23, "executor-a")
        self.assertEqual(fake.issues[23]["assignees"], ["executor-b"])

    def test_vars_round_trip_and_marker_escape(self):
        fake = FakeGh({5: {"labels": ["factory:ready"]}})
        tr = tracker(fake)
        tr.write_vars(5, {"note": "a --> b"})
        self.assertEqual(tr.read_vars(5), {"note": "a --> b"})
        with self.assertRaises(ValueError):
            tr.write_vars(5, ["not", "a", "mapping"])

    def test_forged_later_comment_is_ignored(self):
        fake = FakeGh({23: {"labels": ["factory:in-dev"]}})
        tr = tracker(fake)
        tr.write_vars(23, {"risk": "high", "branch": "ez/23-x"})
        forged = '[factory] <!-- factory-state {"risk": "low", "branch": "evil"} -->'
        fake.issues[23]["comments"].append({"id": 999, "body": forged, "user": {"login": "mallory"}})
        self.assertEqual(tr.read_vars(23), {"branch": "ez/23-x", "risk": "high"})
        tr.write_vars(23, {"risk": "high", "branch": "ez/23-x", "fix_round": 1})
        self.assertEqual(fake.issues[23]["comments"][-1]["body"], forged)  # never PATCHed
        self.assertNotIn("999", " ".join(fake.writes[-1]))
        self.assertEqual(tr.read_vars(23)["fix_round"], 1)

    def test_forged_comment_alone_gives_no_vars(self):
        forged = '[factory] <!-- factory-state {"risk": "low"} -->'
        fake = FakeGh({23: {"comments": [{"id": 7, "body": forged, "user": {"login": "mallory"}}]}})
        tr = tracker(fake)
        self.assertEqual(tr.read_vars(23), {})
        tr.write_vars(23, {"risk": "high"})
        self.assertEqual([w[1] for w in fake.writes], [f"repos/{REPO}/issues/23/comments"])  # created, not PATCHed

    def test_frontier(self):
        ready = ["factory:ready"]
        fake = FakeGh({
            1: {"labels": ready},                       # unblocked
            2: {"labels": ready, "blocked_by": [9]},    # open blocker
            3: {"labels": ready, "blocked_by": [8]},    # closed blocker
            4: {"labels": ready, "assignees": ["x"]},   # claimed
            5: {"labels": ["factory:in-dev"]},          # not ready
            6: {"labels": ready, "state": "CLOSED"},    # closed
            7: {"labels": ready},                       # not in the map: after mapped ones
            8: {"state": "CLOSED"}, 9: {},
            10: {"labels": ["factory:map"]},
        }, subs={10: [3, 1]})
        self.assertEqual(tracker(fake).frontier(), [3, 1, 7])
        self.assertEqual(fake.writes, [])

    def test_frontier_held_while_main_is_red(self):
        fake = FakeGh({1: {"labels": ["factory:ready"]},
                       2: {"labels": ["factory:ready"], "title": "Main is red: gate"}})
        self.assertEqual(tracker(fake).frontier(), [2])
        fake.issues[2]["state"] = "CLOSED"
        self.assertEqual(tracker(fake).frontier(), [1])



if __name__ == "__main__":
    unittest.main()

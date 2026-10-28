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
import sm  # noqa: E402
from drive import drive  # noqa: E402
from github_tracker import GitHubTracker  # noqa: E402
from local_tracker import ClaimError  # noqa: E402

M = sm.load_machine(sm.ROOT / "workflows/delivery.sm.yaml", sm.ROOT / "factory.yaml")
STAGES = yaml.safe_load((sm.ROOT / "factory.yaml").read_text())["tracker"]["stages"]
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
        if args[:2] == ["issue", "list"]:
            label = args[args.index("--label") + 1]
            return json.dumps([{"number": n, "assignees": [{"login": a} for a in i["assignees"]]}
                               for n, i in self.issues.items() if i["state"] == "OPEN" and label in i["labels"]])
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
    def test_drive_ready_to_approved(self):
        fake = FakeGh({23: {"labels": ["factory:ready", "risk:high"], "comments": [
            {"id": 1, "body": "<!-- factory-state {\"risk\": \"low\"} --> not a [factory] comment",
             "user": {"login": "bot"}}]}})
        events = yaml.safe_load((HERE / "fixtures/happy.yaml").read_text())["events"]
        t, trace = drive(M, tracker(fake), 23, "bot", events)
        self.assertEqual((t["state"], trace[-1]["wait"]), ("approved", "human"))
        i = fake.issues[23]
        self.assertEqual(sorted(i["labels"]), ["factory:approved", "risk:high"])
        self.assertEqual(i["assignees"], [])
        state = [c for c in i["comments"] if c["body"].startswith("[factory]")]
        self.assertEqual(len(state), 1)  # created once, then edited in place
        self.assertTrue(state[0]["body"].endswith("Factory-Run: local:20261028T1200-19"))
        self.assertEqual(tracker(fake).read_vars(23), t["vars"])
        self.assertEqual((t["vars"]["risk"], t["vars"]["reviewed_sha"]), ("high", "bbb"))

    def test_second_executor_stops_without_writing(self):
        fake = FakeGh({23: {"labels": ["factory:ready"], "assignees": ["executor-b"]}})
        with self.assertRaisesRegex(ClaimError, "claimed by executor-b"):
            drive(M, tracker(fake), 23, "executor-a", ["stage.ready"])
        self.assertEqual(fake.writes, [])

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


if __name__ == "__main__":
    unittest.main()

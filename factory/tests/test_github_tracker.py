#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Unit tests for factory/factory_plugins/github/tracker.py with a stubbed gh. Run: uv run factory/tests/test_github_tracker.py"""
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import config as cfg  # noqa: E402
cfg.set_team(Path(__file__).resolve().parent / "fixtures/team")
from factory_plugins.github import tracker as github_tracker  # noqa: E402
from factory_plugins.github.tracker import ClaimError, CountedRun, GitHubTracker, RateLimited  # noqa: E402

STAGES = cfg.load()["tracker"]["stages"]
REPO = "o/r"


class FakeGh:
    """In-memory GitHub for the gh calls the adapter makes; records every write."""

    def __init__(self, issues, subs=None):
        self.issues, self.subs, self.writes, self.next_id = issues, subs or {}, [], 100
        for n, i in issues.items():
            i.setdefault("id", n + 1000)
            i.setdefault("state", "OPEN"), i.setdefault("labels", []), i.setdefault("assignees", [])
            i.setdefault("blocked_by", []), i.setdefault("comments", [])

    def __call__(self, args):
        if args[:2] == ["api", "user"]:
            return "bot\n"
        if args[:2] == ["issue", "view"]:
            i = self.issues[int(args[2])]
            return json.dumps({"title": i.get("title", ""), "body": i.get("body", ""),
                               "state": i["state"], "labels": [{"name": x} for x in i["labels"]],
                               "assignees": [{"login": x} for x in i["assignees"]]})
        if args[0] == "api" and len(args) == 4 and re.fullmatch(rf"repos/{REPO}/issues/\d+", args[1]):
            n = int(args[1].rsplit("/", 1)[1])
            return str(self.issues[n]["id"]) + "\n"
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
            if "--method" in args and args[args.index("--method") + 1] == "POST":
                m = re.fullmatch(rf"repos/{REPO}/issues/(\d+)/sub_issues", args[args.index("--method") + 2])
                self.subs.setdefault(int(m[1]), []).append(int(args[args.index("-F") + 1].split("=")[1]))
                return ""
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



class Labels(unittest.TestCase):
    def test_list_create_update_delete(self):
        calls = []

        def run(args):
            calls.append(args)
            return json.dumps([{"name": "factory:x", "color": "ABCDEF", "description": None}]) if args[1] == "list" else ""
        t = GitHubTracker(REPO, STAGES, run=run)
        self.assertEqual(t.list_labels(), {"factory:x": {"color": "abcdef", "description": ""}})
        t.create_label("factory:y", "ededed", "d", repo="o/other")
        t.update_label("factory:x", "000000")
        t.delete_label("risk:low", ["factory:", "risk:"])
        self.assertEqual([c[:5] for c in calls[1:]], [["label", "create", "factory:y", "-R", "o/other"],
                                                      ["label", "edit", "factory:x", "-R", REPO],
                                                      ["label", "delete", "risk:low", "-R", REPO]])

    def test_never_deletes_a_label_outside_the_owned_groups(self):
        calls = []
        with self.assertRaises(ValueError):
            GitHubTracker(REPO, STAGES, run=calls.append).delete_label("bug", ["factory:", "risk:"])
        self.assertEqual(calls, [])


class RateLimit(unittest.TestCase):
    def stub(self, d, script):
        path = Path(d, "gh")
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return {**os.environ, "FACTORY_GH": str(path)}

    LIMITED = ('echo "gh: API rate limit exceeded (HTTP 403)" >&2; '
               'echo "x-ratelimit-remaining: 0" >&2; echo "x-ratelimit-reset: 1000" >&2; exit 1\n')

    def test_403_with_no_calls_left_waits_for_the_reset_and_retries_once(self):
        with tempfile.TemporaryDirectory() as d:
            env = self.stub(d, f'if [ -e "{d}/hit" ]; then echo ok; exit 0; fi; touch "{d}/hit"; ' + self.LIMITED)
            with mock.patch.dict(os.environ, env), mock.patch.object(github_tracker.time, "time", return_value=940), \
                    mock.patch.object(github_tracker.time, "sleep") as sleep:
                self.assertEqual(github_tracker.gh(["api", "x"]), "ok\n")
            sleep.assert_called_once_with(60)

    def test_wait_is_capped_and_a_second_limit_fails_as_rate_limited(self):
        with tempfile.TemporaryDirectory() as d:
            env = self.stub(d, f'echo x >> "{d}/n"; ' + self.LIMITED)
            with mock.patch.dict(os.environ, env), mock.patch.object(github_tracker.time, "time", return_value=0), \
                    mock.patch.object(github_tracker.time, "sleep") as sleep:
                with self.assertRaises(RateLimited) as cm:
                    github_tracker.gh(["api", "x"])
            sleep.assert_called_once_with(300)
            self.assertEqual(cm.exception.kind, "rate-limited")
            self.assertEqual(len(Path(d, "n").read_text().split()), 2)

    def test_other_failures_are_not_retried(self):
        with tempfile.TemporaryDirectory() as d:
            env = self.stub(d, 'echo "HTTP 403 forbidden" >&2; exit 1\n')
            with mock.patch.dict(os.environ, env), mock.patch.object(github_tracker.time, "sleep") as sleep:
                with self.assertRaises(RuntimeError) as cm:
                    github_tracker.gh(["api", "x"])
            self.assertNotIsInstance(cm.exception, RateLimited)
            sleep.assert_not_called()


class ProjectWriteToken(unittest.TestCase):
    def test_only_writes_to_project_repositories_use_the_write_token(self):
        calls = []
        run = github_tracker.ProjectWriteRun(lambda args, token=None: calls.append(token) or "", ["o/App"],
                                             {"FACTORY_WRITE_TOKEN": "pat"})
        run(["pr", "edit", "2", "-R", "o/app", "--add-label", "x"])
        run(["api", "-X", "PUT", "repos/o/app/pulls/2/update-branch"])
        run(["api", "repos/o/app/issues/2/labels", "-f", "labels[]=x"])  # a field: gh POSTs
        run(["label", "create", "p", "-R", "o/app"])
        run(["api", "--paginate", "repos/o/app/pulls?state=open", "--jq", ".[]"])  # a read
        run(["pr", "view", "2", "-R", "o/app"])
        run(["pr", "edit", "2", "-R", "o/r", "--add-label", "x"])  # not a project repository
        self.assertEqual(calls, ["pat"] * 4 + [None] * 3)

    def test_link_to_a_foreign_issue_uses_the_issues_token(self):
        """#1155: linking a slop-eval-factory child to a slop-factory parent writes slop-factory but refers to the
        other repository's issue, which Air's token cannot read: `for_repository` gives it that repository's token."""
        calls = []
        run = github_tracker.ProjectWriteRun(lambda args, token=None: calls.append(token) or "", ["o/team"],
                                             {"FACTORY_ISSUES_TOKEN": "pat"}, {"o/team": "FACTORY_ISSUES_TOKEN"})
        link = ["api", "-X", "POST", "repos/o/engine/issues/7/sub_issues", "-F", "sub_issue_id=99"]
        run(link)
        run.for_repository("o/team")(link)
        run.for_repository("o/engine")(link)  # a child of the own repository: Air's token
        github_tracker.CountedRun(run).for_repository("o/team")(link)  # the gate's counter keeps it (#1155)
        self.assertEqual(calls, [None, "pat", None, "pat"])

    def test_checked_out_repository_with_a_token_runs_with_it(self):
        """#1236: a repository's `token` is used also when the run checks it out; the environment's own repository
        ($FACTORY_ENV_REPOSITORY) keeps Air's token."""
        calls = []
        env = {"FACTORY_ISSUES_TOKEN": "T", "FACTORY_ENV": "team", "FACTORY_ENV_REPOSITORY": "o/team"}
        config = {"repositories": [{"name": "engine", "url": "https://github.com/o/engine"},
                                   {"name": "team", "url": "https://github.com/o/team"}],
                  "environments": {"team": {"repositories": ["team", "engine"], "tokens": {"engine": "FACTORY_ISSUES_TOKEN"}}}}
        tokens = github_tracker.repository_tokens(config, env)
        self.assertEqual(tokens, {"o/engine": "FACTORY_ISSUES_TOKEN"})
        run = github_tracker.ProjectWriteRun(lambda args, token=None: calls.append(token) or "", ["o/engine", "o/team"], env, tokens)
        for repo in ("o/engine", "o/team"):
            run(["label", "create", "x", "-R", repo])
        self.assertEqual(calls, ["T", None])

    def test_each_repository_uses_its_own_token(self):
        """Table: a declared secret wins over $FACTORY_WRITE_TOKEN; unset secrets and unlisted repositories fall back."""
        env = {"A_TOKEN": "a", "B_TOKEN": "b", "FACTORY_WRITE_TOKEN": "w"}
        for tokens, environ, repo, want in (
                ({"o/a": "A_TOKEN", "o/b": "B_TOKEN"}, env, "o/a", "a"),
                ({"o/a": "A_TOKEN", "o/b": "B_TOKEN"}, env, "O/B", "b"),
                ({"o/a": "A_TOKEN"}, env, "o/b", "w"),
                ({"o/a": "MISSING"}, env, "o/a", "w"),
                ({"o/a": "MISSING"}, {"B_TOKEN": "b"}, "o/a", None),
                ({}, {}, "o/a", None)):
            with self.subTest(tokens=tokens, repo=repo, want=want):
                calls = []
                run = github_tracker.ProjectWriteRun(lambda args, token=None: calls.append(token) or "", ["o/a", "o/b"], environ, tokens)
                run(["label", "create", "x", "-R", repo])
                self.assertEqual(calls, [want])

    def test_tokens_come_from_the_run_environment_or_every_environment(self):
        config = {"repositories": [{"name": "a", "url": "https://github.com/o/a.git"}],
                  "environments": {"e1": {"tokens": {"a": "S1"}}, "e2": {"tokens": {"a": "S2"}}, "e3": {}}}
        for environ, want in (({"FACTORY_ENV": "e2"}, {"o/a": "S2"}), ({"FACTORY_ENV": "e3"}, {}), ({}, {"o/a": "S1"})):
            with self.subTest(environ=environ):
                self.assertEqual(github_tracker.repository_tokens(config, environ), want)

    def test_without_the_token_every_call_runs_as_before(self):
        calls = []
        run = github_tracker.ProjectWriteRun(lambda args, **kw: calls.append(kw) or "", ["o/app"], {})
        run(["pr", "edit", "2", "-R", "o/app", "--add-label", "x"])
        self.assertEqual(calls, [{}])

    def test_gh_sets_gh_token_for_that_call_only(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d, "gh")
            path.write_text('#!/bin/sh\necho "$GH_TOKEN"\n')
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
            with mock.patch.dict(os.environ, {"FACTORY_GH": str(path), "GH_TOKEN": "air"}):
                self.assertEqual(github_tracker.gh(["api", "x"], token="pat"), "pat\n")
                self.assertEqual(github_tracker.gh(["api", "x"]), "air\n")
                self.assertEqual(os.environ["GH_TOKEN"], "air")


class Counted(unittest.TestCase):
    def test_a_repeated_read_of_an_issue_timeline_calls_gh_once(self):
        calls = []
        run = CountedRun(lambda args: calls.append(args) or "[]")
        for _ in range(3):
            run(["api", "--paginate", "repos/o/r/issues/7/events", "--jq", ".[]"])
        self.assertEqual((len(calls), run.calls), (1, 1))

    def test_a_write_drops_the_remembered_reads(self):
        run = CountedRun(lambda args: "[]")
        run(["api", "repos/o/r/issues/7"])
        run(["issue", "edit", "7", "-R", "o/r", "--add-label", "x"])
        run(["api", "repos/o/r/issues/7"])
        self.assertEqual(run.calls, 3)

    def test_sweep_of_one_issue_reads_only_issues_linked_to_it(self):
        import gate
        from factory_plugins.github import gate as gate_github
        seen = []  # 20 other open ready tickets exist, but the gh calls below answer for #7 only

        def run(args):
            seen.append(args)
            if args[0] == "api" and re.search(r"issues/(\d+)(/|$)", " ".join(args)):
                return "[]" if "--paginate" in args else json.dumps({"labels": [], "number": 7})
            raise AssertionError(args)
        t = gate_github.GitHubGateTracker(REPO, STAGES, "bot", run=run)
        t.parent = lambda n: None
        t.facts = lambda n: {"stage": "", "blockers_open": 0}
        gate.sweep(t, {}, issue=7, closed_ticket=7)
        touched = {int(m[1]) for a in seen if (m := re.search(r"issues/(\d+)", " ".join(a)))}
        self.assertLessEqual(touched, {7})



class MemoTest(unittest.TestCase):
    def test_reads_by_sha_and_the_pr_list_reach_gh_once_and_a_write_drops_them(self):
        from factory_plugins.github import tracker as github_tracker
        calls = []
        run = github_tracker.CountedRun(lambda args, **kw: (calls.append(args), "{}")[1])
        sha = "a" * 40
        reads = [["api", f"repos/o/r/commits/{sha}"], ["api", f"repos/o/r/compare/main...{sha}"],
                 ["api", "--paginate", "repos/o/r/pulls?state=all&per_page=100", "--jq", ".[]"]]
        for _ in range(2):
            for r in reads:
                run(r)
        self.assertEqual(len(calls), 3)
        run(["api", "repos/o/r/pulls/5"])
        run(["api", "repos/o/r/pulls/5"])
        self.assertEqual(len(calls), 5)  # a PR itself is read fresh: a merge checks its head
        run(["issue", "edit", "7", "--add-label", "x"])
        run(reads[0])
        self.assertEqual(len(calls), 7)

if __name__ == "__main__":
    unittest.main()

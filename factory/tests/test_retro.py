#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for the nightly retro's facts. Run: uv run factory/tests/test_retro.py"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
cfg.set_team(ROOT / "factory/tests/fixtures/team")
import cli  # noqa: E402
import retro  # noqa: E402
import scopes  # noqa: E402
from urllib.parse import parse_qs, unquote, urlsplit  # noqa: E402
from factory_plugins.github.gate import GitHubTicketStore  # noqa: E402
from factory_plugins.youtrack.store import YouTrackTicketStore  # noqa: E402

cli.load_engine(cli.HERE)
BOT, ME = {"login": "jetbrains-air[bot]", "type": "Bot"}, {"login": "me", "type": "User"}


def label(name, at, by=BOT, event="labeled"):
    return {"event": event, "label": {"name": f"factory:{name}"}, "created_at": at, "actor": by}


class RoutineBeginTest(unittest.TestCase):
    def test_routine_begin_needs_no_payload(self):
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            self.addCleanup(os.chdir, old)
            cli.STATE = Path(".factory/begin.json")
            Path("empty.json").write_text("")
            for name in ("retro", "distill"):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cli.begin([name, "empty.json"], ROOT / "factory/tests/fixtures/team"), 0)
                self.assertTrue(json.loads(out.getvalue())["go"])
                self.assertIsNone(json.loads(out.getvalue())["ticket"])


class GitHubFake:
    """A GitHub store over `comments` ({issue: [comment]}): the GitHub store's lesson methods on a fake `gh` api."""

    def __init__(self, comments, scope=None):
        self.comments, self.calls = comments, []
        self.store = GitHubTicketStore("o/r", self.gh, "bot", scope=scope)

    def gh(self, args):
        self.calls.append(args)
        path = args[1] if args[0] == "api" else args[2]
        if args[:2] == ["api", "--paginate"]:
            path = args[2]
            if "/issues?" in path:
                self.query = path
                return "\n".join(json.dumps({"number": n}) for n in self.comments)
            n = int(path.split("/issues/")[1].split("/")[0])
            return "\n".join(json.dumps(c) for c in self.comments[n])
        if args[0] == "api":
            c = next(c for cs in self.comments.values() for c in cs if c["id"] == int(path.rsplit("/", 1)[1]))
            if "-X" in args:
                c["body"] = args[-1].removeprefix("body=")
            return json.dumps(c)
        return ""

    def edits(self):
        return [(a[2], a[5:]) for a in self.calls if a[0] == "issue"]


class YouTrackFake:
    """YouTrack's REST API over `issues` ({id: {tags, comments}}); a query lists the issues with the tag."""

    def __init__(self, issues):
        self.issues, self.calls = issues, []
        self.store = YouTrackTicketStore(self, bots=["bot"], scope={"field": "Subsystem", "value": "Backend"},
                                         base_url="https://yt.example/")

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        url, parts = urlsplit(path), urlsplit(path).path.split("/")[2:]
        if parts == ["issues"]:
            query = unquote(parse_qs(url.query)["query"][0])
            self.query = query
            tag = query.split("tag: {")[1].split("}")[0]
            return [{"idReadable": n} for n, i in self.issues.items() if tag in i["tags"]]
        if parts[0] == "tags":
            return [{"id": "t1", "name": "factory:lesson"}]
        issue = self.issues[parts[1]]
        if len(parts) == 2:
            return {"idReadable": parts[1], "tags": [{"id": "t1", "name": t} for t in issue["tags"]]}
        if parts[2] == "tags":
            if method == "POST":
                issue["tags"].append("factory:lesson")
            else:
                issue["tags"].remove("factory:lesson")
            return {}
        if parts[2] == "comments" and len(parts) == 3:
            return issue["comments"]
        c = next(c for c in issue["comments"] if c["id"] == parts[3])
        if method == "POST":
            c["text"] = body["text"]
        return c


def yt_issues():
    return {"LME-1": {"tags": ["factory:lesson"], "comments": [
                {"id": "4-1", "text": "[factory] Lessons\n<!-- factory:lesson -->\n- a"}, {"id": "4-2", "text": "chat"},
                {"id": "4-3", "text": "<!-- factory:lesson collected -->\n- old"}, {"id": "4-4", "text": "<!-- factory:lesson -->\n- b"}]},
            "LME-2": {"tags": ["factory:lesson"], "comments": [{"id": "5-1", "text": "<!-- factory:lesson -->\n- c"}]},
            "LME-3": {"tags": [], "comments": [{"id": "6-1", "text": "<!-- factory:lesson -->\n- untagged"}]}}


def gh_comments():
    return {10: [{"id": 1, "html_url": "u1", "body": "[factory] Lessons\n<!-- factory:lesson -->\n- a"},
                 {"id": 2, "body": "not a lesson"},
                 {"id": 3, "body": "[factory] Lessons\n<!-- factory:lesson collected -->\n- old"},
                 {"id": 4, "html_url": "u4", "body": "<!-- factory:lesson -->\n- b"}],
            11: [{"id": 5, "body": "<!-- factory:lesson -->\n- c"}]}


class Leaf:
    def __init__(self, store):
        self.tickets = store


class CollectTest(unittest.TestCase):
    def tracker(self):
        fake = GitHubFake(gh_comments())
        return Leaf(fake.store), fake.edits, fake.comments

    def test_collect_lists_uncollected_lessons_and_writes_nothing(self):
        t, edits, _ = self.tracker()
        self.assertEqual(retro.collect(t, "factory:lesson"), [
            {"ticket": 10, "url": "u1", "id": 1, "body": "[factory] Lessons\n\n- a"},
            {"ticket": 10, "url": "u4", "id": 4, "body": "- b"},
            {"ticket": 11, "url": None, "id": 5, "body": "- c"}])
        self.assertEqual(edits(), [])

    def test_mark_marks_only_given_comments_and_unlabels_when_none_is_left(self):
        t, edits, comments = self.tracker()
        self.assertEqual(retro.mark(t, "factory:lesson", ["1", 5]), [1, 5])
        # the label comes off before the comments are read; ticket 10 keeps lesson 4, so it is labelled again
        self.assertEqual([(n, f) for n, f in edits()], [
            ("10", ["--remove-label", "factory:lesson"]), ("10", ["--add-label", "factory:lesson"]),
            ("11", ["--remove-label", "factory:lesson"])])
        self.assertIn("<!-- factory:lesson collected -->", comments[10][0]["body"])
        self.assertNotIn("collected", comments[10][3]["body"])
        self.assertEqual([c["id"] for c in retro.collect(t, "factory:lesson")], [4])

    def test_mark_keeps_label_of_a_lesson_posted_while_it_runs(self):
        fake = GitHubFake(gh_comments())
        gh = fake.gh

        def racing(args):
            out = gh(args)
            if args[:1] == ["issue"] and "--remove-label" in args and args[2] == "10":  # posted mid-mark
                fake.comments[10].append({"id": 6, "body": "<!-- factory:lesson -->\n- late"})
            return out
        fake.store.run = racing
        retro.mark(Leaf(fake.store), "factory:lesson", [1, 4])
        self.assertIn(("10", ["--add-label", "factory:lesson"]), [(n, f) for n, f in fake.edits()])

    def test_mark_relabels_the_ticket_when_a_write_fails(self):
        fake = GitHubFake(gh_comments())
        fake.store.mark_lesson = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            retro.mark(Leaf(fake.store), "factory:lesson", [1])
        self.assertEqual([f for _, f in fake.edits()],
                         [["--remove-label", "factory:lesson"], ["--add-label", "factory:lesson"]])

    def test_collect_reads_only_the_scope_label_of_a_scoped_github_tracker(self):
        fake = GitHubFake(gh_comments(), scope={"label": "area:web"})
        retro.collect(Leaf(fake.store), "factory:lesson")
        self.assertIn("labels=factory:lesson,area:web", fake.query)

    def test_youtrack_collect_returns_the_tagged_in_scope_lessons_and_skips_collected(self):
        yt = YouTrackFake(yt_issues())
        self.assertEqual(retro.collect(Leaf(yt.store), "factory:lesson"), [
            {"ticket": "LME-1", "url": "https://yt.example/issue/LME-1#focus=Comments-4-1.0-0", "id": "4-1",
             "body": "[factory] Lessons\n\n- a"},
            {"ticket": "LME-1", "url": "https://yt.example/issue/LME-1#focus=Comments-4-4.0-0", "id": "4-4", "body": "- b"},
            {"ticket": "LME-2", "url": "https://yt.example/issue/LME-2#focus=Comments-5-1.0-0", "id": "5-1", "body": "- c"}])
        self.assertEqual(yt.query, "(tag: {factory:lesson}) and Subsystem: {Backend}")
        self.assertEqual([c for c in yt.calls if c[0] != "GET"], [])

    def test_youtrack_mark_edits_the_comment_and_removes_the_tag_only_when_none_remains(self):
        yt = YouTrackFake(yt_issues())
        self.assertEqual(retro.mark(Leaf(yt.store), "factory:lesson", ["4-1", "5-1", "6-1", "9-9"]), ["4-1", "5-1"])
        issues = yt.issues
        self.assertIn("<!-- factory:lesson collected -->", issues["LME-1"]["comments"][0]["text"])
        self.assertEqual(issues["LME-1"]["tags"], ["factory:lesson"])  # lesson 4-4 is still uncollected
        self.assertEqual(issues["LME-2"]["tags"], [])
        self.assertNotIn("collected", issues["LME-3"]["comments"][0]["text"])
        yt.calls.clear()
        self.assertEqual(retro.mark(Leaf(yt.store), "factory:lesson", ["4-4"]), ["4-4"])
        self.assertEqual(issues["LME-1"]["tags"], [])
        self.assertEqual(retro.collect(Leaf(yt.store), "factory:lesson"), [])

    def test_collect_in_an_environment_with_a_github_and_a_youtrack_tracker_returns_both(self):
        gh, yt = GitHubFake(gh_comments()), YouTrackFake(yt_issues())
        scoped = scopes.ScopedTracker({}, lambda config, env: (Leaf({"github": gh.store, "yt": yt.store}[env]), None),
                                      envs={"github": {}, "yt": {}})
        self.assertEqual([c["ticket"] for c in retro.collect(scoped, "factory:lesson")], [10, 10, 11, "LME-1", "LME-1", "LME-2"])
        self.assertEqual(sorted(retro.mark(scoped, "factory:lesson", ["1", "5", "4-1", "5-1"]), key=str), [1, "4-1", 5, "5-1"])

    def test_mark_ignores_unknown_and_already_collected_ids(self):
        t, edits, comments = self.tracker()
        self.assertEqual(retro.mark(t, "factory:lesson", [2, 3, 99]), [])
        self.assertEqual(comments, gh_comments())

    def test_main_mark_needs_comment_ids(self):
        for argv in (["mark"], ["mark", "x"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(retro.main(argv), 2)

    def test_main_mark_accepts_github_and_youtrack_comment_ids(self):
        for ids in (["12"], ["4-12"], ["12", "4-12"]):
            with mock.patch.object(retro, "mark", return_value=[]) as m, mock.patch("gate.tracker_of"), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(retro.main(["mark", *ids]), 0)
            self.assertEqual(m.call_args.args[2], ids)


if __name__ == "__main__":
    unittest.main()

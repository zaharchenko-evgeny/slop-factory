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


class CollectTest(unittest.TestCase):
    def tracker(self):
        calls = []
        comments = {10: [{"id": 1, "url": "u1", "body": "[factory] Lessons\n<!-- factory:lesson -->\n- a"},
                         {"id": 2, "body": "not a lesson"},
                         {"id": 3, "body": "[factory] Lessons\n<!-- factory:lesson collected -->\n- old"},
                         {"id": 4, "url": "u4", "body": "<!-- factory:lesson -->\n- b"}],
                    11: [{"id": 5, "body": "<!-- factory:lesson -->\n- c"}]}

        class Tracker:
            def tickets_with(self, attr):
                return [10, 11]

            def ticket_comments(self, n):
                return comments[n]

            def edit_ticket_attrs(self, n, add=(), remove=()):
                calls.append(("unlabel", n, list(remove)) if remove else ("label", n, list(add)))

            def edit_comment(self, n, comment_id, body):
                calls.append(("patch", comment_id))
                next(c for c in comments[n] if c["id"] == comment_id)["body"] = body

        return Tracker(), calls, comments

    def test_collect_lists_uncollected_lessons_and_writes_nothing(self):
        t, calls, _ = self.tracker()
        self.assertEqual(retro.collect(t, "factory:lesson"), [
            {"ticket": 10, "url": "u1", "id": 1, "body": "[factory] Lessons\n\n- a"},
            {"ticket": 10, "url": "u4", "id": 4, "body": "- b"},
            {"ticket": 11, "url": None, "id": 5, "body": "- c"}])
        self.assertEqual(calls, [])

    def test_mark_marks_only_given_comments_and_unlabels_when_none_is_left(self):
        t, calls, comments = self.tracker()
        self.assertEqual(retro.mark(t, "factory:lesson", ["1", 5]), [1, 5])
        # the label comes off before the comments are read; ticket 10 keeps lesson 4, so it is labelled again
        self.assertEqual(calls, [("unlabel", 10, ["factory:lesson"]), ("patch", 1), ("label", 10, ["factory:lesson"]),
                                 ("unlabel", 11, ["factory:lesson"]), ("patch", 5)])
        self.assertIn("<!-- factory:lesson collected -->", comments[10][0]["body"])
        self.assertNotIn("collected", comments[10][3]["body"])
        self.assertEqual([c["id"] for c in retro.collect(t, "factory:lesson")], [4])
        calls.clear()
        retro.mark(t, "factory:lesson", [4])
        self.assertEqual(calls, [("unlabel", 10, ["factory:lesson"]), ("patch", 4), ("unlabel", 11, ["factory:lesson"])])

    def test_mark_keeps_label_of_a_lesson_posted_while_it_runs(self):
        t, calls, comments = self.tracker()
        api = t.edit_comment

        def racing(n, comment_id, body):
            api(n, comment_id, body)
            if not any(c["id"] == 6 for c in comments[10]):  # posted and labelled mid-mark
                comments[10].append({"id": 6, "body": "<!-- factory:lesson -->\n- late"})
        t.edit_comment = racing
        retro.mark(t, "factory:lesson", [1, 4])
        self.assertIn(("label", 10, ["factory:lesson"]), calls)
        self.assertEqual([c["id"] for c in retro.collect(t, "factory:lesson")], [6, 5])

    def test_mark_relabels_the_ticket_when_a_write_fails(self):
        t, calls, _ = self.tracker()

        def broken(n, comment_id, body):
            raise RuntimeError("boom")
        t.edit_comment = broken
        with self.assertRaises(RuntimeError):
            retro.mark(t, "factory:lesson", [1])
        self.assertEqual(calls, [("unlabel", 10, ["factory:lesson"]), ("label", 10, ["factory:lesson"])])

    def test_mark_ignores_unknown_and_already_collected_ids(self):
        t, calls, _ = self.tracker()
        self.assertEqual(retro.mark(t, "factory:lesson", [2, 3, 99]), [])
        self.assertNotIn(("patch", 2), calls)
        self.assertNotIn(("patch", 3), calls)

    def test_main_mark_needs_comment_ids(self):
        for argv in (["mark"], ["mark", "x y"], ["mark", ""], ["mark", "1", "$(x)"], ["collect", "x"], []):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(retro.main(argv), 2, argv)


class ScopedCollectTest(unittest.TestCase):
    """Collect and mark through the scopes wrapper, over a GitHub and a YouTrack environment (#1426)."""
    L = "<!-- factory:lesson -->"

    def scoped(self, env="gh"):
        import scopes
        log = []

        class Fake:
            def __init__(self, tickets, ids):
                self.tickets, self.ids = tickets, ids

            def in_scope(self, n):
                return n in self.tickets

            def tickets_with(self, attr):
                return [n for n, t in self.tickets.items() if attr in t["attrs"]]

            def ticket_comments(self, n):
                return self.tickets[n]["comments"]

            def edit_ticket_attrs(self, n, add=(), remove=()):
                t = self.tickets[n]["attrs"]
                t[:] = [a for a in t if a not in remove] + [a for a in add if a not in t]
                log.append((n, list(add), list(remove)))

            def edit_comment(self, n, comment_id, body):
                next(c for c in self.tickets[n]["comments"] if c["id"] == comment_id)["body"] = body

        gh = Fake({5: {"attrs": ["factory:lesson"], "comments": [
            {"id": 101, "url": "gh-url", "body": f"{self.L}\n- gh lesson"}]}}, None)
        yt = Fake({"LME-7": {"attrs": ["factory:lesson"], "comments": [
            {"id": "4-12", "url": "yt-url", "body": f"{self.L}\n- yt lesson"}, {"id": "4-13", "body": "chat"}]},
            "LME-8": {"attrs": [], "comments": [{"id": "4-14", "body": f"{self.L}\n- unlabelled"}]}}, None)
        config = {"environments": {"gh": {"tracker": {"adapter": "github-issues", "repository": "o/r"}},
                                   "yt": {"tracker": {"adapter": "youtrack", "url": "https://yt", "project": "LME"}}}}
        tracker = scopes.ScopedTracker(config, lambda c, e: ({"gh": gh, "yt": yt}[e], None), env)
        return tracker, gh, yt, log

    def test_collect_reads_every_environment_whichever_one_the_run_is_in(self):
        for env in ("gh", "yt", None):
            tracker, *_ = self.scoped(env)
            self.assertEqual(retro.collect(tracker, "factory:lesson"), [
                {"ticket": 5, "url": "gh-url", "id": 101, "body": "- gh lesson"},
                {"ticket": "LME-7", "url": "yt-url", "id": "4-12", "body": "- yt lesson"}], env)

    def test_mark_writes_each_ticket_in_its_own_environment(self):
        tracker, gh, yt, log = self.scoped()
        self.assertEqual(retro.mark(tracker, "factory:lesson", ["101", "4-12"]), [101, "4-12"])
        self.assertIn("collected", gh.tickets[5]["comments"][0]["body"])
        self.assertIn("collected", yt.tickets["LME-7"]["comments"][0]["body"])
        self.assertEqual(gh.tickets[5]["attrs"] + yt.tickets["LME-7"]["attrs"], [])
        self.assertEqual(retro.collect(tracker, "factory:lesson"), [])

    def test_main_reports_a_crash_as_failed(self):
        err = io.StringIO()
        with mock.patch("gate.systems", side_effect=KeyError("YOUTRACK_TOKEN")), contextlib.redirect_stderr(err):
            self.assertEqual(retro.main(["collect"]), 1)
        self.assertIn("failed: factory retro collect: KeyError", err.getvalue())

    def test_main_collects_through_the_gate_systems_tracker(self):
        tracker, *_ = self.scoped()
        out = io.StringIO()
        with mock.patch("gate.systems", return_value=(tracker, tracker)), contextlib.redirect_stdout(out):
            self.assertEqual(retro.main(["collect"]), 0)
        self.assertEqual([x["ticket"] for x in json.loads(out.getvalue())], [5, "LME-7"])


class StoreLessonsTest(unittest.TestCase):
    """The ticket stores' lesson reads and writes, on stubbed runners (#1426)."""

    def test_github_store_lists_labelled_issues_by_scope_and_edits_a_comment(self):
        from factory_plugins.github.gate import GitHubTicketStore
        calls = []
        issues = [{"number": 5, "labels": [{"name": "factory:lesson"}, {"name": "area:web"}]},
                  {"number": 6, "labels": [{"name": "factory:lesson"}]},
                  {"number": 7, "labels": [{"name": "factory:lesson"}, {"name": "area:web"}], "pull_request": {}}]

        def gh(args, **kw):
            calls.append(list(args))
            return "\n".join(json.dumps(i) for i in issues) if "--paginate" in args else "{}"
        self.assertEqual(GitHubTicketStore("o/r", gh).tickets_with("factory:lesson"), [5, 6])
        self.assertEqual(GitHubTicketStore("o/r", gh, scope={"label": "area:web"}).tickets_with("factory:lesson"), [5])
        self.assertIn("repos/o/r/issues?state=all&labels=factory:lesson&per_page=100", calls[0])
        GitHubTicketStore("o/r", gh).edit_comment(5, 9, "new")
        self.assertEqual(calls[-1], ["api", "repos/o/r/issues/comments/9", "-X", "PATCH", "-f", "body=new"])

    def test_youtrack_store_lists_tagged_tickets_and_reads_and_edits_comments(self):
        from factory_plugins.youtrack.store import YouTrackTicketStore
        calls = []

        def run(method, path, body=None):
            calls.append((method, path, body))
            if "/comments" in path and method == "GET":
                return [{"id": "4-12", "text": "t", "created": 1, "author": {"login": "bot"}}]
            return [{"idReadable": "LME-9"}, {"idReadable": "LME-7"}] if path.startswith("/api/issues?") else {}
        store = YouTrackTicketStore(run, base_url="https://yt/", scope={"field": "Subsystem", "value": "LME"})
        self.assertEqual(store.tickets_with("factory:lesson"), ["LME-7", "LME-9"])
        self.assertIn("tag%3A%20%7Bfactory%3Alesson%7D", calls[0][1])
        self.assertIn("Subsystem", calls[0][1])
        c = store.comments("LME-7")[0]
        self.assertEqual((c["id"], c["url"]), ("4-12", "https://yt/issue/LME-7#focus=Comments-4-12"))
        self.assertIsNone(YouTrackTicketStore(run).comments("LME-7")[0]["url"])
        store.edit_comment("LME-7", "4-12", "new")
        self.assertEqual(calls[-1], ("POST", "/api/issues/LME-7/comments/4-12?fields=id", {"text": "new"}))


if __name__ == "__main__":
    unittest.main()

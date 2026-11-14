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
        comments = {10: [{"id": 1, "html_url": "u1", "body": "[factory] Lessons\n<!-- factory:lesson -->\n- a"},
                         {"id": 2, "body": "not a lesson"},
                         {"id": 3, "body": "[factory] Lessons\n<!-- factory:lesson collected -->\n- old"},
                         {"id": 4, "html_url": "u4", "body": "<!-- factory:lesson -->\n- b"}],
                    11: [{"id": 5, "body": "<!-- factory:lesson -->\n- c"}]}

        class Tracker:
            def _list(self, path):
                if path.startswith("issues?"):
                    return [{"number": 10}, {"number": 11}]
                return comments[int(path.split("/")[1])]

            def _edit(self, n, add=(), remove=()):
                calls.append(("unlabel", n, list(remove)) if remove else ("label", n, list(add)))

            def _api(self, path, *flags):
                calls.append(("patch", int(path.rsplit("/", 1)[1])))
                c = next(c for cs in comments.values() for c in cs if c["id"] == int(path.rsplit("/", 1)[1]))
                c["body"] = flags[-1].removeprefix("body=")

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
        api = t._api

        def racing(path, *flags):
            api(path, *flags)
            if not any(c["id"] == 6 for c in comments[10]):  # posted and labelled mid-mark
                comments[10].append({"id": 6, "body": "<!-- factory:lesson -->\n- late"})
        t._api = racing
        retro.mark(t, "factory:lesson", [1, 4])
        self.assertIn(("label", 10, ["factory:lesson"]), calls)
        self.assertEqual([c["id"] for c in retro.collect(t, "factory:lesson")], [6, 5])

    def test_mark_relabels_the_ticket_when_a_write_fails(self):
        t, calls, _ = self.tracker()

        def broken(path, *flags):
            raise RuntimeError("boom")
        t._api = broken
        with self.assertRaises(RuntimeError):
            retro.mark(t, "factory:lesson", [1])
        self.assertEqual(calls, [("unlabel", 10, ["factory:lesson"]), ("label", 10, ["factory:lesson"])])

    def test_mark_ignores_unknown_and_already_collected_ids(self):
        t, calls, _ = self.tracker()
        self.assertEqual(retro.mark(t, "factory:lesson", [2, 3, 99]), [])
        self.assertNotIn(("patch", 2), calls)
        self.assertNotIn(("patch", 3), calls)

    def test_main_mark_needs_comment_ids(self):
        for argv in (["mark"], ["mark", "x"]):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(retro.main(argv), 2)


class ScopedCollectTest(unittest.TestCase):
    """#1426: a multi-environment (scoped) tracker refused `_list`, so collect and facts crashed every night."""

    def scoped(self):
        import scopes
        gh = CollectTest().tracker()[0]
        gh.repo = "o/factory"

        class Store:
            url = "https://yt.example"

            def tickets_by_stage(self, attrs, open_only=False, now=None):
                return ["LME-1"] if attrs == ["factory:lesson"] else []

            def lesson_comments(self, n):
                return [{"id": "4-9", "url": f"{self.url}/issue/{n}#focus=Comments-4-9", "body": "<!-- factory:lesson -->\n- yt"},
                        {"id": "4-10", "url": None, "body": "no lesson"}]

        class YtGate:
            tickets, github, repo = Store(), object(), "o/jbeval"

        systems = {"team": (gh, None), "jbeval": (YtGate(), None), "intake": (YtGate(), None)}
        envs = {"team": {"adapter": "github-issues"}, "jbeval": {"adapter": "youtrack"}, "intake": {"adapter": "youtrack"}}
        return scopes.ScopedTracker({}, lambda config, env: systems[env], "team", envs, {}), gh

    def test_scoped_tracker_refuses_private_reads(self):
        t, _ = self.scoped()
        with self.assertRaises(AttributeError):
            t._list("issues")

    def test_collect_reads_github_and_youtrack_lessons_through_a_scoped_tracker(self):
        t, _ = self.scoped()
        got = retro.collect(t, "factory:lesson")
        self.assertEqual([c["id"] for c in got], [1, 4, 5, "4-9"])  # the two YouTrack scopes hold one lesson once
        self.assertEqual(got[-1], {"ticket": "LME-1", "url": "https://yt.example/issue/LME-1#focus=Comments-4-9",
                                   "id": "4-9", "body": "- yt", "tracker": "youtrack"})

    def test_mark_works_through_a_scoped_tracker(self):
        t, _ = self.scoped()
        self.assertEqual(retro.mark(t, "factory:lesson", [1, 4, 5]), [1, 4, 5])
        self.assertEqual([c["id"] for c in retro.collect(t, "factory:lesson")], ["4-9"])

    def test_facts_read_github_issue_environments_through_a_scoped_tracker(self):
        from factory_plugins.facts import report
        t, gh = self.scoped()
        gh.stages = {"done": "factory:done"}
        gh._list = lambda path: []
        self.assertEqual(report.facts(t, "2026-11-01T00:00:00Z")["tickets"], [])


class YouTrackLessonCommentsTest(unittest.TestCase):
    def test_lesson_comments_carry_id_and_link(self):
        from factory_plugins.youtrack.store import YouTrackTicketStore
        calls = []

        def run(method, path, body=None):
            calls.append((method, path))
            return [{"id": "4-9", "text": "<!-- factory:lesson -->\n- a"}]
        store = YouTrackTicketStore(run, url="https://yt.example/")
        self.assertEqual(store.lesson_comments("LME-1"), [
            {"id": "4-9", "url": "https://yt.example/issue/LME-1#focus=Comments-4-9", "body": "<!-- factory:lesson -->\n- a"}])
        self.assertTrue(calls[0][1].startswith("/api/issues/LME-1/comments?fields=id,text"))


if __name__ == "__main__":
    unittest.main()

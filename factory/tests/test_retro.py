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


class FactsTest(unittest.TestCase):
    def test_fix_rounds_counted_across_resumes(self):
        pr = [label("fix-mid", "2026-11-01T01:00:00Z"), label("fix-mid", "2026-11-01T02:00:00Z"),
              label("fix-mid", "2026-11-01T03:00:00Z"), label("update-cheap", "2026-11-01T04:00:00Z")]
        issue = [label("ready", "2026-11-01T00:00:00Z"), label("ready", "2026-11-01T02:30:00Z"),
                 label("done", "2026-11-01T06:00:00Z")]
        f = retro.ticket_facts(7, issue, pr, [], [], {BOT["login"]})
        self.assertEqual((f["fix_rounds"], f["update_rounds"], f["ready_to_done_hours"]), (3, 1, 6.0))

    def test_operator_interventions_follow_the_stage_prefix(self):
        """Table: stage prefix -> the labels a person set that count (`ready` is quiet; other prefixes never match)."""
        events = [{**label(name, "T1", ME), "label": {"name": full}}
                  for name, full in (("approved", "slop:approved"), ("ready", "slop:ready"), ("approved", "factory:approved"))]
        for prefix, want in (("slop:", ["slop:approved"]), ("factory:", ["factory:approved"]), ("work:", [])):
            with self.subTest(prefix=prefix), mock.patch.object(retro.vocab, "stage_prefix", return_value=prefix):
                f = retro.ticket_facts(7, events, [], [], [], {BOT["login"]})
                self.assertEqual([x["label"] for x in f["operator_interventions"]], want)

    def test_operator_interventions_listed(self):
        issue = [label("ready", "T1", ME), label("triage", "T0", ME), label("blocked", "T2"),
                 label("approved", "T3", ME), label("needs-human", "T4", ME, "unlabeled")]
        reviews = [{"user": ME, "submitted_at": "T5", "body": "no"},
                   {"user": BOT, "body": "<!-- factory:review focus=behaviour round=1 verdict=changes -->\n**B1** a:1 — x"},
                   {"user": BOT, "body": "<!-- factory:review focus=behaviour round=2 verdict=changes -->"}]
        comments = [{"user": ME, "created_at": "T6"}, {"user": BOT, "created_at": "T6", "body": "[factory] stuck\nmore"}]
        f = retro.ticket_facts(7, issue, [], reviews, comments, {BOT["login"]})
        self.assertEqual([(x["kind"], x.get("label")) for x in f["operator_interventions"]],
                         [("label", "factory:approved"), ("label", "factory:needs-human"), ("review", None), ("comment", None)])
        self.assertEqual(f["reviews"]["behaviour"],
                         {"reviews": 2, "approve": 0, "changes": 2, "blocking": 1, "changes_without_finding": 1,
                          "confirmed": 0, "withdrawn": 0, "escaped": 0})
        self.assertEqual(f["stops"], [{"stage": "factory:blocked", "at": "T2", "reason": "[factory] stuck"}])


def review(focus, verdict, head, findings=0):
    body = f"<!-- factory:review focus={focus} round=1 verdict={verdict} -->\n" + "".join(f"**B{i}** a:1 — x\n" for i in range(findings))
    return {"user": BOT, "body": body, "commit_id": head}


class ReviewScoringTest(unittest.TestCase):
    def facts(self, reviews, **kw):
        return retro.ticket_facts(7, [], [], reviews, [], {BOT["login"]}, pr_number=9, **kw)["reviews"]

    def test_confirmed_when_a_new_head_follows(self):
        got = self.facts([review("behaviour", "changes", "a", 2), review("behaviour", "approve", "b")])
        self.assertEqual((got["behaviour"]["confirmed"], got["behaviour"]["withdrawn"]), (2, 0))

    def test_withdrawn_when_next_review_approves_the_same_head(self):
        got = self.facts([review("behaviour", "changes", "a", 3), review("behaviour", "approve", "a")])
        self.assertEqual((got["behaviour"]["confirmed"], got["behaviour"]["withdrawn"]), (0, 3))

    def test_escaped_counts_later_bug_tickets_of_approved_merged_pr(self):
        later = [{"number": 20, "body": "PR #9 broke the retro\nother", "labels": []},
                 {"number": 21, "body": "see https://github.com/o/r/issues/7", "labels": ["bug"]},
                 {"number": 22, "body": "#7 is related", "labels": []},
                 {"number": 23, "body": "#8 broke it", "labels": []}]
        reviews = [review("behaviour", "approve", "a"), review("tests", "changes", "a", 1)]
        got = self.facts(reviews, merged=True, later=later)
        self.assertEqual((got["behaviour"]["escaped"], got["tests"]["escaped"]), (2, 0))
        self.assertEqual(self.facts(reviews, later=later)["behaviour"]["escaped"], 0)  # not merged

    def test_facts_output_has_per_focus_totals(self):
        one = {"reviews": {"behaviour": {"reviews": 2, "confirmed": 1}}}
        two = {"reviews": {"behaviour": {"reviews": 1, "confirmed": 0}, "tests": {"reviews": 1, "confirmed": 1}}}
        self.assertEqual(retro.review_totals([one, two]),
                         {"behaviour": {"reviews": 3, "confirmed": 1}, "tests": {"reviews": 1, "confirmed": 1}})


class FactsOutputTest(unittest.TestCase):
    def test_facts_returns_tickets_and_totals(self):
        class Tracker:
            stages, bot, trusted = {"done": "factory:done"}, "b", ()

            def _list(self, path):
                if path.startswith("issues?state=all&labels"):
                    return [{"number": 7, "body": ""}]
                if path.startswith("issues?state=all&since"):
                    return []
                if path == "issues/7/events":
                    return [label("done", "2026-11-02T00:00:00Z")]
                if path == "pulls/9/reviews":
                    return [review("behaviour", "approve", "a")]
                return []

            def _pr(self, n):
                return {"number": 9, "merged": True}

        got = retro.facts(Tracker(), "2026-11-01T00:00:00Z")
        self.assertEqual(got["reviews"]["behaviour"]["approve"], 1)
        self.assertEqual([t["ticket"] for t in got["tickets"]], [7])


class ChangedLinesTest(unittest.TestCase):
    def test_whole_file_deletion_counts_once_and_generated_paths_are_skipped(self):
        files = [{"filename": "a.py", "status": "modified", "additions": 5, "deletions": 3},
                 {"filename": "old.py", "status": "removed", "additions": 0, "deletions": 400},
                 {"filename": "gen.json", "status": "modified", "additions": 90, "deletions": 90},
                 {"filename": "engine/template-v1/skills/x/SKILL.md", "status": "added", "additions": 50, "deletions": 0}]
        self.assertEqual(retro.changed_lines(files, ["gen.json"]), 9)



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


if __name__ == "__main__":
    unittest.main()

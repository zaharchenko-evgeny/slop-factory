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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
cfg.set_team(ROOT / "factory/tests/fixtures/team")
import cli  # noqa: E402
import knowledge  # noqa: E402
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


class InboxTest(unittest.TestCase):
    def test_one_inbox_pr_per_night(self):
        pr = lambda n, ref, base="knowledge-inbox": {"number": n, "head": {"ref": ref}, "base": {"ref": base}}  # noqa: E731
        self.assertEqual(knowledge.inbox_pr_action([pr(1, "ez/1-x"), pr(2, "knowledge-inbox", "main")], "knowledge-inbox"),
                         {"branch": "knowledge-inbox", "source": "knowledge-inbox-pr", "action": "create", "pr": None})
        prs = [pr(9, "knowledge-inbox-pr"), pr(4, "knowledge-inbox-pr"), pr(2, "knowledge-inbox-pr", "main")]
        self.assertEqual(knowledge.inbox_pr_action(prs, "knowledge-inbox"),
                         {"branch": "knowledge-inbox", "source": "knowledge-inbox-pr", "action": "update", "pr": 4})


class ChangedLinesTest(unittest.TestCase):
    def test_whole_file_deletion_counts_once_and_generated_paths_are_skipped(self):
        files = [{"filename": "a.py", "status": "modified", "additions": 5, "deletions": 3},
                 {"filename": "old.py", "status": "removed", "additions": 0, "deletions": 400},
                 {"filename": "gen.json", "status": "modified", "additions": 90, "deletions": 90},
                 {"filename": "engine/template-v1/skills/x/SKILL.md", "status": "added", "additions": 50, "deletions": 0}]
        self.assertEqual(retro.changed_lines(files, ["gen.json"]), 9)



class CollectTest(unittest.TestCase):
    def test_collect_removes_label_then_marks(self):
        """The label goes before the comments are read; each lesson comment is taken once."""
        calls, comments = [], {10: [{"id": 1, "html_url": "u1", "body": "[factory] Lessons\n<!-- factory:lesson -->\n- a"},
                                    {"id": 2, "body": "not a lesson"},
                                    {"id": 3, "body": "[factory] Lessons\n<!-- factory:lesson collected -->\n- old"}]}

        class Tracker:
            def _list(self, path):
                calls.append(("list", path))
                if path.startswith("issues?"):
                    return [{"number": 10}]
                return comments[int(path.split("/")[1])]

            def _edit(self, n, add=(), remove=()):
                calls.append(("unlabel", n, list(remove)))

            def _api(self, path, *flags):
                calls.append(("patch", path, flags[-1]))
                comments[10][0]["body"] = flags[-1].removeprefix("body=")

        got = retro.collect(Tracker(), "factory:lesson")
        self.assertEqual(got, [{"ticket": 10, "url": "u1", "body": "[factory] Lessons\n\n- a"}])
        order = [c[0] for c in calls]
        self.assertLess(order.index("unlabel"), order.index("patch"))
        self.assertIn("<!-- factory:lesson collected -->", comments[10][0]["body"])
        self.assertEqual(retro.collect(Tracker(), "factory:lesson"), [])   # nothing is read twice


if __name__ == "__main__":
    unittest.main()

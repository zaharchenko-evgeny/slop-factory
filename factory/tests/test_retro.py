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
cfg.set_team(ROOT)
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
                    self.assertEqual(cli.begin([name, "empty.json"], ROOT), 0)
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
                         {"approve": 0, "changes": 2, "blocking": 1, "changes_without_finding": 1})
        self.assertEqual(f["stops"], [{"stage": "factory:blocked", "at": "T2", "reason": "[factory] stuck"}])


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
                 {"filename": "skills/vendor/x/SKILL.md", "status": "added", "additions": 50, "deletions": 0}]
        self.assertEqual(retro.changed_lines(files, ["gen.json"]), 9)


if __name__ == "__main__":
    unittest.main()

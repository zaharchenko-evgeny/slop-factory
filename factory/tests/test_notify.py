#!/usr/bin/env python3
"""Run: uv run --quiet factory/tests/test_notify.py"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config
config.set_team(ROOT)
from notify import notify


class NotifyTest(unittest.TestCase):
    def run_notify(self, stage, comments=()):
        calls = []
        comments = list(comments)
        label = {"blocked": "factory:blocked", "needs-answers": "factory:needs-answers"}[stage]
        issue = {"labels": [{"name": label}]}
        timeline = [{"source": {"issue": {"html_url": "https://github.com/o/r/pull/9", "pull_request": {"url": "api"}}}}]

        def gh(args):
            calls.append(args)
            if args[0] == "api" and args[1].endswith("/comments"):
                return json.dumps(list(comments))
            if args[0] == "api" and args[1].endswith("/timeline"):
                return json.dumps(timeline)
            if args[0] == "api":
                return json.dumps(issue)
            if args[0] == "issue" and args[1] == "comment":
                comments.append({"body": args[-1]})
            return ""

        return calls, lambda: notify(304, gh)

    def test_blocked_assigns_and_comments_once(self):
        calls, invoke = self.run_notify("blocked", [{"body": "[factory] Blocked by missing credentials."}])
        self.assertTrue(invoke())
        self.assertFalse(invoke())
        writes = [call for call in calls if call[0] == "issue"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[0][-2:], ["--add-assignee", "zaharchenko-evgeny"])
        body = writes[1][-1]
        self.assertIn("`blocked`", body)
        self.assertIn("Blocked by missing credentials.", body)
        self.assertIn("https://github.com/o/r/pull/9", body)

    def test_needs_answers_assigns_with_questions(self):
        calls, invoke = self.run_notify("needs-answers", [{"body": "[factory] Questions: which API?"}])
        self.assertTrue(invoke())
        self.assertIn("Questions: which API?", calls[-1][-1])
        self.assertIn("needs-answers", calls[-1][-1])

    def test_second_label_same_stage_does_nothing(self):
        calls, invoke = self.run_notify("blocked", [{"body": "[factory] reason"},
                                                       {"body": "already done <!-- factory-notify:blocked -->"}])
        self.assertFalse(invoke())
        self.assertFalse(any(call[0] == "issue" for call in calls))


if __name__ == "__main__":
    unittest.main()

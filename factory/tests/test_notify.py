#!/usr/bin/env python3
"""Run: uv run --quiet factory/tests/test_notify.py"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config
config.set_team(ROOT / "factory/tests/fixtures/team")
from notify import notify


class NotifyTest(unittest.TestCase):
    def run_notify(self, stage, comments=(), needs_human=False):
        calls = []
        comments = list(comments)
        label = {"blocked": "factory:blocked", "needs-answers": "factory:needs-answers"}[stage]
        issue = {"labels": [{"name": label}, *([{"name": "factory:needs-human"}] if needs_human else [])]}
        timeline = [{"source": {"issue": {"html_url": "https://github.com/o/r/pull/9", "pull_request": {"url": "api"}}}}]

        def gh(args):
            calls.append(args)
            if args[:3] == ["api", "--paginate", "--slurp"]:
                items = comments if args[3].endswith("/comments") else timeline
                return json.dumps([items[i:i + 30] for i in range(0, len(items), 30)])
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

    def test_gate_notification_prevents_duplicate_assignment_and_comment(self):
        calls, invoke = self.run_notify("needs-answers", [{"body": "[factory] Which API?"}], needs_human=True)
        self.assertFalse(invoke())
        self.assertFalse(any(call[0] == "issue" for call in calls))

    def test_markers_and_reasons_beyond_first_page_are_seen(self):
        filler = [{"body": "chatter"}] * 30
        calls, invoke = self.run_notify("blocked", filler + [{"body": "[factory] Late reason."}])
        self.assertTrue(invoke())
        self.assertIn("Late reason.", calls[-1][-1])
        self.assertFalse(invoke())
        calls, invoke = self.run_notify("blocked", filler + [{"body": "<!-- factory-notify:blocked -->"}])
        self.assertFalse(invoke())


if __name__ == "__main__":
    unittest.main()

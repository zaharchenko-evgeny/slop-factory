#!/usr/bin/env python3
"""Run: uv run --quiet factory/tests/test_comment_wake_label.py"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/comment-wake-label.sh"


class CommentWakeLabelTest(unittest.TestCase):
    def run_event(self, body, event_key="issue"):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            event = {event_key: {"number": 17}, "comment": {"body": body}}
            (root / "event.json").write_text(json.dumps(event))
            called = root / "called"
            gh = root / "gh"
            gh.write_text("#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" > \"$GH_CALL\"\n")
            gh.chmod(0o755)
            env = {**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "GH_CALL": str(called),
                   "GH_REPO": "owner/repo", "GITHUB_EVENT_PATH": str(root / "event.json")}
            result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return called.read_text().strip() if called.exists() else None

    def test_person_comment_adds_factory_comment_label(self):
        self.assertEqual(self.run_event("Please take a look."),
                         "issue edit 17 -R owner/repo --add-label factory:comment")

    def test_factory_from_comment_is_ignored(self):
        self.assertIsNone(self.run_event("<!-- factory:from developer -->"))

    def test_factory_ask_comment_adds_label(self):
        self.assertEqual(self.run_event("<!-- factory:from developer --><!-- factory:ask id=1 -->"),
                         "issue edit 17 -R owner/repo --add-label factory:comment")

    def test_review_comment_uses_pull_request_number(self):
        self.assertEqual(self.run_event("Please review", "pull_request"),
                         "issue edit 17 -R owner/repo --add-label factory:comment")


if __name__ == "__main__":
    unittest.main()

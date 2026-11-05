#!/usr/bin/env python3
"""Run: uv run --quiet factory/tests/test_ci_label.py"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci-state-label.sh"

STUB = """#!/usr/bin/env bash
dir=$(dirname "$0")
echo "$*" >> "$dir/calls"
if [ "$1 $2" = "pr list" ] && [ "$(wc -l < "$dir/calls")" -le "$FAILS" ]; then
  echo 'GraphQL: Something went wrong while executing your query' >&2
  exit 1
fi
case "$1 $2" in
  "pr list") echo 7 ;;
  "pr view") echo "" ;;
esac
"""

UV_STUB = """#!/usr/bin/env bash
echo "$*" >> "$(dirname "$0")/uv-calls"
"""


class CiLabelTest(unittest.TestCase):
    def run_script(self, fails):
        with tempfile.TemporaryDirectory() as tmp:
            gh = Path(tmp, "gh")
            gh.write_text(STUB)
            gh.chmod(0o755)
            uv = Path(tmp, "uv")
            uv.write_text(UV_STUB)
            uv.chmod(0o755)
            env = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", "FAILS": str(fails),
                   "CI_LABEL_WAITS": "0 0", "GH_REPO": "o/r", "BRANCH": "ez/1-x",
                   "SHA": "abc", "CONCLUSION": "success"}
            result = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True)
            return (result, Path(tmp, "calls").read_text().splitlines(),
                    Path(tmp, "uv-calls").read_text().splitlines() if Path(tmp, "uv-calls").exists() else [])

    def test_label_retried_after_transient_error(self):
        result, calls, uv_calls = self.run_script(fails=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pr edit 7 --add-label factory:ci-green", calls)
        self.assertIn("run --quiet factory/gate.py wake-reviewers --issue 1 --completed-head abc", uv_calls)

    def test_fails_after_third_attempt(self):
        result, calls, _ = self.run_script(fails=99)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 3)
        self.assertIn("::error title=ci-state-label::", result.stderr)


CLOSE_SCRIPT = ROOT / "scripts" / "close-main-red.sh"
ISSUES = [(1, "Main is red: agnix"), (2, "Main is red in Air: tests"), (3, "Main is red"), (4, "Fix Main is red: x")]


class CloseMainRedTest(unittest.TestCase):
    def test_closes_only_main_is_red_tickets(self):
        gh_stub = """#!/usr/bin/env bash
echo "$*" >> "$(dirname "$0")/calls"
if [ "$1 $2" = "issue list" ]; then cat "$(dirname "$0")/issues.json"; fi
"""
        import json
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "gh").write_text(gh_stub)
            Path(tmp, "gh").chmod(0o755)
            Path(tmp, "issues.json").write_text(json.dumps([{"number": n, "title": t} for n, t in ISSUES]))
            env = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", "SHA": "abc", "RUN_URL": "https://x/runs/9"}
            result = subprocess.run([str(CLOSE_SCRIPT)], env=env, capture_output=True, text=True)
            calls = Path(tmp, "calls").read_text().splitlines()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c for c in calls if c.startswith("issue close")], ["issue close 1", "issue close 2"])
        self.assertTrue(all("abc" in c for c in calls if c.startswith("issue comment")))


if __name__ == "__main__":
    unittest.main()

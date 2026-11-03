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


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Unit tests for factory/run_role.py with a stub agent and gh run by /bin/bash. Run: uv run factory/tests/test_run_role.py"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import run_role  # noqa: E402
import sm  # noqa: E402

# Prints nothing until the call count reaches $STUB_OK_AT; records argv and cwd per call.
AGENT = """#!/bin/bash
n=$(($(cat "$STUB_DIR/calls" 2>/dev/null || echo 0) + 1))
echo "$n" > "$STUB_DIR/calls"
pwd > "$STUB_DIR/cwd"
printf '%s\\n' "$@" > "$STUB_DIR/argv"
echo "stub stderr $n" >&2
if [ "$n" -ge "$STUB_OK_AT" ]; then printf '```text\\nREVIEWER\\nverdict: approved\\n```\\n'; fi
"""
GH = """#!/bin/bash
echo "{\\"url\\":\\"$3\\",\\"body\\":\\"Closes #9\\",\\"statusCheckRollup\\":[{\\"conclusion\\":\\"SUCCESS\\"}]}"
"""


class RunRole(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        shutil.copy(sm.ROOT / "factory.yaml", self.root)
        shutil.copytree(sm.ROOT / "workflows", self.root / "workflows")
        self.run_dir = self.root / ".factory/runs/9"
        self.wt = self.root / ".factory/worktrees/9"
        self.run_dir.mkdir(parents=True)
        self.wt.mkdir(parents=True)
        stub = self.root / "stub"
        stub.mkdir()
        for name, text in (("agent", AGENT), ("gh", GH)):
            (stub / name).write_text(text)
            (stub / name).chmod(0o755)
        self.stub = stub
        os.environ.update(FACTORY_AGENT=str(stub / "agent"), FACTORY_GH=str(stub / "gh"), STUB_DIR=str(stub))

    def go(self, action, ok_at, risk="low", pr_urls=()):
        os.environ["STUB_OK_AT"] = str(ok_at)
        (self.run_dir / "sm.json").write_text(json.dumps({"vars": {"risk": risk, "pr_urls": list(pr_urls)}}))
        (self.run_dir / f"{action}.in.md").write_text("mode: test\n")
        return run_role.run(self.root, 9, action)

    def calls(self):
        return int((self.stub / "calls").read_text())

    def test_empty_output_is_logged_and_retried_once(self):
        self.assertEqual(self.go("review", ok_at=2), 0)
        self.assertEqual(self.calls(), 2)
        log = (self.run_dir / "review.log").read_text()
        self.assertIn("factory: empty output from review (attempt 1/2); retrying", log)
        self.assertIn("stub stderr 2", log)
        self.assertIn("verdict: approved", (self.run_dir / "review.out.md").read_text())

    def test_empty_output_twice_is_an_error(self):
        self.assertEqual(self.go("review", ok_at=99), 3)
        self.assertEqual(self.calls(), 2)
        self.assertIn("(attempt 2/2); giving up", (self.run_dir / "review.log").read_text())

    def test_worktree_cwd_absolute_prompts_and_model(self):
        self.go("implement", ok_at=1, risk="high")
        self.assertEqual(Path((self.stub / "cwd").read_text().strip()).resolve(), self.wt)
        argv = (self.stub / "argv").read_text().splitlines()
        self.assertIn(str(self.root / "roles/_common.md"), argv)
        self.assertIn(str(self.root / "roles/developer/role.md"), argv)
        self.assertEqual(argv[argv.index("--model") + 1], sm.yaml.safe_load(
            (self.root / "factory.yaml").read_text())["models"]["strong"])
        self.assertIn("factory-9-developer", argv)

    def test_intake_runs_in_root_and_summary_gets_pr_data(self):
        self.go("intake", ok_at=1)
        self.assertEqual(Path((self.stub / "cwd").read_text().strip()).resolve(), self.root)
        self.go("summary", ok_at=1, pr_urls=["https://x/pull/1"])
        self.assertEqual(Path((self.stub / "cwd").read_text().strip()).resolve(), self.wt)
        argv = (self.stub / "argv").read_text()
        self.assertIn('"url":"https://x/pull/1"', argv)
        self.assertIn('"conclusion":"SUCCESS"', argv)


if __name__ == "__main__":
    unittest.main()

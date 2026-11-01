#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for `factory begin` and `factory end` (factory/cli.py). Run: uv run factory/tests/test_cli.py"""
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

PAYLOAD = {"action": "labeled", "issue": {"number": 7, "title": "Add x", "updated_at": "T1"}}


class BeginEndTest(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        cli.STATE = Path(".factory/begin.json")
        self.addCleanup(lambda: (os.chdir(self.old), self.tmp.cleanup()))

    def tracker(self, **record):
        Path("tickets.json").write_text(json.dumps({"7": {"stage": "in-dev", **record}}))
        Path("event.json").write_text(json.dumps(PAYLOAD))
        return ["--tracker", "memory:tickets.json"]

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            return cli.main(list(argv)), out.getvalue()

    def test_begin_exits_10_for_duplicate_event(self):
        tracker = self.tracker(seen=["issues:T1"])
        code, out = self.run_cli("begin", "implement-mid", "event.json", *tracker)
        self.assertEqual((code, json.loads(out)["reason"]), (10, "duplicate event"))

    def test_end_applies_handoff_for_begun_node(self):
        tracker = self.tracker()
        code, out = self.run_cli("begin", "implement-mid", "event.json", *tracker)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["handoff_path"], ".factory/handoff/implement.json")
        Path(".factory/handoff").mkdir()
        Path(".factory/handoff/implement.json").write_text(json.dumps({"handoff": "DEVELOPER", "status": "done", "branch": "ez/7-add-x"}))
        code, out = self.run_cli("end", ".factory/handoff/implement.json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(Path("tickets.json").read_text())["7"]["stage"], "in-review")

    def test_end_without_begin_is_an_error(self):
        self.assertEqual(self.run_cli("end", ".factory/handoff/implement.json")[0], 2)


if __name__ == "__main__":
    unittest.main()

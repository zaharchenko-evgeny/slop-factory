#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for the running marker and `factory recover` through MemoryTracker. Run: uv run factory/tests/test_recover.py"""
import contextlib
import inspect
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
cfg.set_team(ROOT)
import gate  # noqa: E402
import nodes  # noqa: E402
from gate_port import MemoryTracker  # noqa: E402

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
OLD, FRESH = "2026-11-02T11:00:00+00:00", "2026-11-02T11:50:00+00:00"
PAYLOAD = {"action": "labeled", "label": {"name": "factory:implement-mid"},
           "issue": {"number": 7, "title": "Add x", "updated_at": "T1"}}


def mark(since):
    return {"node": "implement", "command": "factory:implement-mid", "on": "issue", "run": "r1", "since": since}


class RecoverTest(unittest.TestCase):
    def gate_main(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()):
            return gate.main(list(argv))

    def test_begin_marks_end_clears(self):
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                Path("t.json").write_text(json.dumps({"7": {"stage": "in-dev", "attrs": ["factory:implement-mid"]}}))
                Path("e.json").write_text(json.dumps(PAYLOAD))
                Path("h.json").write_text(json.dumps({"handoff": "DEVELOPER", "status": "done", "blocked_reason": "none"}))
                tracker = ["--tracker", "memory:t.json"]
                self.assertEqual(self.gate_main("check", "--node", "implement", "--event", "e.json", *tracker), gate.GO)
                running = json.loads(Path("t.json").read_text())["7"]["running"]
                self.assertEqual((running["node"], running["command"], running["on"]), ("implement", "factory:implement-mid", "issue"))
                self.gate_main("apply", "--node", "implement", "--event", "e.json", "--handoff", "h.json", *tracker)
                self.assertNotIn("running", json.loads(Path("t.json").read_text())["7"])
            finally:
                os.chdir(old)

    def test_dead_run_refired_after_timeout(self):
        tr = MemoryTracker({7: {"stage": "in-dev", "running": mark(OLD)}, 8: {"stage": "in-dev", "running": mark(FRESH)}})
        res, failed = gate.recover_running(tr, nodes.load(), 30, NOW)
        self.assertEqual([(r["ticket"], r["recover"], r["applied"]) for r in res], [(7, "refire", True)])
        self.assertFalse(failed)
        self.assertNotIn("running", tr.tickets[7])
        self.assertIn("running", tr.tickets[8])  # a fresh run is left alone
        self.assertIn(["label", '"factory:implement-mid"'], tr.tickets[7]["log"])
        self.assertIn("[factory] recover: stage=in-dev re-fired factory:implement-mid", tr.tickets[7]["log"][-1][1])

    def test_second_stall_blocks_and_needs_human(self):
        tr = MemoryTracker({7: {"stage": "in-dev", "running": mark(OLD)}})
        gate.recover_running(tr, nodes.load(), 30, NOW)
        tr.tickets[7]["running"] = mark(OLD)  # the re-fired run died as well
        res, _ = gate.recover_running(tr, nodes.load(), 30, NOW)
        self.assertEqual([r["recover"] for r in res], ["blocked"])
        self.assertEqual(tr.tickets[7]["stage"], "blocked")
        self.assertIn("factory:needs-human", tr.tickets[7]["attrs"])

    def test_recover_uses_tracker_port_only(self):
        src = inspect.getsource(gate.recover_running)
        for word in ("subprocess", "gh(", "gh ", "GitHub"):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()

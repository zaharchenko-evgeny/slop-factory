#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/runlog.py: the event line and `drive.py report`. Run: uv run factory/tests/test_runlog.py"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import runlog  # noqa: E402


def lines(path, *rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in rows))


USAGE = {"event": "usage", "step": "implement", "role": "developer", "model": "m1", "cost": 0.5}


class RunLog(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_emit_stamps_time_and_run(self):
        f = self.tmp / "events.log"
        runlog.emit(f, "r1", "retry", action="ci", count=1)
        e = json.loads(f.read_text())
        self.assertEqual((e["run"], e["event"], e["count"]), ("r1", "retry", 1))
        self.assertTrue(e["ts"].endswith("+00:00"))

    def test_report_table_and_totals(self):
        lines(self.tmp / "7/events.log",
              {**USAGE, "ts": "2026-10-29T10:00:00+00:00"},
              {"event": "step.end", "ts": "2026-10-29T10:01:00+00:00", "duration_s": 90},
              {"event": "retry", "ts": "2026-10-29T10:01:00+00:00"},
              {"event": "fallback", "ts": "2026-10-29T10:01:00+00:00"},
              {"event": "wait", "ts": "2026-10-29T10:03:00+00:00", "what": "ci", "duration_s": 60},
              {"event": "transition", "ts": "2026-10-29T10:10:00+00:00", "to": "blocked", "note": "stuck"})
        (self.tmp / "7/sm.json").write_text(json.dumps({"state": "blocked", "vars": {"fix_round": 2}, "out": {}}))
        lines(self.tmp / "8/events.log", {**USAGE, "cost": 0.25})  # old log: usage only
        rows = runlog.collect(self.tmp)
        a, b = rows
        self.assertEqual((a["wall_s"], a["active_s"], a["wait_s"], a["cost_total"], a["fix_rounds"], a["retries"], a["fallbacks"]),
                         (600, 90, 60, 0.5, 2, 1, 1))
        self.assertEqual(a["reasons"], ["stuck"])
        self.assertEqual((b["wall_s"], b["active_s"], b["wait_s"], b["cost_total"]), (None, None, None, 0.25))
        out = runlog.table(rows).splitlines()
        self.assertIn("10m00s", out[1])
        self.assertIn("developer/m1: $0.500", out[2])
        self.assertTrue(any(x.startswith("8") and " ?" in x and "0.250" in x for x in out))
        self.assertEqual(out[-1].split()[-4], "0.750")
        self.assertEqual([r["ticket"] for r in runlog.collect(self.tmp, ticket=8)], ["8"])
        self.assertEqual([r["ticket"] for r in runlog.collect(self.tmp, since="2026-10-29")][:1], ["7"])
        self.assertEqual(runlog.collect(self.tmp, since="2999"), [])


if __name__ == "__main__":
    unittest.main()

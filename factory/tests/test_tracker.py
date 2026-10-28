#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Unit tests for factory/local_tracker.py and factory/drive.py. Run: uv run factory/tests/test_tracker.py"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import sm  # noqa: E402
from drive import drive  # noqa: E402
from local_tracker import ClaimError, LocalTracker  # noqa: E402

FIX = HERE / "fixtures"
M = sm.load_machine(sm.ROOT / "workflows/delivery.sm.yaml", sm.ROOT / "factory.yaml")


class Tracker(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.effort = Path(shutil.copytree(FIX / "effort", Path(tmp) / "effort"))
        self.tr = LocalTracker(self.effort)

    def test_drive_ready_to_approved(self):
        events = yaml.safe_load((FIX / "happy.yaml").read_text())["events"]
        t, trace = drive(M, self.tr, "01", "executor-a", events)
        self.assertEqual((t["state"], trace[-1]["wait"]), ("approved", "human"))
        fresh = LocalTracker(self.effort)
        self.assertEqual(fresh.get_state("01"), "approved")
        self.assertEqual(fresh.read_vars("01"), t["vars"])
        self.assertEqual((t["vars"]["risk"], t["vars"]["reviewed_sha"]), ("high", "bbb"))
        text = self.tr.path("01").read_text()
        self.assertNotIn("claimed_by: executor-a", text)
        self.assertIn("claimed_by: not-a-claim", text)  # body text stays untouched
        self.assertTrue(text.endswith("## Comments\n"))
        self.assertEqual(self.tr.frontier(), ["06"])  # 01 left `ready`

    def test_stage_sequence_in_dev_in_review_approved(self):
        stages = []
        set_state = self.tr.set_state
        self.tr.set_state = lambda nn, s: (stages.append(s) if stages[-1:] != [s] else None, set_state(nn, s))
        events = [{"stage.ready": {"run_id": "r"}}, "preflight.done", {"intake.done": {"ready": "yes", "risk": "low"}},
                  {"start.done": {"branch": "b"}}, "implement.done", "checks.passed",
                  {"review_full.done": {"verdict": "approved", "reviewed_sha": "s", "risk": "high"}},
                  {"publish.done": {"pr_urls": ["u"]}}, "summary.done"]
        t, _ = drive(M, self.tr, "01", "executor-a", events)
        self.assertEqual(stages, ["ready", "in-dev", "in-review", "approved"])
        self.assertEqual(t["vars"]["risk"], "high")  # the review raised it

    def test_second_executor_stops_without_writing(self):
        before = self.tr.path("04").read_bytes()
        with self.assertRaisesRegex(ClaimError, "claimed by executor-b"):
            drive(M, self.tr, "04", "executor-a", ["stage.ready"])
        self.assertEqual(self.tr.path("04").read_bytes(), before)

    def test_claim_and_release_round_trip(self):
        before = self.tr.path("01").read_bytes()
        self.tr.claim("01", "executor-a")
        self.tr.claim("01", "executor-a")  # re-claiming your own ticket is fine
        self.assertEqual(self.tr.frontier(), ["06"])
        self.tr.release("01", "executor-b")  # not the holder: no change
        self.tr.release("01", "executor-a")
        self.assertEqual(self.tr.path("01").read_bytes(), before)
        with self.assertRaises(ValueError):
            self.tr.set_state("01", "ready\nclaimed_by: x")

    def test_frontier(self):
        # 02 has an open blocker, 03 is in-dev, 04 is claimed, 05 is done, 07 is a draft.
        self.assertEqual(self.tr.frontier(), ["01", "06"])


if __name__ == "__main__":
    unittest.main()

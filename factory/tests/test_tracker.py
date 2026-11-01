#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Unit tests for factory/local_tracker.py. Run: uv run factory/tests/test_tracker.py"""
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from local_tracker import ClaimError, LocalTracker  # noqa: E402

FIX = HERE / "fixtures"


class Tracker(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.effort = Path(shutil.copytree(FIX / "effort", Path(tmp) / "effort"))
        self.tr = LocalTracker(self.effort)

    def test_review_raising_risk_relabels_and_comments(self):
        self.tr.set_review_labels("01", "low")
        self.tr.set_review_labels("01", "high", docs_impact=True)
        text = self.tr.path("01").read_text()
        self.assertIn("Status: ready\nRisk: high\nDocs impact: yes\n\n", text)
        self.assertNotIn("Risk: low", text)
        with self.assertRaises(KeyError):
            self.tr.set_review_labels("01", "medium")
        self.tr.comment("01", "[factory] hi\n\nFactory-Run: r")
        self.assertTrue(self.tr.path("01").read_text().endswith("## Comments\n\n<!-- comment -->\n[factory] hi\n\nFactory-Run: r\n"))
        self.assertEqual(self.tr.get_state("01"), "ready")

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

    def test_claim_race_exactly_one_holds(self):
        set_field = LocalTracker._set_field

        def slow(tr, *a):  # widen the window between the holder check and the write
            time.sleep(0.05)
            set_field(tr, *a)

        won, lost = [], []
        gate = threading.Barrier(2)

        def go(name):
            tr = LocalTracker(self.effort)
            tr._set_field = lambda *a: slow(tr, *a)
            gate.wait()
            try:
                tr.claim("01", name)
                won.append(name)
            except ClaimError:
                lost.append(name)

        threads = [threading.Thread(target=go, args=(n,)) for n in ("executor-a", "executor-b")]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual((len(won), len(lost)), (1, 1))
        self.assertIn(f"claimed_by: {won[0]}\n", self.tr.path("01").read_text())
        self.assertEqual([p.name for p in self.tr.issues.iterdir() if p.name.startswith(".")], [])

    def test_unattended_runs_push_without_confirmation(self):
        gates = yaml.safe_load((HERE.parents[1] / "factory.yaml").read_text())["gates"]
        self.assertNotIn("push", gates["confirm_external_writes_scope"])
        self.assertEqual(gates["pr_approval"], "human")  # merges stay with the human

    def test_frontier(self):
        # 02 has an open blocker, 03 is in-dev, 04 is claimed, 05 is done, 07 is a draft.
        self.assertEqual(self.tr.frontier(), ["01", "06"])


if __name__ == "__main__":
    unittest.main()

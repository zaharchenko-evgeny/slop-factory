#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Unit tests for factory/local_tracker.py and factory/drive.py. Run: uv run factory/tests/test_tracker.py"""
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
import sm  # noqa: E402
from drive import branch_name, drive  # noqa: E402
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
        self.assertEqual(fresh.read_vars("01"), sm.persisted(t))
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
                  {"approve.done": {"verdict": "needs-human", "reasons": ["risk is high"]}},
                  {"publish.done": {"pr_urls": ["u"]}}, "ci.passed"]
        t, _ = drive(M, self.tr, "01", "executor-a", events)
        self.assertEqual(stages, ["ready", "in-dev", "in-review", "approved"])
        self.assertEqual(t["vars"]["risk"], "high")  # the review raised it

    def test_review_raising_risk_relabels_and_comments(self):
        self.tr.set_review_labels("01", "low")
        self.tr.set_review_labels("01", "high", docs_impact=True)
        text = self.tr.path("01").read_text()
        self.assertIn("Status: ready\nRisk: high\nDocs impact: yes\n\n", text)
        self.assertNotIn("Risk: low", text)
        with self.assertRaises(KeyError):
            self.tr.set_review_labels("01", "medium")
        self.tr.comment("01", "[factory] hi\n\nFactory-Run: r")
        self.assertTrue(self.tr.path("01").read_text().endswith("## Comments\n\n[factory] hi\n\nFactory-Run: r\n"))
        self.assertEqual(self.tr.get_state("01"), "ready")

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

    def test_resume_from_stored_out_and_pending(self):
        events = yaml.safe_load((FIX / "happy.yaml").read_text())["events"]
        cut = next(i for i, e in enumerate(events) if str(next(iter(e)) if isinstance(e, dict) else e).startswith("start."))
        t1, _ = drive(M, self.tr, "01", "executor-a", events[:cut + 1])  # stops with implement running
        self.assertEqual((t1["state"], t1["pending"]), ("in-dev", ["implement"]))
        t2, _ = drive(M, LocalTracker(self.effort), "01", "executor-a", events[cut + 1:])
        self.assertEqual(t2["state"], "approved")
        self.assertEqual(t2["out"]["intake"], t1["out"]["intake"])

    def test_claim_released_when_ticket_read_fails(self):
        self.tr.read_vars = lambda nn: (_ for _ in ()).throw(ValueError("bad state"))
        with self.assertRaisesRegex(ValueError, "bad state"):
            drive(M, self.tr, "01", "executor-a", ["stage.ready"])
        self.assertEqual(self.tr._field("01", "claimed_by"), "")

    def test_branch_name(self):
        pat = "ez/{ticket}-{slug}"
        self.assertEqual(branch_name(pat, 29, "Atomic claims, resumable runs and unattended pushes"),
                         "ez/29-atomic-claims-resumable-runs")
        self.assertEqual(branch_name(pat, 7, "x", "body\nslug: my-slug\n"), "ez/7-my-slug")
        self.assertEqual(branch_name(pat, 7, "!!"), "ez/7-ticket")

    def test_unattended_runs_push_without_confirmation(self):
        gates = yaml.safe_load((sm.ROOT / "factory.yaml").read_text())["gates"]
        self.assertNotIn("push", gates["confirm_external_writes_scope"])
        self.assertEqual(gates["pr_approval"], "human")  # merges stay with the human

    def test_frontier(self):
        # 02 has an open blocker, 03 is in-dev, 04 is claimed, 05 is done, 07 is a draft.
        self.assertEqual(self.tr.frontier(), ["01", "06"])


if __name__ == "__main__":
    unittest.main()

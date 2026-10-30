#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_checks

CFG = {"checks": [
    {"name": "a", "mode": "blocking", "run": "true"},
    {"name": "adv", "mode": "advisory", "run": "false"},
    {"name": "b", "mode": "blocking", "run": "false"},
    {"name": "c", "mode": "blocking", "run": "true"},
]}


class RunChecks(unittest.TestCase):
    def test_blocking_only_stops_at_first_failure(self):
        failed, rows, _ = run_checks.run_checks(CFG, ".", "main", "blocking", stop=True)
        self.assertTrue(failed)
        self.assertEqual([(r["name"], r["status"]) for r in rows], [("a", "pass"), ("b", "fail")])

    def test_all_modes_run_every_check(self):
        _, rows, _ = run_checks.run_checks(CFG, ".", "main")
        self.assertEqual([r["name"] for r in rows], ["a", "adv", "b", "c"])


if __name__ == "__main__":
    unittest.main()

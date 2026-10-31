#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/deploy.py. Run: uv run factory/tests/test_deploy.py
Regenerate the fixture: UPDATE_FIXTURE=1 uv run factory/tests/test_deploy.py"""
import json
import os
import sys
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "factory"))
import deploy
import nodes

FIX = HERE / "fixtures/deploy"


class PlanTest(unittest.TestCase):
    def test_air_matches_fixture(self):
        plan = deploy.plan_air(yaml.safe_load((FIX / "nodes.yaml").read_text()),
                               yaml.safe_load((FIX / "factory.yaml").read_text()))
        got = json.dumps(plan, indent=2, sort_keys=True) + "\n"
        if os.environ.get("UPDATE_FIXTURE"):
            (FIX / "air-plan.json").write_text(got)
        self.assertEqual(got, (FIX / "air-plan.json").read_text())

    def test_real_plan_is_prefixed_and_covers_pilot(self):
        config = yaml.safe_load((ROOT / "factory.yaml").read_text())
        n = nodes.load()
        plan = deploy.plan_air(n, config)
        names = {a["name"] for a in plan["automations"]}
        self.assertTrue(all(x.startswith("[factory:") for x in names))
        self.assertIn("[factory:implement-mid]", names)
        self.assertIn("[factory:review-structure-security]", names)
        self.assertNotIn("[factory:merge]", names)
        self.assertEqual(deploy.plan_air(n, config), plan)

    def test_local_is_launchd_job(self):
        job = deploy.schedule_local.render(yaml.safe_load((ROOT / "factory.yaml").read_text()))
        self.assertEqual(job["Label"], "com.offline-eval-factory.loop")


if __name__ == "__main__":
    unittest.main()

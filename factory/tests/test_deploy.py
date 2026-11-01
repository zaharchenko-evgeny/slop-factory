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

    def test_plan_lists_every_label_the_gate_writes(self):
        plan = deploy.plan_air(nodes.load(), yaml.safe_load((HERE.parent.parent / "factory.yaml").read_text()))
        for want in ("factory:ready", "factory:implement-cheap", "factory:fix-strong", "complexity:small", "risk:high"):
            self.assertIn(want, plan["labels"])
        self.assertFalse([x for x in plan["labels"] if "<" in x])

    def test_script_nodes_only_run_the_sweep(self):
        plan = json.loads((FIX / "air-plan.json").read_text())
        a = {x["name"]: x for x in plan["automations"]}
        self.assertEqual(a["[factory:sweep]"]["prompt"], deploy.SCRIPT_PROMPT)
        self.assertIn("gate.py check --node intake", a["[factory:intake]"]["prompt"])

    def test_air_shape_and_no_github_token(self):
        plan = json.loads((FIX / "air-plan.json").read_text())
        a = {x["name"]: x for x in plan["automations"]}
        self.assertEqual(a["[factory:sweep]"]["triggers"],
                         [{"triggerType": "SCHEDULED", "cronExpression": "*/10 * * * *", "timezone": "UTC"}])
        t = a["[factory:intake]"]["triggers"][0]
        self.assertEqual((t["triggerType"], t["source"]), ("EVENT", "github"))
        self.assertEqual(set(a["[factory:intake]"]["agentAndModel"]), {"id", "name", "isDefault", "agentId"})
        self.assertEqual(set(a["[factory:intake]"]["repository"]), {"fullName", "cloneUrl", "browseUrl", "provider"})
        self.assertEqual(a["[factory:intake]"]["triggerVcs"], [{"outputBehaviour": "read-only"}])
        for e in deploy.plan_air(nodes.load(), yaml.safe_load((ROOT / "factory.yaml").read_text()))["environments"]:
            self.assertNotIn("GITHUB_TOKEN", e["secrets"])
        join = next(x for x in deploy.plan_air(nodes.load(), yaml.safe_load((ROOT / "factory.yaml").read_text()))
                    ["automations"] if x["name"] == "[factory:join]")
        self.assertEqual([(x["eventType"], x["filters"].get("label")) for x in join["triggers"]],
                         [("pull_request_review.submitted", None), ("pull_request.labeled", "factory:ci-red")])
        assert not any(x["triggerType"] == "SCHEDULED" for a in deploy.plan_air(nodes.load(), yaml.safe_load((ROOT / "factory.yaml").read_text()))["automations"] for x in a["triggers"]), "no polling"

    def test_prompt_carries_focus_and_tier(self):
        config = yaml.safe_load((ROOT / "factory.yaml").read_text())
        a = {x["name"]: x["prompt"] for x in deploy.plan_air(nodes.load(), config)["automations"]}
        self.assertIn("check --node review --event <file> --focus behaviour --tier review`", a["[factory:review-behaviour]"])
        self.assertIn("check --node fix --event <file> --tier mid`", a["[factory:fix-mid]"])
        for f, tier in (("full", "review_strong"), ("behaviour", "review"), ("structure-security", "review_strong")):
            self.assertIn(f"check --node review --event <file> --focus {f} --tier {tier}`", a[f"[factory:review-{f}]"])
        self.assertIn("apply --node review --focus behaviour --tier review --event", a["[factory:review-behaviour]"])
        self.assertIn("apply --node fix --tier mid --event", a["[factory:fix-mid]"])
        self.assertIn("check --node intake --event <file>`", a["[factory:intake]"])

    def test_real_plan_is_prefixed_and_covers_pilot(self):
        config = yaml.safe_load((ROOT / "factory.yaml").read_text())
        n = nodes.load()
        plan = deploy.plan_air(n, config)
        names = {a["name"] for a in plan["automations"]}
        self.assertTrue(all(x.startswith("[factory:") for x in names))
        pilot = {f"[factory:{x}]" for x in (
            "intake", "join", "finish",
            *(f"{b}-{t}" for b in ("implement", "fix") for t in ("cheap", "mid", "strong")),
            *(f"review-{f}" for f in ("full", "behaviour", "structure-security")))}
        self.assertEqual(names, pilot)
        self.assertEqual({n for n, v in n["nodes"].items() if v.get("pilot")},
                         {"intake", "implement", "review", "join", "fix", "finish"})
        self.assertEqual(deploy.plan_air(n, config), plan)

    def test_local_is_launchd_job(self):
        job = deploy.schedule_local.render(yaml.safe_load((ROOT / "factory.yaml").read_text()))
        self.assertEqual(job["Label"], "com.offline-eval-factory.loop")


if __name__ == "__main__":
    unittest.main()

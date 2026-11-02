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
import config as cfg  # noqa: E402
cfg.set_team(Path(__file__).resolve().parents[2])
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

    def test_nodes_get_environment_by_env(self):
        plan = deploy.plan_air(nodes.load(), cfg.load())
        env = {x["name"]: x["environmentConfigId"] for x in plan["automations"]}
        for name, want in (("intake", "read"), ("join", "read"), ("finish", "read"), ("review-full", "read"),
                           ("implement-mid", "write"), ("fix-cheap", "write"), ("update-strong", "write")):
            self.assertEqual(env[f"[Slop factory] {name}"], f"[Slop factory] {want}")
        self.assertEqual({e["name"]: e["variables"] for e in plan["environments"]},
                         {"[Slop factory] read": {"FACTORY_ENV": "read"}, "[Slop factory] write": {"FACTORY_ENV": "write"},
                          "[Slop factory] ops": {"FACTORY_ENV": "ops"}})  # ops: only a disabled node, still planned

    def test_local_target_is_rejected(self):
        self.assertEqual(deploy.main(["plan", "--target", "local"]), 2)

    def test_plan_lists_every_label_the_gate_writes(self):
        plan = deploy.plan_air(nodes.load(), cfg.load())
        for want in ("factory:ready", "factory:implement-cheap", "factory:fix-strong", "complexity:small", "risk:high"):
            self.assertIn(want, plan["labels"])
        self.assertFalse([x for x in plan["labels"] if "<" in x])

    def test_script_nodes_only_run_the_sweep(self):
        plan = json.loads((FIX / "air-plan.json").read_text())
        a = {x["name"]: x for x in plan["automations"]}
        self.assertTrue(a["[Slop factory] sweep"]["prompt"].endswith("Run `factory sweep` once and report its JSON output."))
        self.assertIn("`factory begin intake`", a["[Slop factory] intake"]["prompt"])

    def test_recover_runs_the_stale_sweep_twice_a_day_on_the_cheap_tier(self):
        for plan in (json.loads((FIX / "air-plan.json").read_text()),
                     deploy.plan_air(nodes.load(), cfg.load())):
            a = next(x for x in plan["automations"] if x["name"] == "[Slop factory] recover")
            self.assertEqual(a["triggers"], [{"triggerType": "SCHEDULED", "cronExpression": "0 6,18 * * *", "timezone": "UTC"}])
            self.assertTrue(a["prompt"].endswith("Run `factory sweep --stale` once and report its JSON output."))
            intake = next(x for x in plan["automations"] if x["name"] == "[Slop factory] intake")  # router: cheap
            self.assertEqual(a["agentAndModel"], intake["agentAndModel"])

    def test_air_shape_and_no_github_token(self):
        plan = json.loads((FIX / "air-plan.json").read_text())
        a = {x["name"]: x for x in plan["automations"]}
        self.assertEqual(a["[Slop factory] sweep"]["triggers"],
                         [{"triggerType": "SCHEDULED", "cronExpression": "*/10 * * * *", "timezone": "UTC"}])
        t = a["[Slop factory] intake"]["triggers"][0]
        self.assertEqual((t["triggerType"], t["source"]), ("EVENT", "github"))
        self.assertEqual(set(a["[Slop factory] intake"]["agentAndModel"]), {"id", "name", "isDefault", "agentId"})
        self.assertEqual(set(a["[Slop factory] intake"]["repository"]), {"fullName", "cloneUrl", "browseUrl", "provider"})
        self.assertEqual(a["[Slop factory] intake"]["triggerVcs"], [{"outputBehaviour": "read-only"}])
        for e in deploy.plan_air(nodes.load(), cfg.load())["environments"]:
            self.assertNotIn("GITHUB_TOKEN", e["secrets"])
        join = next(x for x in deploy.plan_air(nodes.load(), cfg.load())
                    ["automations"] if x["name"] == "[Slop factory] join")
        self.assertEqual([(x["eventType"], x["filters"].get("label")) for x in join["triggers"]],
                         [("pull_request_review.submitted", None), ("pull_request.labeled", "factory:ci-red")])
        assert not any(x["triggerType"] == "SCHEDULED" for a in deploy.plan_air(nodes.load(), cfg.load())["automations"] if a["name"] != "[Slop factory] recover" for x in a["triggers"]), "no polling"  # recover is the one safety net

    def test_one_triggervcs_entry_per_trigger_and_a_spec_per_automation(self):
        for a in deploy.plan_air(nodes.load(), cfg.load())["automations"]:
            self.assertEqual(len(a["triggerVcs"]), len(a["triggers"]), a["name"])
            self.assertNotIn("```json", a["prompt"])  # no JSON in a prompt: the run compiles its spec
        a = {x["name"]: x for x in deploy.plan_air(nodes.load(), cfg.load())["automations"]}
        spec = a["[Slop factory] implement-mid"]["spec"]
        self.assertEqual((spec["node"], spec["tier"], spec["engine"]), ("implement", "mid", cfg.load()["engine"]))
        self.assertEqual(set(spec["outcomes"]), {"done", "error", "blocked", "failed"})
        self.assertIn("- `error`: the work needs the next tier", a["[Slop factory] implement-mid"]["prompt"])
        self.assertEqual(set(a["[Slop factory] join"]["spec"]["runs"]), {"finish", "recover"})

    def test_compile_automation_gives_the_gate_options(self):
        config, graph = cfg.load(), nodes.load()
        got = deploy.compile_automation("review-behaviour", graph, config)
        self.assertEqual((got["node"], got["focus"], got["tier"]), ("review", "behaviour", "review"))
        self.assertEqual(deploy.compile_automation("fix-mid", graph, config)["tier"], "mid")
        self.assertIsNone(deploy.compile_automation("intake", graph, config)["tier"])
        self.assertRaises(KeyError, deploy.compile_automation, "nope", graph, config)

    def test_agent_prompt_sections(self):
        a = {x["name"]: x["prompt"] for x in deploy.plan_air(nodes.load(), cfg.load())["automations"]}
        for name, role, intro in (("review-behaviour", "reviewer", "You review one branch independently"),
                                  ("implement-mid", "developer", "You implement one ticket in its worktree")):
            prompt = a[f"[Slop factory] {name}"]
            marks = [prompt.index(m) for m in (intro, "## Your skills", "## This step", "## How you start and finish")]
            self.assertEqual(marks, sorted(marks), name)
            self.assertTrue(prompt.startswith(f"You are the {role} of Slop factory"))
            self.assertIn((ROOT / f"roles/{role}/role.md").read_text().strip(), prompt)
            self.assertIn(f"`factory begin {name}`", prompt)
            self.assertIn("`factory end <handoff_path>`", prompt)
            for word in ("gate.py", "--node", "--tier", "--focus", "read `role_file`"):
                self.assertNotIn(word, prompt)
        self.assertIn("(behaviour focus)", a["[Slop factory] review-behaviour"])
        self.assertIn("- ponytail: Lazy senior dev mode", a["[Slop factory] implement-mid"])
        intake = a["[Slop factory] intake"]  # `bug` is disabled in the graph
        self.assertIn("- `grill`:", intake)
        self.assertNotIn("- `bug`:", intake)

    def test_pull_request_scripts_run_from_main(self):
        a = {x["name"]: x["prompt"] for x in deploy.plan_air(nodes.load(), cfg.load())["automations"]}
        self.assertIn("`factory sweep --main`", a["[Slop factory] join"])
        self.assertIn("`factory sweep --main --issue <issue number from the trigger payload>`", a["[Slop factory] finish"])
        self.assertNotIn("--main", a["[Slop factory] recover"])

    def test_real_plan_is_prefixed_and_covers_pilot(self):
        config = cfg.load()
        n = nodes.load()
        plan = deploy.plan_air(n, config)
        names = {a["name"] for a in plan["automations"]}
        self.assertEqual(plan["prefix"], "[Slop factory] ")
        self.assertTrue(all(x.startswith(plan["prefix"]) for x in names))
        pilot = {f"[Slop factory] {x}" for x in (
            "intake", "join", "finish", "recover",
            *(f"{b}-{t}" for b in ("implement", "fix", "update") for t in ("cheap", "mid", "strong")),
            *(f"review-{f}" for f in ("full", "behaviour", "structure-security")))}
        self.assertEqual(names, pilot)
        self.assertEqual({n for n, v in n["nodes"].items() if v.get("pilot")},
                         {"intake", "implement", "review", "join", "fix", "update", "finish", "recover"})
        self.assertEqual(deploy.plan_air(n, config), plan)


if __name__ == "__main__":
    unittest.main()

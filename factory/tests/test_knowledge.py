#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/knowledge.py. Run: uv run factory/tests/test_knowledge.py"""
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import knowledge  # noqa: E402
import nodes  # noqa: E402
from gate_port import MemoryTracker  # noqa: E402


class RetroTest(unittest.TestCase):
    def test_two_done_tickets_write_two_entries_once(self):
        tr = MemoryTracker({1: {"stage": "done", "title": "A", "fix_rounds": 2}, 2: {"stage": "done", "title": "B"},
                            3: {"stage": "in-review", "title": "C"}})
        inbox = {}
        self.assertEqual(knowledge.retro_entries(tr, [1, 2, 3], inbox), ["retro-1.md", "retro-2.md"])
        self.assertIn("fix rounds 2", inbox["retro-1.md"])
        self.assertEqual(knowledge.retro_entries(tr, [1, 2, 3], inbox), [])  # a rerun adds nothing


class DistillTest(unittest.TestCase):
    def test_three_entries_plan_one_pr_per_target(self):
        entries = {
            "retro-1.md": "- [project] docs/agents/testing.md: run uv run for tests\n- [noise] it rained",
            "retro-2.md": "- [factory] roles/developer/role.md: run ShellCheck first",
            "retro-3.md": "- [engine] gate retries a merged PR\n- [project] docs/agents/testing.md: dup",
        }
        plan = knowledge.distill_plan(entries)
        self.assertEqual(plan["prs"], {"project": {"files": ["AGENTS.md", "docs/agents/testing.md"]},
                                       "team": {"files": ["roles/developer/role.md"]}})
        self.assertEqual(plan["tickets"], ["gate retries a merged PR"])
        self.assertEqual(plan["consumed"], sorted(entries))

    def test_paths_outside_the_target_are_dropped(self):
        plan = knowledge.distill_plan({"e.md": "- [project] factory/gate.py: x\n- [factory] docs/design.md: y"})
        self.assertEqual(plan["prs"], {})


class RoutineNodesTest(unittest.TestCase):
    def test_knowledge_paths_under_team_root(self):
        roles = ROOT / "roles"
        for name in ("retro/role.md", "distill/role.md"):
            self.assertIn("$FACTORY_TEAM/", (roles / name).read_text())
        self.assertIn("knowledge` directory under `$FACTORY_TEAM", (roles / "_common.md").read_text())
        self.assertIn("knowledge` directory under `$FACTORY_TEAM", (roles / "reviewer/role.md").read_text())
        self.assertIn("relative to the team root (`FACTORY_TEAM`)", (ROOT / "factory/knowledge.py").read_text())

    def test_both_schedules_run_on_ops(self):
        n = nodes.load()
        crons = {k: v["cron"] for k, v in n["schedules"].items()}
        self.assertEqual((crons["retro"], crons["distill"]), ("0 2 * * *", "0 3 * * 1"))
        for name, role in (("retro", "retro"), ("distill", "distill")):
            self.assertEqual((n["nodes"][name]["env"], n["nodes"][name]["role"], n["nodes"][name]["triggers"]),
                             ("ops", role, [f"schedule {name}"]))
        self.assertIn("knowledge", n["nodes"]["review"]["focuses"])


if __name__ == "__main__":
    unittest.main()

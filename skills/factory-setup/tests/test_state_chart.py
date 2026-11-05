#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The state chart generator and verifier. Run: uv run skills/factory-setup/tests/test_state_chart.py"""
import copy
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
ROOT = SKILL.parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL))
import render  # noqa: E402
import state_chart as sc  # noqa: E402

STATES = {
    "ready": {"kind": "active", "store": {"label": "factory:ready"}},
    "in-dev": {"kind": "active", "store": {"label": "factory:in-dev"}},
    "done": {"kind": "final", "store": {"label": "factory:done"}},
    "blocked": {"kind": "waiting", "store": {"label": "factory:blocked"}},
}
CHAIN = {
    "nodes": {
        "start": {"wakes_on": ["state ready"], "when": {"state": "ready"}, "role": "script", "outcomes": {
            "started": {"effects": [{"state": "in-dev"}]}}},
        "implement": {"wakes_on": ["state in-dev"], "when": {"state": "in-dev"}, "role": "developer", "outcomes": {
            "done": {"effects": [{"state": "done"}]},
            "blocked": {"effects": [{"state": "blocked"}]}}},
    }
}


def project(states=STATES):
    return {"states": copy.deepcopy(states)}


def workflow(nodes=None):
    return copy.deepcopy(nodes or CHAIN)


def team_copy(tmp, delivery_md=None):
    """A team directory with the template's project files and a delivery.md the test may change."""
    shutil.copy(ROOT / "template/factory.project.yaml", Path(tmp) / "factory.project.yaml")
    (Path(tmp) / "workflows").mkdir()
    shutil.copy(ROOT / "template/workflows/delivery.yaml", Path(tmp) / "workflows/delivery.yaml")
    text = (ROOT / "template/workflows/delivery.md").read_text() if delivery_md is None else delivery_md
    (Path(tmp) / "workflows/delivery.md").write_text(text)


class Chart(unittest.TestCase):
    def test_template_chart_is_current_and_sound(self):
        self.assertEqual(sc.check(ROOT / "template"), [])

    def test_rendered_project_chart_verifies(self):
        answers = {name: f"example-{name.lower()}" for name in
                   {m for f in render.PROJECT_T.rglob("*") if f.is_file() for m in render.PH.findall(f.read_text())}}
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in render.render_project(answers).items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(text)
            self.assertEqual(sc.check(tmp), [])

    def test_clean_chain_has_no_errors(self):
        self.assertEqual(sc.soundness(project(), workflow()), [])

    def test_dead_end_active_state_is_reported(self):
        nodes = workflow()
        del nodes["nodes"]["implement"]["outcomes"]["done"]   # in-dev now leaves only to blocked
        del nodes["nodes"]["implement"]["outcomes"]["blocked"]
        errors = sc.soundness(project(), nodes)
        self.assertTrue(any(e.startswith("dead-end-state: in-dev") for e in errors), errors)

    def test_island_without_entry_is_unreachable(self):
        # Two states that only lead to each other: no start state reaches them.
        states = {**STATES,
                  "loop-a": {"kind": "active", "store": {"label": "factory:loop-a"}},
                  "loop-b": {"kind": "active", "store": {"label": "factory:loop-b"}}}
        nodes = workflow()
        nodes["nodes"]["spin"] = {"wakes_on": ["state loop-a"], "when": {"state": "loop-a"}, "role": "script",
                                  "outcomes": {"go": {"effects": [{"state": "loop-b"}]}}}
        nodes["nodes"]["spin2"] = {"wakes_on": ["state loop-b"], "when": {"state": "loop-b"}, "role": "script",
                                   "outcomes": {"go": {"effects": [{"state": "loop-a"}]}}}
        errors = sc.soundness(project(states), nodes)
        self.assertTrue(any(e.startswith("unreachable-state: loop-a") for e in errors), errors)
        self.assertTrue(any(e.startswith("unreachable-state: loop-b") for e in errors), errors)

    def test_state_without_entry_point_is_a_start_and_needs_a_way_out(self):
        states = {**STATES, "orphan": {"kind": "active", "store": {"label": "factory:orphan"}}}
        errors = sc.soundness(project(states), workflow())
        self.assertEqual(errors, ["dead-end-state: orphan: an active state with no arrow out; a ticket there never moves"])

    def test_unknown_state_is_reported(self):
        nodes = workflow()
        nodes["nodes"]["implement"]["outcomes"]["done"]["effects"] = [{"state": "shipped"}]
        errors = sc.soundness(project(), nodes)
        self.assertTrue(any(e.startswith("unknown-state: implement.done: shipped") for e in errors), errors)

    def test_disabled_node_leaves_its_state_off(self):
        nodes = workflow()
        nodes["nodes"]["extra"] = {"enabled": False, "wakes_on": ["state ready"], "when": {"state": "ready"},
                                   "role": "router", "outcomes": {"later": {"effects": [{"state": "blocked"}]}}}
        states = {**STATES, "later": {"kind": "active", "store": {"label": "factory:later"}}}
        nodes["nodes"]["extra"]["outcomes"]["later"]["effects"] = [{"state": "later"}]
        self.assertEqual(sc.soundness(project(states), nodes), [])
        _, _, off, _ = sc.graph(project(states), nodes)
        self.assertEqual(off, {"later"})
        self.assertIn("class s_later off", sc.mermaid(*sc.graph(project(states), nodes)[:2], off=off))

    def test_pr_fact_state_may_start_a_path(self):
        states = {**STATES, "ci-red": {"kind": "active", "store": {"pr": "ci-red"}}}
        self.assertEqual(sc.soundness(project(states), workflow()), [])

    def test_drift_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            team_copy(tmp, (ROOT / "template/workflows/delivery.md").read_text().replace(
                "s_in_dev --> s_in_review", "s_in_dev --> s_in_dev", 1))
            self.assertTrue(any(e.startswith("chart-stale") for e in sc.check(tmp)))

    def test_missing_markers_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            team_copy(tmp, "# Workflow\n")
            self.assertTrue(any(e.startswith("chart-missing") for e in sc.check(tmp)))

    def test_write_restores_current_chart(self):
        with tempfile.TemporaryDirectory() as tmp:
            team_copy(tmp, (ROOT / "template/workflows/delivery.md").read_text().replace(
                "s_in_dev --> s_in_review", "s_in_dev --> s_in_dev", 1))
            sc.write(tmp)
            self.assertEqual(sc.check(tmp), [])

    def test_labels_keep_colons_and_angles_out_of_mermaid(self):
        nodes = workflow()
        nodes["nodes"]["implement"]["outcomes"]["done"]["effects"].append({"command": "implement-<next tier>"})
        text = sc.mermaid(*sc.graph(project(), nodes)[:2])
        self.assertIn("implement · done / +implement-{next tier}", text)
        self.assertNotIn("<", text.split("\n", 1)[1])


if __name__ == "__main__":
    unittest.main()

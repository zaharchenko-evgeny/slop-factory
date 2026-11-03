#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/nodes.py. Run: uv run factory/tests/test_nodes.py"""
import copy
import re
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
cfg.set_team(Path(__file__).resolve().parents[2] / "factory/tests/fixtures/team")
import nodes  # noqa: E402

CONFIG = cfg.load()


def broken(mutate):
    n = copy.deepcopy(nodes.load())
    mutate(n["nodes"])
    return nodes.validate(n, CONFIG)


class NodesTest(unittest.TestCase):
    def test_file_is_valid(self):
        self.assertEqual(nodes.validate(nodes.load(), CONFIG), [])

    def test_project_name_is_required(self):
        for project in ({}, {"name": "  "}):
            config = copy.deepcopy(CONFIG)
            config["project"] = project
            with self.subTest(project=project):
                self.assertTrue(any(e.startswith("project-name:") for e in nodes.validate(nodes.load(), config)))

    def test_broken_fixtures(self):
        cases = {
            "no-trigger": lambda n: n["intake"].update(triggers=[]),
            "unknown-effect": lambda n: n["join"]["outcomes"]["approved"]["effects"].append({"explode": 1}),
            "ci-gate": lambda n: n["finish"].pop("ci-independent"),
            "unknown-role": lambda n: n["intake"].update(role="ghost"),
            "unknown-environment": lambda n: n["intake"].update(env="moon"),
            "unreachable-stage": lambda n: n["join"]["outcomes"].pop("approved"),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                errs = broken(mutate)
                self.assertTrue(any(e.startswith(name) for e in errs), errs)

    def test_unused_stage_fails_validation(self):
        config = copy.deepcopy(CONFIG)
        config["tracker"]["stages"]["orphan"] = "factory:orphan"
        self.assertIn("unused-stage: orphan", nodes.validate(nodes.load(), config))

    def test_chart_is_current(self):
        want = nodes.block(nodes.load())
        for doc in ("docs/statechart.md", "docs/statechart-proposed.md"):
            m = re.search(r"<!-- nodes-chart -->\n(.*?)<!-- /nodes-chart -->", (ROOT / doc).read_text(), re.S)
            self.assertIsNotNone(m, f"{doc} has no nodes-chart block")
            self.assertEqual(m.group(1), want, f"stale: run nodes.py chart and update {doc}")

    def test_chart_marks_planned_nodes_dashed_and_lists_triggers(self):
        c = nodes.chart(nodes.load())
        self.assertIn("on: issue +factory:ready", c)
        self.assertIn("on: PR +factory:ci-green", c)
        self.assertRegex(c, r'N_adopt\[.*\]:::planned')
        self.assertIn("-.->", c)


if __name__ == "__main__":
    unittest.main()

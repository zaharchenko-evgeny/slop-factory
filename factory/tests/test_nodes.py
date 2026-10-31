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
import nodes  # noqa: E402

CONFIG = yaml.safe_load((ROOT / "factory.yaml").read_text())


def broken(mutate):
    n = copy.deepcopy(nodes.load())
    mutate(n["nodes"])
    return nodes.validate(n, CONFIG)


class NodesTest(unittest.TestCase):
    def test_file_is_valid(self):
        self.assertEqual(nodes.validate(nodes.load(), CONFIG), [])

    def test_broken_fixtures(self):
        cases = {
            "no-trigger": lambda n: n["intake"].update(triggers=[]),
            "unknown-effect": lambda n: n["join"]["outcomes"]["approved"]["effects"].append({"explode": 1}),
            "unknown-role": lambda n: n["intake"].update(role="ghost"),
            "unknown-environment": lambda n: n["intake"].update(environment="moon"),
            "unreachable-stage": lambda n: n["implement"]["outcomes"].pop("failed"),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                errs = broken(mutate)
                self.assertTrue(any(e.startswith(name) for e in errs), errs)

    def test_chart_is_current(self):
        doc = (ROOT / "docs/statechart.md").read_text()
        m = re.search(r"<!-- nodes-chart -->\n```mermaid\n(.*?)```\n<!-- /nodes-chart -->", doc, re.S)
        self.assertIsNotNone(m, "docs/statechart.md has no nodes-chart block")
        self.assertEqual(m.group(1), nodes.chart(nodes.load()), "stale: run nodes.py chart and update docs/statechart.md")


if __name__ == "__main__":
    unittest.main()

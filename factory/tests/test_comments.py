#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Tests for the opt-in comments module (ADR-0025): off by default, on with `modules: [comments]`.
Run: uv run factory/tests/test_comments.py"""
import copy
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import config as cfg  # noqa: E402
import project_mode  # noqa: E402
cfg.set_team(project_mode.team(Path(__file__).resolve().parent / "fixtures/team"))
import definition  # noqa: E402
import gate  # noqa: E402
import nodes  # noqa: E402
from gate_port import MemoryTracker  # noqa: E402

FIXTURES = ROOT / "tests/fixtures"


class DefinitionModuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "team"
        shutil.copytree(FIXTURES / "v2", self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def edit(self, name, change):
        path = self.root / name
        data = yaml.safe_load(path.read_text())
        change(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    def off(self):
        self.edit(definition.FACTORY, lambda f: f.pop("modules"))

    def test_slop_fixture_has_the_module_off(self):
        defn = definition.load(FIXTURES / "slop")
        self.assertFalse(definition.module_on(defn, "comments"))
        self.assertFalse({n for n in defn["workflow"]["nodes"] if n == "mentions" or n.startswith("reply-")})
        self.assertNotIn("mention-router", defn["factory"]["agents"])

    def test_off_generates_nothing_and_warns_for_each_mentionable_agent(self):
        on = definition.load(self.root)
        self.assertTrue({"mentions", "reply-dev", "reply-reviewer"} <= set(on["workflow"]["nodes"]))
        self.assertIn("ask", on["workflow"]["nodes"]["implement-medium"]["outcomes"])
        self.assertFalse([w for w in definition.warnings(on) if w.startswith("module-off")])
        self.off()
        off = definition.load(self.root)
        self.assertFalse({n for n in off["workflow"]["nodes"] if n == "mentions" or n.startswith("reply-")})
        self.assertNotIn("ask", off["workflow"]["nodes"]["implement-medium"]["outcomes"])
        self.assertEqual([w for w in definition.warnings(off) if w.startswith("module-off")],
                         ["module-off: agents.dev: mentionable needs modules: [comments]",
                          "module-off: agents.reviewer: mentionable needs modules: [comments]"])
        self.assertEqual(definition.errors(off), [])

    def test_off_refuses_the_module_guard_and_effects(self):
        def comment_node(w):
            w["nodes"]["comment"] = {**w["nodes"]["intake"], "outcomes": {
                "route": {"effects": [{"route": "to"}]}, "asked": {"if": {"mention": "dev"}, "effects": []},
                "answered": {"effects": [{"comment": "reply", "on": "mention"}]}}}
        self.edit(definition.WORKFLOW, comment_node)
        self.assertFalse([e for e in definition.errors(definition.load(self.root)) if e.startswith(("module-off", "unknown-"))])
        self.off()
        errs = [e for e in definition.errors(definition.load(self.root)) if e.startswith(("module-off", "unknown-"))]
        self.assertEqual(sorted(errs), ["module-off: nodes.comment.answered: `on: mention` needs modules: [comments]",
                                        "module-off: nodes.comment.asked: mention needs modules: [comments]",
                                        "module-off: nodes.comment.route: route needs modules: [comments]"])

    def test_unknown_module(self):
        self.edit(definition.FACTORY, lambda f: f.update(modules=["comments", "chat"]))
        self.assertIn("unknown-module: modules: chat", definition.errors(definition.load(self.root)))

    def test_reserved_names_are_free_while_off(self):
        def reply_node(w):
            w["nodes"]["reply-x"] = {**w["nodes"]["intake"]}
        self.edit(definition.WORKFLOW, reply_node)
        self.assertTrue([e for e in definition.errors(definition.load(self.root)) if e.startswith("reserved-node")])
        self.off()
        self.assertFalse([e for e in definition.errors(definition.load(self.root)) if e.startswith("reserved-node")])

    def test_config_carries_the_modules(self):
        before = cfg._team
        cfg.set_team(self.root)
        try:
            self.assertEqual(cfg.load()["modules"], ["comments"])
            self.off()
            self.assertEqual(cfg.load()["modules"], [])
        finally:
            cfg.set_team(before)


class GateModuleTest(unittest.TestCase):
    """The gate refuses the module's guard, effects and `ask` while the module is off."""

    def graph(self):
        graph = copy.deepcopy(nodes.load())
        graph["nodes"]["comment"] = {**graph["nodes"]["intake"], "when": {"always": True}, "outcomes": {
            "route": {"effects": [{"route": "to"}]}, "ask": {"to": ["reviewer"], "effects": [{"comment": "text"}]}}}
        return graph

    def test_off_by_default(self):
        self.assertEqual(cfg.load()["modules"], [])
        self.assertFalse(gate.comments_on())

    def test_off_refuses_comment_effects_and_asks(self):
        tr = MemoryTracker({"7": {"stage": "triage"}})
        with self.assertRaisesRegex(gate.Bad, r"comments module is off \(route needs `modules: \[comments\]`\)"):
            gate.apply("comment", "route", {"ticket": "7", "to": "fix"}, tr, self.graph())
        with self.assertRaisesRegex(gate.Bad, r"comments module is off \(ask needs"):
            gate.apply("comment", "ask", {"ticket": "7", "to": "reviewer", "text": "?"}, tr, self.graph())
        self.assertNotIn("log", tr.tickets["7"])

    def test_off_refuses_the_mention_guard(self):
        with self.assertRaisesRegex(gate.Bad, r"unknown guard: mention \(mention needs `modules: \[comments\]`\)"):
            gate.guard_holds({"mention": "dev"}, {"comments": [{"body": "@factory:dev ?"}]})
        with patch("gate.comments_on", return_value=True):
            self.assertTrue(gate.guard_holds({"mention": "dev"}, {"comments": [{"body": "@factory:dev ?"}]}))

    def test_graph_validation_names_the_module(self):
        graph, config = self.graph(), cfg.load()
        errs = nodes.validate(graph, config)
        self.assertIn("unknown-effect: comment.route: {'route': 'to'} (needs modules: [comments])", errs)
        self.assertFalse([e for e in nodes.validate(graph, {**config, "modules": ["comments"]}) if "comment.route" in e])


class PackagingTest(unittest.TestCase):
    def test_the_module_ships_with_the_engine(self):
        pyproject = tomllib.loads((ROOT.parent / "pyproject.toml").read_text())
        self.assertIn("comments", pyproject["tool"]["setuptools"]["packages"])


if __name__ == "__main__":
    unittest.main()

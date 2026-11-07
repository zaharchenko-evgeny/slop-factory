#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Tests for factory/team_setup.py (ADR-0021). Run: uv run factory/tests/test_setup.py"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import definition  # noqa: E402
import team_setup  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/v2"


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root, self.home, self.src = self.tmp / "team", self.tmp / "home", self.tmp / "src"
        shutil.copytree(FIXTURE, self.root)
        # A fetched skill is one directory with a SKILL.md; factory.yaml pins every skill to its hash.
        (self.src).mkdir()
        (self.src / "SKILL.md").write_text("---\nname: s\n---\nA skill.\n")
        digest = team_setup.tree_hash(self.src)
        path = self.root / "factory.yaml"
        data = yaml.safe_load(path.read_text())
        for spec in data["skills"].values():
            spec["sha256"] = digest
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        self.ran = []

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def install(self, environment="engine", fetcher=None):
        return team_setup.install(definition.load(self.root), environment, self.home,
                                  fetcher or (lambda spec, tmp: self.src),
                                  run=lambda cmd, **kw: self.ran.append(cmd))

    def test_installs_skills_of_the_environments_roles(self):
        lines = self.install()
        skills = {p.name for p in (self.home / ".agents/skills").iterdir()}
        # engine: developer, reviewer, router agents run there; retro runs in `knowledge`
        self.assertTrue({"factory-step", "ponytail", "tdd", "ponytail-review", "to-tickets"} <= skills, skills)
        self.assertNotIn("retro", skills)
        self.assertIn("skill tdd: github:cursor/plugins@4dd1d05818d1", lines)

    def test_refuses_a_skill_whose_hash_differs(self):
        other = self.tmp / "other"
        other.mkdir()
        (other / "SKILL.md").write_text("changed\n")
        with self.assertRaises(SystemExit) as e:
            self.install(fetcher=lambda spec, tmp: other)
        self.assertIn("sha256 differs", str(e.exception))

    def test_runs_role_setup_scripts(self):
        self.install()
        self.assertIn(["bash", str(self.root / "roles/developer/setup.sh")], self.ran)
        self.ran.clear()
        self.install("knowledge")
        self.assertEqual(self.ran, [])   # retro's role has no setup.sh

    def test_local_skill_and_environment_script(self):
        (self.root / "roles/reviewer/skills/adr-reading").mkdir(parents=True)
        (self.root / "roles/reviewer/skills/adr-reading/SKILL.md").write_text("Read ADRs.\n")
        (self.root / "tools.sh").write_text("true\n")
        path = self.root / "factory.yaml"
        data = yaml.safe_load(path.read_text())
        data["roles"]["reviewer"]["skills"].append({"adr-reading": {"path": "roles/reviewer/skills/adr-reading"}})
        data["environments"]["engine"]["setup"] = "tools.sh"
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        lines = self.install()
        self.assertTrue((self.home / ".agents/skills/adr-reading/SKILL.md").is_file())
        self.assertEqual(self.ran[0], ["bash", str(self.root / "tools.sh")])   # environment first, then roles
        self.assertIn("ran tools.sh", lines)

    def test_mcp_json_has_exactly_the_nodes_custom_servers_with_references_kept(self):
        path = self.root / "factory.yaml"
        data = yaml.safe_load(path.read_text())
        data["defaults"].setdefault("env", {}).update({"IDE_PORT": {}, "DOCS_TOKEN": {"secret": True}})
        data["roles"]["developer"]["mcp"] = {
            "youtrack": {"connector": "internal-youtrack"},
            "idea": {"command": "npx", "args": ["-y", "@jetbrains/mcp-proxy"], "env": {"IDE_PORT": "${IDE_PORT}"}}}
        data["agents"]["dev"]["mcp"] = {"docs": {"url": "https://mcp.example/sse", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}}}
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        checkout = self.tmp / "checkout"
        checkout.mkdir()
        defn = definition.load(self.root)
        lines = team_setup.install(defn, "engine", self.home, lambda spec, tmp: self.src, run=lambda cmd, **kw: None,
                                   checkout=checkout, node="implement-medium")
        written = json.loads((checkout / ".mcp.json").read_text())
        self.assertEqual(written, {"mcpServers": {
            "idea": {"command": "npx", "args": ["-y", "@jetbrains/mcp-proxy"], "env": {"IDE_PORT": "${IDE_PORT}"}},
            "docs": {"url": "https://mcp.example/sse", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}}}})
        self.assertIn(".mcp.json: docs, idea", lines)
        (checkout / ".mcp.json").unlink()
        team_setup.install(defn, "engine", self.home, lambda spec, tmp: self.src, run=lambda cmd, **kw: None,
                           checkout=checkout, node="intake")   # a node without custom servers: no file
        self.assertFalse((checkout / ".mcp.json").exists())

    def test_mcp_json_of_an_environment_without_a_node_is_the_union_and_conflicts_fail(self):
        path = self.root / "factory.yaml"
        data = yaml.safe_load(path.read_text())
        data["roles"]["developer"]["mcp"] = {"a": {"command": "x"}}
        data["agents"]["dev-strong"]["mcp"] = {"a": {"command": "y"}}
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        with self.assertRaises(SystemExit) as e:
            team_setup.install(definition.load(self.root), "engine", self.home, lambda spec, tmp: self.src,
                               run=lambda cmd, **kw: None, checkout=self.tmp)
        self.assertIn("define MCP server a differently", str(e.exception))

    def test_refuses_an_invalid_definition(self):
        (self.root / "roles/router/role.md").unlink()
        with self.assertRaises(SystemExit) as e:
            self.install()
        self.assertIn("missing-role-body", str(e.exception))


if __name__ == "__main__":
    unittest.main()

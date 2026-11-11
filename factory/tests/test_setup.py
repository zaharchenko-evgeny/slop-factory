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

    def test_credential_of_a_writable_repository_with_a_token(self):
        """#1237: only a non-main `write: true` repository with a `token` gets a helper; it names the secret, not its value."""
        main, other = "o/main", "o/other"
        defn = definition.load(self.root)
        cases = [  # (name, second repository keys, secret set, helper set)
            ("write and token", {"write": True, "token": "OTHER_TOKEN"}, True, True),
            ("write, token not set", {"write": True, "token": "OTHER_TOKEN"}, False, False),
            ("write, no token", {"write": True}, True, False),
            ("token, read-only", {"token": "OTHER_TOKEN"}, True, False),
        ]
        for name, keys, secret, helper in cases:
            with self.subTest(name):
                checkout, ran = self.tmp / name.replace(" ", "-") / "main", []
                (checkout.parent / "other/.git").mkdir(parents=True)
                checkout.mkdir()
                defn["factory"]["environments"]["engine"]["repositories"] = [{"repo": main, "write": True}, {"repo": other, **keys}]
                lines = team_setup.set_credentials(defn, "engine", checkout, run=lambda cmd, **kw: ran.append(cmd),
                                                   environ={"OTHER_TOKEN": "s3cret"} if secret else {})
                self.assertEqual(bool(ran), helper, ran)
                self.assertNotIn("s3cret", " ".join(lines + [" ".join(c) for c in ran]))
                if helper:
                    self.assertEqual(ran[0][-2:], ["credential.helper", ""])
                    self.assertIn("password=$OTHER_TOKEN", ran[1][-1])
                    self.assertEqual(lines, ["credential o/other: OTHER_TOKEN"])
                    self.assertEqual(ran[1][:3], ["git", "-C", str(checkout.parent / "other")])

    def test_credential_refuses_a_token_that_is_not_a_secret_name(self):
        defn = definition.load(self.root)
        defn["factory"]["environments"]["engine"]["repositories"] = [{"repo": "o/main", "write": True},
                                                                     {"repo": "o/other", "write": True, "token": "X; rm -rf ~"}]
        with self.assertRaises(SystemExit):
            team_setup.set_credentials(defn, "engine", self.tmp / "main", run=lambda *a, **k: None, environ={})

    def test_roles_in_reads_every_environment_form(self):
        defn = {"factory": {"agents": {"a": {"role": "dev"}, "b": {"role": "ops"}}},
                "workflow": {"settings": {}, "nodes": {"n": {"agent": "a"}, "m": {"agent": "b"}}}}
        w, n = defn["workflow"], defn["workflow"]["nodes"]
        cases = [  # (settings, node n extra, node m extra, roles in engine)
            ({"environments": ["engine", "team"]}, {}, {"environment": "team"}, ["dev"]),
            ({"environment": "engine"}, {}, {}, ["dev", "ops"]),
            ({"environments": ["team"]}, {"environments": ["engine", "team"]}, {}, ["dev"]),
            ({"environment": "team"}, {"environment": "engine"}, {"enabled": False, "environment": "engine"}, ["dev"]),
            ({}, {}, {}, []),
        ]
        for settings, extra_n, extra_m, want in cases:
            with self.subTest(settings=settings, n=extra_n, m=extra_m):
                w["settings"] = settings
                n["n"], n["m"] = {"agent": "a", **extra_n}, {"agent": "b", **extra_m}
                self.assertEqual(team_setup.roles_in(defn, "engine"), want)

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

    def test_setup_exports_node_repo(self):
        calls = []
        team_setup.install(definition.load(self.root), "engine", self.home, lambda spec, tmp: self.src,
                           run=lambda cmd, **kw: calls.append(kw), node_repo="o/web")
        self.assertEqual({kw["env"]["FACTORY_NODE_REPO"] for kw in calls}, {"o/web"})
        calls.clear()
        self.install()   # no node repository: the scripts inherit the environment unchanged
        team_setup.install(definition.load(self.root), "engine", self.home, lambda spec, tmp: self.src,
                           run=lambda cmd, **kw: calls.append(kw))
        self.assertTrue(all("env" not in kw for kw in calls))

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

    def test_mcp_json_is_not_written_without_a_node(self):
        path = self.root / "factory.yaml"
        data = yaml.safe_load(path.read_text())
        data["roles"]["developer"]["mcp"] = {"a": {"command": "x"}}
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        team_setup.install(definition.load(self.root), "engine", self.home, lambda spec, tmp: self.src,
                           run=lambda cmd, **kw: None, checkout=self.tmp)
        self.assertFalse((self.tmp / ".mcp.json").exists())

    def test_roles_in_follows_node_environments(self):
        def defn(settings, node_env):
            node = {"agent": "dev", **node_env}
            return {"factory": {"agents": {"dev": {"role": "developer"}}},
                    "workflow": {"settings": settings, "nodes": {"n": node}}}
        rows = [
            ({"environments": ["engine", "team"]}, {}, "engine", ["developer"]),
            ({"environments": ["engine", "team"]}, {}, "team", ["developer"]),
            ({"environments": ["engine", "team"]}, {}, "other", []),
            ({"environment": "engine"}, {}, "engine", ["developer"]),
            ({"environment": "engine"}, {}, "team", []),
            ({"environments": ["engine"]}, {"environment": "team"}, "engine", []),
            ({"environments": ["engine"]}, {"environment": "team"}, "team", ["developer"]),
            ({"environment": "engine"}, {"environments": ["a", "b"]}, "b", ["developer"]),
            ({}, {}, "engine", []),
        ]
        for settings, node_env, env, want in rows:
            with self.subTest(settings=settings, node=node_env, env=env):
                self.assertEqual(team_setup.roles_in(defn(settings, node_env), env), want)

    def test_refuses_an_invalid_definition(self):
        (self.root / "roles/router/role.md").unlink()
        with self.assertRaises(SystemExit) as e:
            self.install()
        self.assertIn("missing-role-body", str(e.exception))


class RolesInEnvironments(unittest.TestCase):
    def test_roles_follow_node_environments(self):
        agents = {"dev": {"role": "developer"}, "ops": {"role": "ops"}}
        cases = [
            ("no environment, settings.environments lists both", {"environments": ["engine", "team"]}, {}, ["developer"], ["developer"]),
            ("settings.environment default", {"environment": "team"}, {}, [], ["developer"]),
            ("node environment wins", {"environments": ["engine", "team"]}, {"environment": "team"}, [], ["developer"]),
            ("node environments list", {}, {"environments": ["engine"]}, ["developer"], []),
            ("disabled node", {"environments": ["engine", "team"]}, {"enabled": False}, [], []),
            ("no environment at all", {}, {}, [], []),
        ]
        for label, settings, node, engine, team in cases:
            with self.subTest(label):
                defn = {"factory": {"agents": agents},
                        "workflow": {"settings": settings, "nodes": {"implement": {"agent": "dev", **node}}}}
                self.assertEqual(team_setup.roles_in(defn, "engine"), engine)
                self.assertEqual(team_setup.roles_in(defn, "team"), team)


if __name__ == "__main__":
    unittest.main()

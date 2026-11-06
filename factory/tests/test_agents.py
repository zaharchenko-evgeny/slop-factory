#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for agents (ADR-0020): `agents:` in factory.project.yaml and `agent:` on workflow nodes.
Run: uv run factory/tests/test_agents.py"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
import deploy  # noqa: E402
import gate  # noqa: E402
import project  # noqa: E402
import workflow  # noqa: E402


def with_agents():
    """(project, workflow) migrated from engine/nodes.yaml and the team fixture, with a developer and a router agent."""
    nodes = yaml.safe_load((ROOT.parent / config.NODES).read_text())
    descriptor = yaml.safe_load((ROOT / "tests/fixtures/team/factory.descriptor.yaml").read_text())
    proj, wf = workflow.migrate(nodes, descriptor)
    proj["agents"] = {"dev": {"persona": "dev", "role": "developer", "tier": "mid"},
                      "ria": {"persona": "ria", "role": "router", "tier": "cheap"}}
    wf["nodes"]["implement"]["agent"] = "dev"
    wf["nodes"]["intake"]["agent"] = "ria"
    return proj, wf


class AgentsTest(unittest.TestCase):
    def errors(self, proj, wf):
        return workflow.validate(proj, wf)

    def test_declared_agents_validate(self):
        self.assertEqual(self.errors(*with_agents()), [])

    def test_duplicate_persona_rejected(self):
        proj, wf = with_agents()
        proj["agents"]["ria"]["persona"] = "dev"
        self.assertIn("duplicate-persona: dev: agents dev, ria", self.errors(proj, wf))

    def test_role_mismatch_between_node_and_agent_rejected(self):
        proj, wf = with_agents()
        wf["nodes"]["fix"]["agent"] = "ria"
        self.assertIn("agent-role: fix: agent ria is a router, the node a developer", self.errors(proj, wf))

    def test_bad_persona_slug_rejected(self):
        for persona in ("Dev", "1dev", "dev_x", ""):
            proj, wf = with_agents()
            proj["agents"]["dev"]["persona"] = persona
            self.assertTrue(any(e.startswith("bad-persona: agents.dev:") for e in self.errors(proj, wf)), persona)

    def test_node_without_agent_gets_the_implicit_agent_of_its_role(self):
        proj, wf = with_agents()
        agents, errors = project.node_agents(proj, wf["nodes"])
        self.assertEqual(errors, [])
        self.assertEqual(agents["fix"], {**proj["roles"]["developer"], "id": "developer", "persona": "developer",
                                         "role": "developer"})
        self.assertEqual(agents["implement"]["id"], "dev")
        self.assertNotIn("start", agents)  # a script node runs no agent

    def test_unknown_agent_id_rejected(self):
        proj, wf = with_agents()
        wf["nodes"]["fix"]["agent"] = "nobody"
        self.assertIn("unknown-agent: fix: nobody", self.errors(proj, wf))

    def test_agent_on_script_node_rejected(self):
        proj, wf = with_agents()
        wf["nodes"]["start"]["agent"] = "dev"
        self.assertIn("agent-on-script: start: a script node runs no agent", self.errors(proj, wf))

    def test_deploy_rejects_an_unknown_agent(self):
        proj, wf = with_agents()
        wf["nodes"]["fix"]["agent"] = "nobody"
        with self.assertRaisesRegex(ValueError, "unknown-agent: fix: nobody"):
            deploy.agents_of(workflow.to_nodes(proj, wf), config.merge(proj, {}))

    def test_agent_tier_replaces_the_role_tier(self):
        proj, wf = with_agents()
        proj["agents"]["ria"]["tier"] = "mid"
        wf["nodes"]["intake"].pop("tier", None)  # a node's own tier still wins over its agent's
        nodes = workflow.to_nodes(proj, wf)
        agent = deploy.agents_of(nodes, proj)["intake"]
        self.assertEqual(deploy.variants("intake", nodes["nodes"]["intake"], proj["roles"], agent),
                         [("intake", None, "mid", None)])

    def test_gate_spec_nodes_keep_agent_persona_and_mentions(self):
        spec = {"node": "intake", "role": "router", "when": {}, "outcomes": {}, "agent": "ria", "persona": "ria",
                "mentions": ["dev"], "mentions_on": {"dev": ["ticket"]}}
        with tempfile.NamedTemporaryFile("w", suffix=".json") as f:
            f.write(json.dumps(spec))
            f.flush()
            node = gate.spec_nodes(f.name)["nodes"]["intake"]
        self.assertEqual({k: node[k] for k in gate.AGENT_KEYS}, {k: spec[k] for k in gate.AGENT_KEYS})


if __name__ == "__main__":
    unittest.main()

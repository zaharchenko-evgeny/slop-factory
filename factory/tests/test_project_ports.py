#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for the descriptor-backed tracker port (factory/gate_port.py DescriptorPort) against the fixture project.
Run: uv run factory/tests/test_project_ports.py"""
import ast
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import project_mode  # noqa: E402
from factory_plugins.github.gate import GitHubGateTracker  # noqa: E402
from gate_port import MemorySystem, UnsupportedStore  # noqa: E402
from test_youtrack_tracker import FakeYouTrack  # noqa: E402
from factory_plugins.youtrack.store import YouTrackTicketStore  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/project"
YOUTRACK = ROOT / "tests/fixtures/project-youtrack"
ISSUE = json.loads((YOUTRACK / "issue.json").read_text())


def tracker(**ticket):
    return MemorySystem({7: {"stage": "draft", **ticket}}, project=project_mode.legacy_descriptor(FIXTURE))


class ProjectPortsTest(unittest.TestCase):
    def test_label_state_round_trip(self):
        t = tracker()
        self.assertEqual(t.state(7), "draft")
        self.assertTrue(t.set_state(7, "ready", event="e1"))
        self.assertEqual(t.state(7), "ready")
        self.assertFalse(t.set_state(7, "ready"))
        self.assertEqual(t.facts(7)["seen"], ["e1"])

    def test_pr_state_from_pr_facts(self):
        self.assertEqual(tracker(pr=5, pr_state="open").pr_states(7), [])
        approved = tracker(pr=5, pr_state="open", round_complete=True, round_verdict="approve")
        self.assertEqual(approved.pr_states(7), ["pr-approved"])
        self.assertEqual(tracker(pr=5, pr_state="merged").pr_states(7), ["pr-merged"])
        with self.assertRaisesRegex(ValueError, "PR fact"):
            approved.set_state(7, "pr-approved")

    def test_command_round_trip(self):
        t = tracker()
        self.assertTrue(t.add_command(7, "review"))
        self.assertTrue(t.add_command(7, "pr-review"))
        self.assertEqual(sorted(t.commands(7)), ["pr-review", "review"])
        self.assertTrue(t.remove_command(7, "pr-review"))
        self.assertEqual(t.commands(7), ["review"])

    def test_unsupported_stores_raise(self):
        t = tracker()
        with self.assertRaisesRegex(UnsupportedStore, "needs-review: this tracker has no field store"):
            t.set_state(7, "needs-review")
        self.assertTrue(t.add_command(7, "close"))  # #1298: a tag store is a label on a tracker without tags
        with self.assertRaises(KeyError):
            t.add_command(7, "deploy")

    def test_tag_stores_are_labels_on_a_label_tracker(self):  # #1298: GitHub issues beside YouTrack tickets
        descriptor = project_mode.legacy_descriptor(FIXTURE)
        descriptor["states"]["ready"] = {"kind": "active", "store": {"tag": "factory:ready"}}
        descriptor["commands"]["implement-<tier>"] = {"store": {"tag": "factory:implement-<tier>"}}
        t = MemorySystem({7: {"stage": "draft"}}, project=descriptor)
        self.assertTrue(t.set_state(7, "ready"))
        self.assertEqual(t.state(7), "ready")
        self.assertTrue(t.add_command(7, "implement-mid"))
        self.assertEqual(t.commands(7), ["implement-mid"])

    def youtrack(self):
        """A tracker over the fixture project-youtrack, its tickets in a stubbed YouTrack."""
        fake = FakeYouTrack(ISSUE, tags=("factory:implement-mid", "factory:implement-cheap", "risk:low", "close"))
        descriptor = project_mode.legacy_descriptor(YOUTRACK)
        stages = {n: s["store"]["field"]["value"] for n, s in descriptor["states"].items()}
        store = YouTrackTicketStore(fake, state_field="State")
        return GitHubGateTracker("example/project", stages, "factory[bot]", run=lambda *a, **k: "", project=descriptor,
                                 tickets=store), fake

    def test_field_state_round_trip(self):
        t, fake = self.youtrack()
        self.assertEqual(t.state("LME-1701"), "in-dev")
        self.assertTrue(t.set_state("LME-1701", "blocked"))
        self.assertEqual(t.state("LME-1701"), "blocked")
        self.assertFalse(t.set_state("LME-1701", "blocked"))
        self.assertEqual([c for c in fake.calls if c[0] == "POST"][-1][2]["customFields"],
                         [{"name": "State", "$type": "StateIssueCustomField", "value": {"name": "Blocked"}}])

    def test_tag_command_round_trip(self):
        t, fake = self.youtrack()
        self.assertEqual(t.commands("LME-1701"), ["implement-mid"])
        self.assertTrue(t.remove_command("LME-1701", "implement-*"))
        self.assertEqual(t.commands("LME-1701"), [])
        self.assertTrue(t.add_command("LME-1701", "close"))
        self.assertFalse(t.add_command("LME-1701", "close"))
        self.assertEqual(t.commands("LME-1701"), ["close"])
        self.assertEqual(sorted(x["name"] for x in fake.issue["tags"]), ["close", "risk:low"])

    def test_command_templates_fill_their_placeholder(self):
        descriptor = project_mode.legacy_descriptor(FIXTURE)
        descriptor["commands"]["implement-<tier>"] = {"store": {"label": "factory:implement-<tier>"}}
        t = MemorySystem({7: {"stage": "ready"}}, project=descriptor)
        self.assertTrue(t.add_command(7, "implement-mid"))
        self.assertEqual(t.commands(7), ["implement-mid"])
        self.assertTrue(t.remove_command(7, "implement-*"))
        self.assertEqual(t.commands(7), [])

    def test_no_hardcoded_factory_labels_in_engine(self):
        """Engine code names labels through factory/vocab.py; only review and note markers (`<!-- factory:...`) and
        docstrings may spell one."""
        label = re.compile(r"(?<!<!-- )factory:[a-z]")
        found = []
        for path in sorted(ROOT.glob("*.py")):
            tree = ast.parse(path.read_text())
            docs = {id(n.body[0].value) for n in ast.walk(tree)
                    if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
            found += [f"{path.name}:{n.lineno}: {n.value!r}" for n in ast.walk(tree)
                      if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs
                      and label.search(n.value)]
        self.assertEqual(found, [])

    def test_no_project_is_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, "no project descriptor"):
            MemorySystem({7: {"stage": "ready"}}).state(7)



class TicketRepositoriesTest(unittest.TestCase):
    def test_full_names_map_to_repository_names(self):
        from gate_port import ticket_repositories
        names = ["slop-factory", "slop-eval-factory"]
        self.assertEqual(ticket_repositories("repositories: zaharchenko-evgeny/slop-factory", names, "slop-factory"),
                         ["slop-factory"])
        self.assertEqual(ticket_repositories("repositories: slop-factory, o/slop-eval-factory", names, "slop-factory"),
                         ["slop-factory", "slop-eval-factory"])
        self.assertEqual(ticket_repositories("repositories: slop-factory, slop-eval-factory (fixture only)",
                                             names, "slop-factory"),
                         ["slop-factory", "slop-eval-factory"])
        with self.assertRaises(ValueError):
            ticket_repositories("repositories: o/other", names, "slop-factory")


if __name__ == "__main__":
    unittest.main()

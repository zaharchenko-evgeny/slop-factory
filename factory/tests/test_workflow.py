#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/workflow.py. Run: uv run factory/tests/test_workflow.py"""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
import nodes as nodes_mod  # noqa: E402
import project  # noqa: E402
import workflow  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/project"


def this_repository():
    nodes = yaml.safe_load((REPO / "workflows/nodes.yaml").read_text())
    descriptor = yaml.safe_load((REPO / "factory.descriptor.yaml").read_text())
    return nodes, descriptor


class WorkflowTest(unittest.TestCase):
    def migrated(self):
        return workflow.migrate(*this_repository())

    def test_fixture_validates(self):
        descriptor = project.load(FIXTURE)
        self.assertEqual(workflow.validate(descriptor, workflow.load(FIXTURE, descriptor)), [])

    def test_unknown_state_rejected(self):
        proj, wf = self.migrated()
        wf["nodes"]["intake"]["when"]["state"] = "nope"
        wf["nodes"]["start"]["outcomes"]["started"]["effects"][-1]["state"] = "gone"
        errors = workflow.validate(proj, wf)
        self.assertIn("unknown-state: intake.when: nope", errors)
        self.assertIn("unknown-state: start.started: gone", errors)

    def test_unknown_command_and_effect_rejected(self):
        proj, wf = self.migrated()
        wf["nodes"]["implement"]["wakes_on"] = ["command deploy-<tier>"]
        wf["nodes"]["implement"]["outcomes"]["done"]["effects"].append({"label": "factory:x"})
        errors = workflow.validate(proj, wf)
        self.assertIn("unknown-command: implement.wakes_on: deploy-<tier>", errors)
        self.assertTrue(any(e.startswith("unknown-effect: implement.done") for e in errors), errors)

    def test_uncovered_active_state_rejected(self):
        proj, wf = self.migrated()
        proj["states"]["parked"] = {"kind": "active", "store": {"label": "factory:parked"}}
        proj["states"]["shelved"] = {"kind": "waiting", "store": {"label": "factory:shelved"}}
        errors = workflow.validate(proj, wf)
        self.assertTrue(any(e.startswith("uncovered-state: parked") for e in errors), errors)
        self.assertFalse(any("shelved" in e for e in errors), errors)

    def test_migrate_round_trips_this_repository(self):
        nodes, descriptor = this_repository()
        proj, wf = workflow.migrate(nodes, descriptor)
        proj, wf = yaml.safe_load(workflow.dump(proj)), yaml.safe_load(workflow.dump(wf))
        self.assertEqual(workflow.validate(proj, wf), [])
        self.assertEqual(workflow.to_nodes(proj, wf), nodes)
        for wake in (w for n in wf["nodes"].values() for w in n["wakes_on"]):
            self.assertNotIn(":", wake, "a v2 wake names no label")
        self.assertEqual(proj["states"]["ci-green"], {"kind": "active", "store": {"pr": "ci-green"}})
        self.assertEqual(proj["states"]["pr-merged"], {"kind": "final", "store": {"pr": "merged"}})
        self.assertEqual(proj["commands"]["implement-<tier>"], {"store": {"label": "factory:implement-<tier>"}})
        self.assertEqual(proj["commands"]["fix-<tier>"], {"store": {"pr_label": "factory:fix-<tier>"}})
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / config.PROJECT_DESCRIPTOR).write_text(workflow.dump(proj))
            self.assertEqual(project.validate(tmp), [])
        legacy, current = project.legacy_config(proj)["tracker"], config.load()["tracker"]
        self.assertEqual(legacy["stages"], current["stages"])
        self.assertEqual(legacy["stage_kinds"], current["stage_kinds"])

    def test_migrate_command_writes_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with contextlib.redirect_stdout(io.StringIO()):
                code = workflow.main(["migrate", str(REPO / "workflows/nodes.yaml"),
                                      str(REPO / "factory.descriptor.yaml"), "--out", tmp])
            self.assertEqual(code, 0)
            proj = yaml.safe_load((Path(tmp) / config.PROJECT_DESCRIPTOR).read_text())
            self.assertEqual(proj["workflow"], workflow.WORKFLOW_PATH)
            self.assertEqual(yaml.safe_load((Path(tmp) / workflow.WORKFLOW_PATH).read_text())["format"], 2)

    def test_nodes_load_renders_project_workflow(self):
        config.set_team(FIXTURE)
        try:
            rendered = nodes_mod.load()
        finally:
            config.set_team(None)
        self.assertEqual(rendered["nodes"]["review"]["triggers"],
                         ["issues.labeled factory:review", "pull_request.labeled factory:pr-review"])
        self.assertEqual(rendered["nodes"]["start"]["outcomes"]["started"]["effects"],
                         [{"label": "factory:review"}, {"stage": "needs-review"}])
        self.assertEqual(rendered["nodes"]["merge"]["when"], {"stage": "pr-approved"})


if __name__ == "__main__":
    unittest.main()

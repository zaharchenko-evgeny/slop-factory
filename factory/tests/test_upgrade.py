#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/upgrade.py and the engine's engine/template-v1/. Run: uv run factory/tests/test_upgrade.py"""
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
import upgrade  # noqa: E402
import workflow  # noqa: E402

TEMPLATE = REPO / "engine/template-v1"


def run(argv, team):
    config.set_team(team)
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            code = upgrade.main(argv)
    finally:
        config.set_team(None)
    return code, out.getvalue()


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.old, self.project = self.tmp / "old", self.tmp / "project"
        shutil.copytree(TEMPLATE, self.old)
        (self.old / workflow.WORKFLOW_PATH).write_text("nodes: {}\n")  # an older engine's template
        (self.old / "gone.md").write_text("removed since\n")
        shutil.copytree(self.old, self.project)
        (self.project / "knowledge/README.md").unlink()  # a template path the project does not have

    def test_diff_lists_changed_template_files(self):
        code, out = run(["--diff", "--from", str(self.old)], self.project)
        self.assertEqual(code, 0)
        self.assertIn(f"+++ b/{workflow.WORKFLOW_PATH}", out)
        self.assertIn("-nodes: {}", out)
        self.assertIn("--- a/gone.md", out)
        self.assertNotIn("knowledge/README.md", out)
        self.assertNotIn("b/README.md", out)
        self.assertNotIn("# edited:", out)

    def test_edited_file_not_overwritten_without_apply(self):
        edited = (self.project / workflow.WORKFLOW_PATH)
        edited.write_text("nodes: {mine: {}}\n")
        code, out = run(["--diff", "--from", str(self.old)], self.project)
        self.assertEqual(code, 0)
        self.assertIn(f"# edited: {workflow.WORKFLOW_PATH} differs from the template", out)
        self.assertEqual(edited.read_text(), "nodes: {mine: {}}\n")
        code, _ = run(["--apply", workflow.WORKFLOW_PATH], self.project)
        self.assertEqual(code, 0)
        self.assertEqual(edited.read_text(), (TEMPLATE / workflow.WORKFLOW_PATH).read_text())
        self.assertEqual(run(["--apply", "nope.md"], self.project)[0], 1)

    def test_diff_needs_an_old_engine(self):
        descriptor = self.project / config.PROJECT_DESCRIPTOR
        descriptor.write_text(descriptor.read_text().replace('engine: "{{ENGINE_REF}}"', 'engine: "no-such-ref"'))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(run(["--diff"], self.project)[0], 2)
        self.assertIn("no template at 'no-such-ref'", err.getvalue())

    def test_template_matches_migrate(self):
        """The generator check of engine/template-v1/: its generated parts equal a fresh `--generate-template`."""
        nodes = yaml.safe_load((REPO / config.NODES).read_text())
        descriptor, delivery = upgrade.generated((TEMPLATE / config.PROJECT_DESCRIPTOR).read_text(), nodes)
        self.assertEqual((TEMPLATE / config.PROJECT_DESCRIPTOR).read_text(), descriptor)
        self.assertEqual((TEMPLATE / workflow.WORKFLOW_PATH).read_text(), delivery)
        project = yaml.safe_load(descriptor)
        self.assertEqual(workflow.validate(project, yaml.safe_load(delivery)), [])
        self.assertEqual(workflow.to_nodes(project, yaml.safe_load(delivery)), nodes)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Templates reproduce this repository's files. Run: uv run skills/factory-setup/tests/test_templates.py"""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
ROOT = SKILL.parent.parent
sys.path.insert(0, str(SKILL))
sys.path.insert(0, str(ROOT / "factory"))
import render  # noqa: E402

ANSWERS = json.loads((HERE / "this-repo.answers.json").read_text())
OUT = render.render(ANSWERS)
# Values that identify this project; none may appear in a template.
PROJECT = ["offline-eval", "zaharchenko", "Offline Evaluation", "hetzner", "Qwen", "claude-", "ez/"]


class Templates(unittest.TestCase):
    def test_reproduces_current_files(self):
        for rel, text in OUT.items():
            if rel == "AGENTS.workflow.md":
                cur = (ROOT / "AGENTS.md").read_text()
                want = cur[cur.index("## Development workflow"):cur.index("## Tracker and git")]
            elif rel == "workflows/delivery.sm.yaml":
                continue  # this setup template still serves projects that retain the local engine
            else:
                want = (ROOT / rel).read_text()
            self.assertEqual(text.rstrip("\n"), want.rstrip("\n"), rel)

    def test_templates_name_no_project_value(self):
        for f in render.T.rglob("*"):
            if f.is_file():
                for word in PROJECT:
                    self.assertNotIn(word, f.read_text(), f"{f.name}: {word}")

    def test_placeholders_documented(self):
        doc = (SKILL / "SKILL.md").read_text()
        used = {m for f in render.T.rglob("*") if f.is_file() for m in render.PH.findall(f.read_text())}
        for name in used:
            self.assertIn(f"`{name}`", doc, name)
        self.assertEqual(used - {"ROLE", "TOOLS"}, set(ANSWERS) - {"ROLES"})

    def test_project_render_validates(self):
        import project
        import workflow
        files = {f for f in render.PROJECT_T.rglob("*") if f.is_file()}
        names = {m for f in files for m in render.PH.findall(f.read_text())}
        doc = (SKILL / "SKILL.md").read_text()
        for name in names:
            self.assertIn(f"`{name}`", doc, name)
        answers = {name: f"example-{name.lower()}" for name in names}
        out = render.render_project(answers)
        self.assertIn("workflows/delivery.yaml", out)
        self.assertFalse([rel for rel, text in out.items() if render.PH.search(text)])
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in out.items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(text)
            self.assertEqual(project.validate(tmp), [])
            descriptor = project.load(tmp)
            self.assertEqual(descriptor["tracker"]["repository"], "example-tracker_repository")
            self.assertEqual(workflow.validate(descriptor, workflow.load(tmp, descriptor)), [])
            self.assertNotIn("roles", {Path(rel).parts[0] for rel in out})


if __name__ == "__main__":
    unittest.main()

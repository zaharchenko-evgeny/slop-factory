#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""The setup skill's templates. Run: uv run skills/factory-setup/tests/test_templates.py"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
ROOT = SKILL.parent.parent
sys.path.insert(0, str(SKILL))
sys.path.insert(0, str(ROOT / "factory"))
import definition  # noqa: E402
import render  # noqa: E402

ANSWERS = json.loads((HERE / "this-repo.answers.json").read_text())
# Values that identify this project; none may appear in a template.
PROJECT = ["offline-eval", "zaharchenko", "Offline Evaluation", "hetzner", "Qwen", "claude-", "ez/", "slop", "Slop"]


def files(root):
    return [f for f in root.rglob("*") if f.is_file() and "__pycache__" not in f.parts]


class Templates(unittest.TestCase):
    def test_agents_section_reproduces_this_repository(self):
        text = render.render(ANSWERS)["AGENTS.workflow.md"]
        cur = (ROOT / "AGENTS.md").read_text()
        want = cur[cur.index("## Development workflow"):cur.index("## Tracker and git")]
        self.assertEqual(text.rstrip("\n"), want.rstrip("\n"))

    def test_templates_name_no_project_value(self):
        for f in [*files(render.T), *files(render.PROJECT_T)]:
            for word in PROJECT:
                self.assertNotIn(word, f.read_text(), f"{f.relative_to(SKILL)}: {word}")

    def test_placeholders_documented_and_answered(self):
        doc = (SKILL / "SKILL.md").read_text()
        used = {m for f in [*files(render.T), *files(render.PROJECT_T)] for m in render.PH.findall(f.read_text())}
        for name in used:
            self.assertIn(f"`{name}`", doc, name)
        self.assertEqual(used, set(ANSWERS))

    def test_rendered_team_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in render.render_project(ANSWERS).items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(text)
            defn = definition.load(tmp)
            self.assertEqual(definition.errors(defn), [])
            self.assertEqual(definition.warnings(defn), [])
            self.assertEqual(defn["factory"]["tracker"]["repository"], "zaharchenko-evgeny/slop-factory")

    def test_cli_writes_a_valid_team(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, str(SKILL / "render.py"), str(HERE / "this-repo.answers.json"), tmp], check=True)
            self.assertTrue(os.access(Path(tmp) / ".air/cloud/startup.sh", os.X_OK))
            run = subprocess.run(["uv", "run", "--quiet", str(ROOT / "factory/cli.py"), "validate"], capture_output=True,
                                 text=True, env={**os.environ, "FACTORY_TEAM": tmp})
            self.assertEqual(run.stdout.strip(), "factory: ok", run.stderr)


if __name__ == "__main__":
    unittest.main()

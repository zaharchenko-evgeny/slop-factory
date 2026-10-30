#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Templates reproduce this repository's files. Run: uv run skills/factory-setup/tests/test_templates.py"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
ROOT = SKILL.parent.parent
sys.path.insert(0, str(SKILL))
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
            else:
                want = (ROOT / rel.replace("delivery.sm", "delivery.v1.sm")).read_text()  # the setup skill still renders the v1 machine
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

    def test_rendered_machine_validates(self):
        with tempfile.TemporaryDirectory() as d:
            for rel in ("factory.yaml", "workflows/delivery.sm.yaml"):
                (Path(d) / Path(rel).name).write_text(OUT[rel])
            r = subprocess.run(["uv", "run", "--quiet", str(ROOT / "factory/sm_v1.py"), "--sm", f"{d}/delivery.sm.yaml",
                                "--config", f"{d}/factory.yaml", "validate"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()

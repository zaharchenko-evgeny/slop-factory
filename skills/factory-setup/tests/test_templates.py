#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""The setup skill's templates. Run: uv run skills/factory-setup/tests/test_templates.py"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
ROOT = SKILL.parent.parent
sys.path.insert(0, str(SKILL))
sys.path.insert(0, str(ROOT / "factory"))
import definition  # noqa: E402
import render  # noqa: E402
import statechart  # noqa: E402

ANSWERS = json.loads((HERE / "this-repo.answers.json").read_text())
# Values that identify this project; none may appear in a template.
PROJECT = ["offline-eval", "zaharchenko", "Offline Evaluation", "hetzner", "Qwen", "claude-", "ez/", "slop", "Slop"]


# A workflow whose `review` stage has no node that moves out of it.
STUCK = """
stages: { final: [done], waiting: [blocked] }
nodes:
  intake: { agent: router, triggers: [{ stage: triage }], when: { stage: triage },
            outcomes: { ready: { effects: [{ stage: review }] } } }
  finish: { agent: clerk, triggers: [{ pr: merged }], outcomes: { done: { if: { stage: approved }, effects: [{ stage: done }] } } }
"""


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

    def test_workflow_has_ci_red_and_walkthrough(self):
        nodes = yaml.safe_load((render.PROJECT_T / "workflow.yaml").read_text())["nodes"]
        self.assertIn("ci-red", nodes)
        self.assertIn("walkthrough", nodes)
        self.assertTrue((render.PROJECT_T / "roles/walkthrough/role.md").is_file())

    def test_every_node_is_documented(self):
        doc = (SKILL / "references/nodes.md").read_text()
        for name in yaml.safe_load((render.PROJECT_T / "workflow.yaml").read_text())["nodes"]:
            self.assertIn(f"`{name}`", doc, name)

    def test_setup_scripts_parse(self):
        for script in render.PROJECT_T.glob("roles/*/setup.sh"):
            subprocess.run(["bash", "-n", str(script)], check=True)

    def test_rendered_team_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in render.render_project(ANSWERS).items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(text)
            defn = definition.load(tmp)
            self.assertEqual(definition.errors(defn), [])
            self.assertEqual(definition.warnings(defn), [])
            self.assertEqual(defn["factory"]["tracker"]["repository"], "zaharchenko-evgeny/slop-factory")

    def test_statechart_skill_is_pinned_and_vendored(self):
        spec = importlib.util.spec_from_file_location("vendor_skills", ROOT / "scripts/vendor-skills.py")
        vs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vs)
        pin = vs.load()["statechart"]
        self.assertEqual((pin["source"], pin["commit"], pin["path"]),
                         ("cursor/plugins", "519d36aa9895", "pstack/skills/statechart"))
        self.assertEqual(vs.check(), [])  # the vendored folder matches the pin's sha256

    def test_rendered_template_breaks_no_statechart_rule(self):
        workflow = yaml.safe_load(render.render_project(ANSWERS)["workflow.yaml"])
        self.assertEqual(statechart.check(workflow)[0], [])

    def test_stage_without_way_out_breaks_a_rule(self):
        broken, _ = statechart.check(yaml.safe_load(STUCK))
        self.assertIn("NoDeadlock", broken)
        self.assertIn("MovingNode", broken)
        self.assertIn("DoneReachable", broken)

    def test_undeclared_stage_breaks_one_stage_label(self):
        workflow = yaml.safe_load(STUCK.replace("stage: review", "stage: reveiw"))
        self.assertIn("OneStageLabel", statechart.check(workflow)[0])

    def test_render_refuses_a_broken_workflow(self):
        files = {"workflow.yaml": STUCK, "README.md": "# x\n"}
        with self.assertRaisesRegex(SystemExit, "NoDeadlock"):
            render.with_chart(files)

    def test_readme_has_the_state_chart(self):
        readme = render.with_chart(render.render_project(ANSWERS))["README.md"]
        self.assertRegex(readme, r"```mermaid\n(?:.*\n)*?stateDiagram")

    def test_cli_writes_a_valid_team(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([sys.executable, str(SKILL / "render.py"), str(HERE / "this-repo.answers.json"), tmp], check=True)
            self.assertTrue(os.access(Path(tmp) / ".air/cloud/startup.sh", os.X_OK))
            self.assertIn("stateDiagram", (Path(tmp) / "README.md").read_text())
            run = subprocess.run(["uv", "run", "--quiet", str(ROOT / "factory/cli.py"), "validate"], capture_output=True,
                                 text=True, env={**os.environ, "FACTORY_TEAM": tmp})
            self.assertEqual(run.stdout.strip(), "factory: ok", run.stderr)


if __name__ == "__main__":
    unittest.main()

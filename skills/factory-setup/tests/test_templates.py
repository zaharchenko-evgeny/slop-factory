#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""The setup skill's templates. Run: uv run skills/factory-setup/tests/test_templates.py"""
import hashlib
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
THREE = json.loads((HERE / "three-repos.answers.json").read_text())
# Answers that no template placeholder names: `render.py` reads them to derive blocks and options.
OPTIONAL = {"CODE_REPOSITORIES", "CI_WORKFLOW", "TRACKER_REPOSITORY", "TRACKER", "BLOCKERS", "ISSUES_TOKEN_ENV"}
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

ORPHAN = """
stages: { final: [done] }
nodes:
  intake: { agent: router, triggers: [{ stage: triage }], when: { stage: triage },
            outcomes: { ready: { effects: [{ stage: done }] } } }
  idle: { agent: clerk, triggers: [{ stage: orphan }], when: { stage: orphan } }
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
        derived = {"ENGINE_SKILL_SOURCE", *render.derive(ANSWERS)}  # render.py derives these from the answers
        for name in used - derived | OPTIONAL:
            self.assertIn(f"`{name}`", doc, name)
        self.assertEqual(used - derived, set(ANSWERS) - OPTIONAL)

    def test_workflow_has_ci_red_and_no_walkthrough_node(self):
        nodes = yaml.safe_load((render.PROJECT_T / "workflow.yaml").read_text())["nodes"]
        self.assertIn("ci-red", nodes)
        self.assertNotIn("walkthrough", nodes)
        self.assertFalse((render.PROJECT_T / "roles" / "walkthrough").exists())
        factory = yaml.safe_load(render.render_project(ANSWERS)["factory.yaml"])
        self.assertIn("walkthrough", factory["roles"]["reviewer"]["skills"])
        self.assertNotIn("walkthrough", factory["roles"])
        self.assertNotIn("walkthrough", factory["agents"])

    def test_reviewer_role_orders_the_review_sections(self):
        role = (render.PROJECT_T / "roles/reviewer/role.md").read_text()
        at = [role.index(h) for h in ("## Decision for you", "## Summary", "## Walkthrough", "## Merge danger")]
        self.assertEqual(at, sorted(at))

    def test_every_node_is_documented(self):
        doc = (SKILL / "references/nodes.md").read_text()
        for name in yaml.safe_load((render.PROJECT_T / "workflow.yaml").read_text())["nodes"]:
            self.assertIn(f"`{name}`", doc, name)

    def test_setup_scripts_parse(self):
        for script in render.PROJECT_T.glob("roles/*/setup.sh"):
            subprocess.run(["bash", "-n", str(script)], check=True)

    def test_rendered_factory_has_the_workflow_check(self):
        self.assertIn(".github/workflows/factory-workflow-check.yml", render.render_project(ANSWERS))

    def test_rendered_team_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in render.render_project(ANSWERS).items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(text)
            defn = definition.load(tmp)
            self.assertEqual(definition.errors(defn), [])
            self.assertEqual(definition.warnings(defn), [])
            self.assertEqual(defn["factory"]["tracker"]["repository"], "zaharchenko-evgeny/slop-factory")
            pin = defn["factory"]["skills"]["factory-deploy"]
            self.assertEqual(pin["source"], "github:zaharchenko-evgeny/slop-factory@main")
            self.assertEqual(pin["path"], ".agents/skills/factory-deploy")
            self.assertEqual(pin["sha256"], "bd997445b6b34390926afd9cb2e4af665b773c46233a2ec5e5ec08ed2e6502bb")
            run = subprocess.run(["factory", "validate", "--team", tmp], capture_output=True, text=True)
            self.assertEqual(run.stdout.strip(), "factory: ok", run.stderr)

    def test_one_repository_renders_the_same_files_as_before(self):
        """`this-repo.render.sha256.json` holds the sha256 of each file as the single-repository template wrote it."""
        want = json.loads((HERE / "this-repo.render.sha256.json").read_text())
        got = {rel: hashlib.sha256(text.encode()).hexdigest() for rel, text in render.render_project(ANSWERS).items()}
        self.assertEqual(got, want)

    def test_three_repositories_with_youtrack_render_three_code_environments_and_the_hub(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in render.render_project(THREE).items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(text)
            defn = definition.load(tmp)
            self.assertEqual(definition.errors(defn), [])  # no `per-repo-env`, no `deaf-trigger`
            factory, workflow = defn["factory"], defn["workflow"]
            envs = factory["environments"]
            self.assertEqual(list(envs), ["api", "web", "docs", "hub", "knowledge", "team"])
            main = {name: next(r["repo"] for r in e["repositories"] if r.get("main")) for name, e in envs.items()}
            self.assertEqual(main["api"], "acme/api")
            self.assertEqual(main["web"], "acme/web")
            self.assertEqual(main["docs"], "acme/docs")
            read_only = {name: [r["repo"] for r in envs[name]["repositories"] if not r.get("main")] for name in ("api", "web", "docs")}
            self.assertEqual(read_only, {"api": ["acme/web", "acme/factory-team"], "web": ["acme/api", "acme/factory-team"],
                                         "docs": ["acme/factory-team"]})
            self.assertEqual(envs["web"]["repositories"][0]["default_branch"], "develop")
            self.assertEqual(definition.per_repo_envs(defn), {"acme/api": "api", "acme/web": "web", "acme/docs": "docs"})
            self.assertEqual(workflow["settings"]["environment"], "hub")
            self.assertEqual(workflow["blockers"], "merge")
            self.assertEqual(factory["tracker"], {"adapter": "youtrack", "url": "https://acme.youtrack.cloud", "project": "ACME",
                                             "repository_field": "Repository"})
            self.assertEqual(factory["defaults"]["settings"]["issues_token_env"], "FACTORY_ISSUES_TOKEN")
            self.assertIn("FACTORY_ISSUES_TOKEN", factory["defaults"]["env"])
            readme = (Path(tmp) / "README.md").read_text()
            self.assertIn("## Repositories", readme)
            for name in ("api", "web", "docs"):
                self.assertIn(f"automation `<node> {name}`", readme)

    def test_github_issues_in_several_repositories_validate(self):
        answers = {**THREE, "TRACKER": {"adapter": "github-issues"}, "TRACKER_REPOSITORY": "acme/api"}
        files = render.render_project(answers)
        self.assertIn("  adapter: github-issues\n  repository: acme/api\n", files["factory.yaml"])
        self.assertEqual(yaml.safe_load(files["factory.yaml"])["environments"]["hub"]["repositories"][0]["repo"], "acme/api")
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in files.items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(text)
            self.assertEqual(definition.errors(definition.load(tmp)), [])
            run = subprocess.run(["factory", "validate", "--team", tmp], capture_output=True, text=True)
            self.assertEqual(run.stdout.strip(), "factory: ok", run.stderr)

    def test_code_repository_answers_that_cannot_render_are_refused(self):
        repo = {"name": "api", "repo": "acme/api"}
        for bad, why in (([], "at least one"), ([repo, {**repo, "repo": "acme/other"}], "own name"),
                         ([repo, {"name": "web", "repo": "acme/api"}], "own name"),
                         ([{**repo, "reads": ["nope"]}], "unknown repository")):
            with self.assertRaisesRegex(ValueError, why):
                render.render_project({**ANSWERS, "CODE_REPOSITORIES": bad})

    def test_readme_documents_factory_deploy(self):
        readme = render.render_project(ANSWERS)["README.md"]
        self.assertIn("## Deploy", readme)
        self.assertIn("factory-deploy", readme)

    def test_statechart_skill_is_pinned_and_fetched_not_vendored(self):
        spec = importlib.util.spec_from_file_location("vendor_skills", ROOT / "scripts/vendor-skills.py")
        vs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(vs)
        pin = vs.load()["statechart"]
        self.assertEqual((pin["source"], pin["commit"], pin["path"]),
                         ("cursor/plugins", "519d36aa9895", "pstack/skills/statechart"))
        self.assertEqual(vs.check(), [])
        self.assertFalse((ROOT / "engine/template-v1/skills/statechart").exists())
        with tempfile.TemporaryDirectory() as tmp:  # the fetched folder matches the pin's sha256
            self.assertTrue((statechart.skill_dir(tmp) / "SKILL.md").is_file())

    def test_statechart_step_stops_when_the_pin_cannot_be_fetched(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url./nonexistent/.insteadOf", "GIT_CONFIG_VALUE_0": "https://github.com/"}):
            with self.assertRaisesRegex(RuntimeError, "cannot fetch skill statechart at its pin"):
                statechart.skill_dir(tmp)

    def test_rendered_template_breaks_no_statechart_rule(self):
        workflow = yaml.safe_load(render.render_project(ANSWERS)["workflow.yaml"])
        self.assertEqual(statechart.check(workflow)[0], [])

    def test_stage_without_way_out_breaks_a_rule(self):
        broken, _ = statechart.check(yaml.safe_load(STUCK))
        self.assertIn("NoDeadlock", broken)
        self.assertIn("MovingNode", broken)
        self.assertIn("DoneReachable", broken)

    def test_unreachable_active_stage_without_way_out_breaks_moving_node(self):
        self.assertEqual(statechart.check(yaml.safe_load(ORPHAN))[0], ["MovingNode"])

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

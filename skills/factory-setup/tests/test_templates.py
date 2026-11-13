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

ORPHAN = """
stages: { final: [done] }
nodes:
  intake: { agent: router, triggers: [{ stage: triage }], when: { stage: triage },
            outcomes: { ready: { effects: [{ stage: done }] } } }
  idle: { agent: clerk, triggers: [{ stage: orphan }], when: { stage: orphan } }
"""


def files(root):
    return [f for f in root.rglob("*") if f.is_file() and "__pycache__" not in f.parts]



def _ok(stdout):
    """The validate output less the ADR-0027 `deprecated-key` warnings: the template keeps `writes` and `access`
    until deploy reads the new model (#1134)."""
    return "\n".join(line for line in stdout.strip().splitlines() if not line.startswith("warning: deprecated-key:"))


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
        used.discard("ENGINE_SKILL_SOURCE")  # render.py derives this from ENGINE
        used.discard("CODE_HOST")  # derived from CODE_REPOSITORIES and the optional YouTrack answers
        used.discard("TRACKER")
        for name in used:
            self.assertIn(f"`{name}`", doc, name)
        self.assertEqual(used, set(ANSWERS))

    def test_workflow_has_ci_failed_and_no_walkthrough_node(self):
        nodes = yaml.safe_load((render.PROJECT_T / "workflow.yaml").read_text())["nodes"]
        self.assertIn("ci-failed", nodes)
        self.assertNotIn("ci-red", nodes)
        self.assertEqual(nodes["ci-failed"]["when"], {"stage": "approved"})
        self.assertNotIn("ci", nodes["review-low"]["when"])  # a review never waits for CI
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
        for script in [*render.PROJECT_T.glob("roles/*/setup.sh"), render.PROJECT_T / "environments/setup.sh"]:
            subprocess.run(["bash", "-n", str(script)], check=True)

    def test_team_environments_install_the_workflow_check_runtimes(self):
        files = render.render_project(ANSWERS)
        envs = yaml.safe_load(files["factory.yaml"])["environments"]
        for name, env in envs.items():
            if any(r["repo"] == ANSWERS["TEAM_REPOSITORY"] for r in env["repositories"]):
                self.assertEqual(env.get("setup"), "environments/setup.sh", name)
        script = files["environments/setup.sh"]
        self.assertIn("nodejs-wheel==22.", script)  # the statechart skill needs Node 22.18+
        self.assertIn("temurin", script.lower())  # and Java 11+ for TLC

    def test_rendered_factory_has_the_workflow_check(self):
        self.assertIn(".github/workflows/factory-workflow-check.yml", render.render_project(ANSWERS))

    def test_rendered_team_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in render.render_project(ANSWERS).items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(text)
            defn = definition.load(tmp)
            self.assertEqual(definition.errors(defn), [])
            self.assertEqual([w for w in definition.warnings(defn) if not w.startswith("deprecated-key:")], [])  # until #1134
            self.assertEqual(defn["factory"]["tracker"]["repository"], "zaharchenko-evgeny/slop-factory")
            pin = defn["factory"]["skills"]["factory-deploy"]
            self.assertEqual(pin["source"], "github:zaharchenko-evgeny/slop-factory@main")
            self.assertEqual(pin["path"], ".agents/skills/factory-deploy")
            self.assertEqual(pin["sha256"], "bd997445b6b34390926afd9cb2e4af665b773c46233a2ec5e5ec08ed2e6502bb")
            run = subprocess.run(["factory", "validate", "--team", tmp], capture_output=True, text=True)
            self.assertEqual(_ok(run.stdout), "factory: ok", run.stderr)

    def validate(self, answers, cli=True):
        """The loaded definition of a team rendered from `answers`; its definition errors are empty and, with `cli`,
        `factory validate` passes."""
        with tempfile.TemporaryDirectory() as tmp:
            for rel, text in render.render_project(answers).items():
                (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
                (Path(tmp) / rel).write_text(text)
            defn = definition.load(tmp)
            self.assertEqual(definition.errors(defn), [])
            if cli:
                run = subprocess.run(["uv", "run", "--quiet", str(ROOT / "factory/cli.py"), "validate"], capture_output=True,
                                     text=True, env={**os.environ, "FACTORY_TEAM": tmp})
                self.assertEqual(_ok(run.stdout), "factory: ok", run.stdout + run.stderr)
            return defn

    def test_two_repository_answers_render_a_valid_definition(self):
        defn = self.validate({**ANSWERS, "CODE_REPOSITORIES": ["o/api", "o/web"]})
        factory, workflow = defn["factory"], defn["workflow"]
        self.assertEqual(factory["tracker"]["repository"], "o/api")
        self.assertEqual(factory["tracker"]["repositories"], ["o/api", "o/web"])
        for env in factory["environments"].values():
            self.assertFalse(any(r.get("main") for r in env["repositories"]), env)
        self.assertEqual([r["repo"] for r in factory["environments"]["main"]["repositories"]], ["o/api", "o/web"])
        nodes = workflow["nodes"]
        for name in ("implement-small", "implement-medium", "implement-large", "fix", "update"):
            self.assertEqual(nodes[name]["writes"], "all", name)
            self.assertNotIn("listens", nodes[name], name)  # the default: tickets
        self.assertEqual(nodes["retro"]["writes"], [ANSWERS["TEAM_REPOSITORY"]])
        for name in ("intake", "dispatch", "review-low", "review-high", "automerge", "merge", "finish", "notify",
                     "knowledge-review", "reconcile", "doctor-main", "doctor-team"):
            self.assertNotIn("writes", nodes[name], name)

    def test_one_repository_answers_render_the_single_tracker(self):
        defn = self.validate(ANSWERS)
        self.assertEqual(defn["factory"]["tracker"], {"label_prefix": "factory:", "adapter": "github-issues",
                                                  "repository": "zaharchenko-evgeny/slop-factory"})
        self.assertEqual([r["repo"] for r in defn["factory"]["environments"]["main"]["repositories"]],
                         ["zaharchenko-evgeny/slop-factory"])

    def test_youtrack_projects_render_tracker_projects(self):
        projects = {"API": "o/api", "WEB": "o/web"}
        # `factory validate` also wants YouTrack state stores, which the setup interview adds after rendering
        defn = self.validate({**ANSWERS, "CODE_REPOSITORIES": ["o/api", "o/web"], "TRACKER_URL": "https://example.youtrack.cloud",
                              "TRACKER_PROJECTS": projects}, cli=False)
        self.assertEqual(defn["factory"]["tracker"], {"label_prefix": "factory:", "adapter": "youtrack",
                                                      "url": "https://example.youtrack.cloud", "project": "API", "projects": projects})
        self.assertEqual(defn["factory"]["code_host"], {"adapter": "github", "repository": "o/api"})

    def test_youtrack_project_of_an_unknown_repository_is_refused(self):
        with self.assertRaisesRegex(ValueError, "o/other"):
            render.render_project({**ANSWERS, "TRACKER_URL": "https://y", "TRACKER_PROJECTS": {"API": "o/other"}})

    def test_no_code_repository_is_refused(self):
        with self.assertRaisesRegex(ValueError, "CODE_REPOSITORIES"):
            render.render_project({**ANSWERS, "CODE_REPOSITORIES": []})

    def test_list_placeholder_repeats_its_line(self):
        for items, want in ((["a"], "- repo: a\nend"), (["a", "b"], "- repo: a\n- repo: b\nend"), ([], "end"),
                            ("a", "- repo: a\nend")):
            self.assertEqual(render.fill("- repo: {{X}}\nend", {"X": items}), want, items)
        self.assertEqual(render.fill("  {{X}}\n  {{Y}}", {"X": ["a", "b"], "Y": "c"}), "  a\n  b\n  c")

    def test_readme_lists_each_repository_with_its_automations(self):
        files = render.render_project({**ANSWERS, "CODE_REPOSITORIES": ["o/api", "o/web"]})
        readme = files["README.md"]
        section = readme[readme.index("## Repositories"):]
        by_repo = {line.split("`")[1]: line for line in section.splitlines() if line.startswith("- `")}
        self.assertEqual(list(by_repo), ["o/api", "o/web", ANSWERS["TEAM_REPOSITORY"]])
        self.assertIn("`implement-medium api`", by_repo["o/api"])
        self.assertIn("`implement-medium web`", by_repo["o/web"])
        self.assertNotIn("api`", by_repo["o/web"].replace("o/web", ""))
        self.assertIn("`reconcile`", by_repo["o/api"])  # a scheduled node runs in the tracker repository
        self.assertEqual(by_repo[ANSWERS["TEAM_REPOSITORY"]].split(": ")[1], "`retro`")
        single = render.render_project(ANSWERS)["README.md"]
        self.assertIn("`implement-medium`", single)
        self.assertNotIn("implement-medium slop", single)

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
            self.assertEqual(_ok(run.stdout), "factory: ok", run.stderr)



class Download(unittest.TestCase):
    def fake(self, answers):
        calls = []

        class Body:
            def read(self):
                return b"jar"

        def urlopen(url, timeout):
            calls.append(url)
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return Body()
        return calls, urlopen

    def http(self, code):
        import urllib.error
        return urllib.error.HTTPError("u", code, "x", {}, None)

    def test_a_5xx_answer_is_retried(self):
        from unittest.mock import patch
        calls, urlopen = self.fake([self.http(504), self.http(502), "ok"])
        with patch.object(statechart.urllib.request, "urlopen", urlopen):
            self.assertEqual(statechart._download("u", sleep=lambda s: None), b"jar")
        self.assertEqual(len(calls), 3)

    def test_a_4xx_answer_is_not_retried(self):
        from unittest.mock import patch
        calls, urlopen = self.fake([self.http(404)])
        with patch.object(statechart.urllib.request, "urlopen", urlopen), self.assertRaises(Exception):
            statechart._download("u", sleep=lambda s: None)
        self.assertEqual(len(calls), 1)

if __name__ == "__main__":
    unittest.main()

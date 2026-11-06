#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Tests for factory/definition.py (ADR-0021). Run: uv run factory/tests/test_definition.py"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import definition  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/v2"


class DefinitionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "team"
        shutil.copytree(FIXTURE, self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def edit(self, name, change):
        """Apply `change(data)` to factory.yaml or workflow.yaml of the copy."""
        path = self.root / name
        data = yaml.safe_load(path.read_text())
        change(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    def errors(self):
        return definition.errors(definition.load(self.root))

    def assertError(self, prefix):
        errs = self.errors()
        self.assertTrue(any(e.startswith(prefix) for e in errs), errs)

    def test_fixture_is_valid(self):
        self.assertTrue(definition.present(self.root))
        self.assertEqual(self.errors(), [])

    def test_fixture_warns_only_about_labels_a_person_adds(self):
        self.assertEqual(definition.warnings(definition.load(self.root)), [])
        self.edit("workflow.yaml", lambda w: w["nodes"].pop("adopt"))
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["proceed"]["effects"]
                  .append({"label": "factory:nobody"}))
        self.assertEqual(definition.warnings(definition.load(self.root)),
                         ["unwatched-label: factory:nobody: no node triggers on it"])

    def test_unknown_agent(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"].update(agent="nobody"))
        self.assertError("unknown-agent: nodes.intake")

    def test_unknown_environment(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["retro"].update(environment="nowhere"))
        self.assertError("unknown-environment: nodes.retro")

    def test_unknown_model(self):
        self.edit("factory.yaml", lambda f: f["agents"]["dev"].update(model="gpt-0"))
        self.assertError("unknown-model: agents.dev")

    def test_unknown_role(self):
        self.edit("factory.yaml", lambda f: f["agents"]["dev"].update(role="painter"))
        self.assertError("unknown-role: agents.dev")

    def test_unknown_skill(self):
        self.edit("factory.yaml", lambda f: f["roles"]["developer"]["skills"].append("juggling"))
        self.assertError("unknown-skill: roles.developer: juggling")

    def test_sourced_skill_needs_sha256(self):
        self.edit("factory.yaml", lambda f: f["skills"]["tdd"].pop("sha256"))
        self.assertError("unpinned-skill: skills.tdd")

    def test_inline_role_skill(self):
        self.edit("factory.yaml", lambda f: f["roles"]["reviewer"]["skills"].append(
            {"adr-reading": {"path": "roles/reviewer/skills/adr-reading"}}))
        self.assertEqual(self.errors(), [])
        self.edit("factory.yaml", lambda f: f["roles"]["reviewer"]["skills"].append({"broken": {}}))
        self.assertError("bad-skill: roles.reviewer: broken")

    def test_unknown_webhook(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["notify"]["triggers"].append({"webhook": "nope"}))
        self.assertError("unknown-webhook: nodes.notify: nope")

    def test_one_main_repository(self):
        self.edit("factory.yaml", lambda f: f["environments"]["engine"]["repositories"][1].update(main=True))
        self.assertError("main-repository: environments.engine")

    def test_writable_repository_needs_token(self):
        self.edit("factory.yaml", lambda f: f["environments"]["engine"]["repositories"][1].pop("token_env"))
        self.assertError("write-token: environments.engine")

    def test_token_env_must_be_declared(self):
        self.edit("factory.yaml", lambda f: f["environments"]["engine"].pop("env"))
        self.assertError("undeclared-env: environments.engine: FACTORY_WRITE_TOKEN")

    def test_role_needs_body_and_body_needs_role(self):
        (self.root / "roles/router/role.md").unlink()
        self.assertError("missing-role-body: roles.router")
        (self.root / "roles/painter").mkdir()
        (self.root / "roles/painter/role.md").write_text("Paint.\n")
        self.assertError("unlisted-role: roles/painter")

    def test_else_names_an_outcome(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["join"]["outcomes"]["approved"].update({"else": "nothing"}))
        self.assertError("unknown-else: nodes.join.approved: nothing")

    def test_undefined_template_name(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"].update(prompt="Limit {{ settings.nope }}"))
        self.assertError("render: nodes.intake")

    def test_settings_merge_order(self):
        defn = definition.load(self.root)
        s = definition.settings(defn, "fix-mid")
        self.assertEqual(s["fix_rounds"], 3)                    # node
        self.assertEqual(s["limits"]["runs_per_day"], 20)       # workflow settings
        self.assertEqual(s["size_stop_factor"], 1.5)            # agent `dev`
        self.assertEqual(s["branch_pattern"], "ez/{ticket}-{slug}")   # factory defaults
        defn["workflow"]["nodes"]["fix-mid"]["settings"]["branch_pattern"] = None   # `null` clears
        self.assertNotIn("branch_pattern", definition.settings(defn, "fix-mid"))

    def test_render_uses_environment_repositories(self):
        body, prompt = definition.render(definition.load(self.root), "knowledge-review")
        self.assertIn("`zaharchenko-evgeny/slop-eval-factory` (main)", body)
        self.assertIn("focus knowledge", prompt)

    def test_compile_variant_nodes_into_steps(self):
        descriptor, wf = definition.to_project(definition.load(self.root))
        n = wf["nodes"]
        self.assertEqual(n["implement"]["tiers"], ["cheap", "mid", "strong"])
        self.assertEqual(n["implement"]["wakes_on"], ["command implement-<tier>"])
        self.assertEqual(n["review"]["focuses"], ["full", "cheap"])
        self.assertEqual(n["update"]["tiers"], ["cheap", "strong"])
        air = descriptor["targets"]["air"]["models"]
        self.assertEqual((air["cheap"], air["mid"]), ("codex/gpt-6-luna", "claude/claude-sonnet-5-5[1m]"))
        self.assertEqual(descriptor["roles"]["reviewer"]["modes"], {"full": {"tier": "sol"}, "cheap": {"tier": "luna"}})
        self.assertEqual(descriptor["reviews"]["required"], {"low": ["cheap"], "high": ["full"]})

    def test_compile_triggers(self):
        wakes = {k: v["wakes_on"] for k, v in definition.to_project(definition.load(self.root))[1]["nodes"].items()}
        self.assertEqual(wakes["join"], ["pr reviewed", "run quality-gates"])   # ci: failed
        self.assertEqual(wakes["finish"], ["state pr-merged", "ticket closed", "state blocked", "ticket commented"])
        self.assertEqual(wakes["notify"], ["state blocked", "state needs-answers", "webhook inbox"])
        self.assertEqual(wakes["retro"], ["schedule retro"])
        self.assertEqual(wakes["human"], ["manual"])

    def test_compile_guards_and_effects(self):
        desc, wf = definition.to_project(definition.load(self.root))
        n = wf["nodes"]
        self.assertNotIn("done", n["human"]["outcomes"]["ready"]["from"])          # `!done`
        self.assertIn("approved", n["human"]["outcomes"]["ready"]["from"])
        self.assertEqual(n["join"]["outcomes"]["approved"], {"ci": ["green"], "else": "changes",
                                                            "effects": [{"state": "approved"}]})
        self.assertEqual(n["implement"]["outcomes"]["error"]["effects"],
                         [{"uncommand": "implement-*"}, {"command": "implement-<next tier>"}, {"state": "in-dev"}])
        self.assertEqual(desc["commands"]["knowledge-review"], {"store": {"pr_label": "factory:knowledge-review"}})
        self.assertEqual(desc["states"]["done"]["kind"], "final")

    def test_compiled_view_passes_engine_checks(self):
        os.environ["FACTORY_TEAM"] = str(self.root)
        try:
            import config, nodes  # noqa: E401
            self.assertEqual(nodes.validate(nodes.load(), config.load()), [])
        finally:
            del os.environ["FACTORY_TEAM"]

    def test_cli_validate(self):
        run = subprocess.run(["uv", "run", "--quiet", str(ROOT / "cli.py"), "validate"], capture_output=True,
                             text=True, env={**os.environ, "FACTORY_TEAM": str(self.root)})
        self.assertEqual((run.returncode, run.stdout.strip()), (0, "factory: ok"), run.stderr)


if __name__ == "__main__":
    unittest.main()

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

    def test_missing_owner_fails(self):
        self.edit("factory.yaml", lambda f: f.pop("owner"))
        self.assertIn("missing-owner: factory.yaml: `owner` (the GitHub login a person-wait is assigned to)", self.errors())

    def test_owner_reaches_the_descriptor(self):
        descriptor, _ = definition.to_project(definition.load(self.root))
        self.assertEqual(descriptor["owner"], "zaharchenko-evgeny")
        self.assertNotIn("notify", descriptor)

    def test_fixture_warns_only_about_labels_a_person_adds(self):
        self.assertEqual(definition.warnings(definition.load(self.root)), [])
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["ready"]["effects"]
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
        self.edit("workflow.yaml", lambda w: w["nodes"]["automerge"]["outcomes"]["merge"].update({"else": "nothing"}))
        self.assertError("unknown-else: nodes.automerge.merge: nothing")

    def test_guard_reads_known_facts(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["merge"]["outcomes"]["merged"].update({"if": {"colour": "red"}}))
        self.assertError("unknown-guard: nodes.merge.merged: colour")
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["ready"]["effects"].append({"set-field": "risk"}))
        self.assertError("unknown-effect: nodes.intake.ready: set-field")

    def test_undefined_template_name(self):
        body = self.root / "roles/router/role.md"
        body.write_text(body.read_text() + "\nLimit {{ settings.nope }}\n")
        self.assertError("render: nodes.intake")

    def test_nodes_have_no_prompt(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"].update(prompt="Scope it."))
        self.assertError("node-prompt: nodes.intake")

    def test_settings_merge_order(self):
        defn = definition.load(self.root)
        s = definition.settings(defn, "fix")
        self.assertEqual(s["fix_rounds"], 3)                    # node
        self.assertEqual(s["limits"]["runs_per_day"], 20)       # workflow settings
        self.assertEqual(s["size_stop_factor"], 1.5)            # agent `dev`
        self.assertEqual(s["branch_pattern"], "ez/{ticket}-{slug}")   # factory defaults
        defn["workflow"]["nodes"]["fix"]["settings"]["branch_pattern"] = None   # `null` clears
        self.assertNotIn("branch_pattern", definition.settings(defn, "fix"))

    def test_render_uses_environment_repositories(self):
        defn = definition.load(self.root)
        body = definition.render(defn, "knowledge-review")
        self.assertIn("`zaharchenko-evgeny/slop-eval-factory` (main)", body)
        self.assertIn("with focus knowledge;", body)
        self.assertIn("in mode `fix`", definition.render(defn, "fix"))          # what differs comes from settings

    def test_compile_is_one_to_one(self):
        defn = definition.load(self.root)
        descriptor, wf = definition.to_project(defn)
        self.assertEqual(set(wf["nodes"]), set(defn["workflow"]["nodes"]))
        n = wf["nodes"]
        self.assertEqual((n["implement-small"]["tier"], n["implement-medium"]["tier"], n["review-high"]["tier"]),
                         ("luna", "sonnet", "sol"))
        self.assertNotIn("tiers", n["implement-small"])
        self.assertEqual(n["implement-small"]["wakes_on"], ["command implement-small"])
        self.assertEqual(descriptor["targets"]["air"]["models"]["sol"], "codex/gpt-6.1-sol")
        self.assertEqual(descriptor["commands"]["review-low"], {"store": {"pr_label": "factory:review-low"}})
        self.assertEqual(descriptor["commands"]["risk:low"], {"store": {"label": "risk:low"}})

    def test_compile_triggers(self):
        wakes = {k: v["wakes_on"] for k, v in definition.to_project(definition.load(self.root))[1]["nodes"].items()}
        self.assertEqual(wakes["fix"], ["command fix"])   # red CI reaches it through the reviewer's verdict
        self.assertEqual(wakes["finish"], ["state pr-merged", "ticket closed"])
        self.assertEqual(wakes["notify"], ["state blocked", "state needs-answers", "command needs-human"])
        self.assertEqual(wakes["retro"], ["schedule retro"])
        self.assertEqual(wakes["human"], ["manual"])

    def test_compile_guards_and_effects(self):
        desc, wf = definition.to_project(definition.load(self.root))
        n = wf["nodes"]
        self.assertNotIn("done", n["human"]["outcomes"]["ready"]["from"])          # `!done`
        self.assertIn("draft", n["human"]["outcomes"]["ready"]["from"])
        self.assertEqual(n["merge"]["outcomes"]["merged"]["if"], {"review": "approved-at-head"})
        self.assertEqual(n["merge"]["outcomes"]["merged"]["ci"], ["green"])
        self.assertEqual(n["automerge"]["outcomes"]["merge"]["if"], {"label": "risk:low"})
        self.assertEqual(n["automerge"]["outcomes"]["merge"]["else"], "needs-human")
        self.assertEqual(n["dispatch"]["outcomes"]["small"]["effects"],
                         [{"command": "complexity:small"}, {"command": "implement-small"}, {"state": "in-dev"}])
        self.assertEqual(desc["states"]["done"]["kind"], "final")

    def test_compiled_view_passes_engine_checks(self):
        os.environ["FACTORY_TEAM"] = str(self.root)
        try:
            import config, nodes  # noqa: E401
            self.assertEqual(nodes.validate(nodes.load(), config.load()), [])
        finally:
            del os.environ["FACTORY_TEAM"]

    def test_a_ticket_walks_the_agent_only_flow(self):
        """triage -> ready -> in-dev -> in-review -> approved -> merged -> done on a memory tracker (ADR-0021)."""
        import config
        import gate
        import nodes as nodes_mod
        from gate_port import MemoryTracker
        before = config._team
        config.set_team(self.root)
        try:
            n = nodes_mod.load()
            t = MemoryTracker({"7": {"stage": "triage", "head": "h1", "pr": 5, "pr_state": "open", "ci": "green"}})
            for node, outcome, extra in [("intake", "medium-high", {}), ("dispatch", "medium", {}),
                                         ("implement-medium", "done-low", {}),
                                         ("review-low", "approve", {"findings": [], "verdict": "approve"})]:
                self.assertTrue(gate.apply(node, outcome, {"ticket": "7", **extra}, t, n)[1]["applied"], node)
            attrs = t.facts("7")["attrs"]
            self.assertIn("risk:low", attrs)
            self.assertNotIn("risk:high", attrs)                     # one value per group
            t.tickets["7"]["reviews"] = [{"agent": True, "commit_id": "h1", "verdict": "approve"}]
            self.assertTrue(gate.apply("automerge", "merge", {"ticket": "7"}, t, n)[1]["applied"])
            self.assertEqual(gate.decide("merge", "7", t, n)[1]["outcome"], "merged")
            t.tickets["7"]["pr_state"] = "merged"
            self.assertEqual(gate.decide("finish", "7", t, n)[1]["outcome"], "done")
            self.assertEqual(t.facts("7")["stage"], "done")
            risky = MemoryTracker({"8": {"stage": "approved", "attrs": ["risk:high"], "pr": 6, "pr_state": "open"}})
            gate.apply("automerge", "merge", {"ticket": "8", "reason": "[factory] fine"}, risky, n)
            self.assertIn("factory:needs-human", risky.facts("8")["attrs"])   # the guard sent it to a person
        finally:
            config.set_team(before)

    def test_neighbours_come_from_the_workflow(self):
        pairs = definition.neighbours(definition.load(self.root))
        for a, b in [("dev", "reviewer"), ("dev", "reviewer-cheap"), ("reviewer", "approver"), ("dispatcher", "dev"),
                     ("approver", "clerk"), ("dev", "dev-strong")]:
            self.assertIn(frozenset((a, b)), pairs, (a, b))
        self.assertNotIn(frozenset(("router", "approver")), pairs)

    def test_comment_marker_names_the_agent(self):
        import config
        import gate
        before = config._team
        config.set_team(self.root)
        try:
            self.assertEqual(gate.comment_marker("review-high"), "<!-- factory:from reviewer -->")
            self.assertEqual(gate.comment_marker("ask-dev"), "<!-- factory:from dev reply -->")
        finally:
            config.set_team(before)

    def test_cli_validate(self):
        run = subprocess.run(["uv", "run", "--quiet", str(ROOT / "cli.py"), "validate"], capture_output=True,
                             text=True, env={**os.environ, "FACTORY_TEAM": str(self.root)})
        self.assertEqual((run.returncode, run.stdout.strip()), (0, "factory: ok"), run.stderr)


if __name__ == "__main__":
    unittest.main()

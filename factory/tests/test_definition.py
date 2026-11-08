#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Tests for factory/definition.py (ADR-0021). Run: uv run factory/tests/test_definition.py"""
import json
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

    def test_per_repository_node_needs_an_environment_per_code_repository(self):
        def two_repos(f):
            f["environments"]["engine"]["repositories"] += [{"repo": "o/web"}]
            f["environments"]["web"] = {**f["environments"]["engine"], "repositories": [{"repo": "o/web", "main": True}]}
        self.edit("factory.yaml", two_repos)
        self.assertEqual(self.errors(), [])
        self.edit("factory.yaml", lambda f: f["environments"].pop("web"))
        self.assertError("per-repo-env: o/web")

    def test_write_mode_compiles_from_the_default_settings(self):
        self.edit("factory.yaml", lambda f: f["defaults"]["settings"].update(write_mode="token"))
        self.assertEqual(self.errors(), [])
        project, _ = definition.to_project(definition.load(self.root))
        self.assertEqual(project["write_mode"], "token")

    def test_role_secret_reaches_the_environments_of_its_nodes_only(self):
        self.edit("factory.yaml", lambda f: f["roles"]["router"].update(
            env={"ISSUES_TOKEN": {"required": True, "secret": True, "description": "Issues read/write"}}))
        self.assertEqual(self.errors(), [])
        project, _ = definition.to_project(definition.load(self.root))
        engine, team = project["environments"]["engine"], project["environments"]["team"]
        self.assertIn("ISSUES_TOKEN", engine["secrets"])  # intake (router) runs in the default environment
        self.assertEqual(engine["secret_help"]["ISSUES_TOKEN"], "Issues read/write")
        self.assertNotIn("ISSUES_TOKEN", team["secrets"])

    def test_assignment_effects_validate_and_compile_unchanged(self):
        effects = [{"assign": "owner", "on": "pr"}, {"unassign": "all"}]
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["ready"].update(effects=effects))
        self.assertEqual(self.errors(), [])
        _, workflow = definition.to_project(definition.load(self.root))
        self.assertEqual(workflow["nodes"]["intake"]["outcomes"]["ready"]["effects"], effects)

    def test_assignment_field_form_validates(self):
        effects = [{"assign": {"field": "assignee"}, "on": "pr"}]
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["ready"].update(effects=effects))
        self.assertEqual(self.errors(), [])

    def test_assignment_effect_rejects_other_dicts(self):
        for value in ({"fieldd": "x"}, {"field": "x", "extra": 1}, {"field": 3}):
            with self.subTest(value=value):
                self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["ready"].update(
                    effects=[{"assign": value}]))
                self.assertError("bad-effect: nodes.intake.ready: assign")

    def test_assignment_effect_rejects_non_string_values(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["ready"].update(
            effects=[{"assign": 3}]))
        self.assertError("bad-effect: nodes.intake.ready: assign")

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

    def test_event_node_in_non_tracker_environment_is_deaf_trigger(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["notify"].update(environment="team"))
        self.assertError("deaf-trigger: nodes.notify: environment team main repository zaharchenko-evgeny/slop-eval-factory")

    def test_pr_label_node_outside_pr_repository_is_deaf_trigger(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["automerge"]["outcomes"]["needs-human"]["effects"][-1].update(on="pr"))
        self.edit("workflow.yaml", lambda w: w["nodes"]["notify"].update(
            environment="team", triggers=[{"label": "factory:needs-human"}]))
        self.assertError("deaf-trigger: nodes.notify: environment team main repository zaharchenko-evgeny/slop-eval-factory")

    def test_pr_and_ci_triggers_outside_code_repositories_are_deaf_triggers(self):
        for trigger in ({"pr": "merged"}, {"ci": "failed"}):
            self.edit("workflow.yaml", lambda w: w["nodes"]["notify"].update(environment="team", triggers=[trigger]))
            self.assertError("deaf-trigger: nodes.notify: environment team main repository zaharchenko-evgeny/slop-eval-factory")

    def test_scheduled_node_in_any_environment_is_fine(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["retro"].update(environment="team"))
        self.assertEqual(self.errors(), [])

    def test_pr_label_node_in_pr_repository_environment_is_fine(self):
        def add_pr_repository(factory):
            factory["environments"]["engine"]["repositories"].append({"repo": "owner/web"})
            factory["environments"]["web"] = {"repositories": [{"repo": "owner/web", "main": True}]}
        self.edit("factory.yaml", add_pr_repository)
        self.edit("workflow.yaml", lambda w: w["nodes"]["retro"].update(
            environment="web", outcomes={**w["nodes"]["retro"]["outcomes"],
                                         "proposed": {"effects": [{"label": "factory:knowledge-review", "on": "pr"},
                                                                      {"label": "factory:review-low", "on": "pr"}]}}))
        self.edit("workflow.yaml", lambda w: w["nodes"]["review-low"].update(
            environment="web", triggers=[{"label": "factory:review-low"}]))
        self.edit("workflow.yaml", lambda w: w["nodes"]["knowledge-review"].update(environment="web"))
        self.assertEqual(self.errors(), [])

    def test_one_main_repository(self):
        self.edit("factory.yaml", lambda f: f["environments"]["engine"]["repositories"][1].update(main=True))
        self.assertError("main-repository: environments.engine")

    def test_writable_repository_needs_token(self):
        self.edit("factory.yaml", lambda f: f["environments"]["engine"]["repositories"][1].update(write=True))
        self.assertError("write-token: environments.engine")

    def test_token_env_must_be_declared(self):
        self.edit("factory.yaml", lambda f: f["environments"]["engine"]["repositories"][1].update(write=True, token_env="T"))
        self.assertError("undeclared-env: environments.engine: T")
        self.edit("factory.yaml", lambda f: f["environments"]["engine"].update(env={"T": {"secret": True}}))
        self.assertEqual(self.errors(), [])

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

    MCP = {"youtrack": {"connector": "internal-youtrack"},
           "idea": {"command": "npx", "args": ["-y", "@jetbrains/mcp-proxy"], "env": {"IDE_PORT": "${IDE_PORT}"}},
           "docs": {"url": "https://mcp.example/sse", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}}}

    def add_mcp(self, layer="roles.developer", servers=None, env=("IDE_PORT", "DOCS_TOKEN")):
        """Put `servers` on `layer` ("roles.developer", "agents.dev", "defaults") and declare `env` in defaults."""
        def change(f):
            f["defaults"].setdefault("env", {}).update({v: {"required": False} for v in env})
            top, _, name = layer.partition(".")
            (f["defaults"] if top == "defaults" else f[top][name])["mcp"] = servers if servers is not None else self.MCP
        self.edit("factory.yaml", change)

    def test_mcp_merges_by_key_across_layers_and_null_removes(self):
        self.add_mcp("defaults", {"glean": {"connector": "internal-glean"}, "docs": {"url": "https://old.example/sse"}})
        self.add_mcp("roles.developer")
        self.edit("factory.yaml", lambda f: f["agents"]["dev"].update(mcp={"slack": {"connector": "slack"}, "docs": None}))
        self.edit("workflow.yaml", lambda w: w["nodes"]["implement-medium"].update(mcp={"glean": None}))
        defn = definition.load(self.root)
        self.assertEqual(sorted(definition.mcp(defn, "implement-medium")), ["idea", "slack", "youtrack"])
        self.assertEqual(sorted(definition.mcp(defn, "implement-small")), ["docs", "glean", "idea", "youtrack"])  # other agent
        self.assertEqual(definition.mcp(defn, "implement-small")["docs"], self.MCP["docs"])   # role replaces default's entry whole
        self.assertEqual(definition.mcp_connectors(definition.mcp(defn, "implement-small")), ["internal-glean", "internal-youtrack"])
        self.assertEqual(sorted(definition.mcp_custom(definition.mcp(defn, "implement-small"))), ["docs", "idea"])

    def test_mcp_connectors_reach_the_compiled_node(self):
        self.add_mcp()
        _, wf = definition.to_project(definition.load(self.root))
        self.assertEqual(wf["nodes"]["implement-medium"]["mcp"], ["internal-youtrack"])
        self.assertNotIn("mcp", wf["nodes"]["intake"])

    def test_mcp_valid_servers_pass_validate(self):
        self.add_mcp()
        self.assertEqual(self.errors(), [])

    def test_mcp_validate_unknown_connector(self):
        self.add_mcp(servers={"x": {"connector": "nope"}})
        self.assertError("unknown-mcp-connector: roles.developer.mcp.x: nope")
        self.edit("factory.yaml", lambda f: f.setdefault("targets", {}).update(air={"mcp_connectors": ["nope"]}))
        self.assertEqual(self.errors(), [])   # the list in `targets.air.mcp_connectors` replaces the default

    def test_mcp_validate_undeclared_env(self):
        self.add_mcp(env=("IDE_PORT",))
        self.assertError("mcp-undeclared-env: roles.developer.mcp.docs: ${DOCS_TOKEN}")
        self.add_mcp()   # now declared
        self.assertEqual(self.errors(), [])
        self.edit("workflow.yaml", lambda w: w["nodes"]["fix"].update(mcp={"d": {"url": "https://x.example/${HOST}"}}))
        self.assertError("mcp-undeclared-env: nodes.fix.mcp.d: ${HOST}")
        self.edit("workflow.yaml", lambda w: w["nodes"]["fix"].update(env={"HOST": {"required": False}}))   # the node's own env
        self.assertEqual(self.errors(), [])

    def test_mcp_validate_literal_secret(self):
        self.add_mcp(servers={"docs": {"url": "https://x.example", "headers": {"Authorization": "Bearer abc123"}},
                              "idea": {"command": "x", "env": {"PLAIN": "ghp_abcdef123456"}}})
        errs = self.errors()
        self.assertIn("mcp-literal-secret: roles.developer.mcp.docs.headers.Authorization: use a ${VAR} reference", errs)
        self.assertIn("mcp-literal-secret: roles.developer.mcp.idea.env.PLAIN: use a ${VAR} reference", errs)

    def test_mcp_validate_mixed_entry(self):
        self.add_mcp(servers={"x": {"connector": "slack", "command": "npx"}})
        self.assertError("mcp-mixed-entry: roles.developer.mcp.x")

    def test_nodes_have_no_prompt(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"].update(prompt="Scope it."))
        self.assertError("node-prompt: nodes.intake")

    def test_compile_lists_the_writable_repositories_of_each_environment(self):
        import definition
        envs = definition.to_project(definition.load(self.root))[0]["environments"]
        for name, env in envs.items():
            with self.subTest(env=name):
                self.assertTrue(set(env["writable"]) <= set(env["repositories"]))
                self.assertIn(env["repositories"][0], env["writable"])  # the main repository is always writable

    def test_issues_token_env_setting_passes_validation(self):
        self.edit("factory.yaml", lambda f: f["defaults"].setdefault("settings", {}).update(issues_token_env="FACTORY_ISSUES_TOKEN"))
        run = subprocess.run(["uv", "run", "--quiet", str(ROOT / "cli.py"), "validate"], capture_output=True,
                             text=True, env={**os.environ, "FACTORY_TEAM": str(self.root)})
        self.assertEqual((run.returncode, run.stdout.strip()), (0, "factory: ok"), run.stdout + run.stderr)

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

    def test_ci_result_labels_are_pr_labels(self):
        """#790: CI labels the PR itself, so no effect needs `on: pr` for a CI result label."""
        descriptor = definition.to_project(definition.load(ROOT / "tests/fixtures/slop"))[0]
        for fact in ("ci-green", "ci-red"):
            self.assertEqual(descriptor["commands"][fact], {"store": {"pr_label": f"factory:{fact}"}})

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

    def test_nodes_read_the_definition_in_the_gate_shape(self):
        graph = definition.nodes(definition.load(self.root))
        n = graph["nodes"]
        self.assertEqual(n["fix"]["triggers"], ["pull_request.labeled factory:fix"])
        self.assertEqual(n["finish"]["triggers"], ["pull_request.merged", "issues.closed"])
        self.assertEqual(n["retro"]["triggers"], ["schedule retro"])
        self.assertTrue(n["retro"]["routine"])
        self.assertEqual(n["human"]["triggers"], ["manual"])
        self.assertEqual(n["human"]["when"], {"actor": "human"})
        self.assertNotIn("done", n["human"]["outcomes"]["ready"]["from"])          # `!done`
        self.assertIn("draft", n["human"]["outcomes"]["ready"]["from"])
        self.assertEqual(n["dispatch"]["outcomes"]["small"]["effects"],
                         [{"label": "complexity:small"}, {"label": "factory:implement-small"}, {"stage": "in-dev"}])
        self.assertEqual(n["automerge"]["triggers"], ["pull_request.labeled factory:automerge"])  # a PR label
        self.assertTrue(n["automerge"]["ci-independent"])
        self.assertEqual(n["automerge"]["outcomes"]["merge"]["effects"][1], {"label": "factory:merge", "on": "pr"})
        self.assertEqual(graph["schedules"]["retro"], {"cron": "0 2 * * *"})
        self.assertEqual(graph["environments"]["engine"]["repository"], "primary")
        self.assertEqual(graph["environments"]["team"]["repository"], "slop-eval-factory")

    def test_config_reads_stages_and_commands_from_the_definition(self):
        tracker = definition.config(definition.load(self.root))["tracker"]
        self.assertEqual(tracker["stages"]["in-dev"], "factory:in-dev")
        self.assertEqual(tracker["stage_kinds"]["done"], "final")
        self.assertNotIn("pr-merged", tracker["stages"])  # a PR fact is no label
        self.assertEqual(tracker["commands"]["review-low"], {"pr_label": "factory:review-low"})

    def test_compiled_view_passes_engine_checks(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"FACTORY_TEAM": str(self.root)}):  # restores the caller's value
            import config, nodes  # noqa: E401
            self.assertEqual(nodes.validate(nodes.load(), config.load()), [])

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
            written, plain = [], t.apply
            t.apply = lambda e: written.append(e) or plain(e)
            for node, outcome, extra in [("intake", "medium-high", {}), ("dispatch", "medium", {}),
                                         ("implement-medium", "done-low", {}),
                                         ("review-low", "approve", {"findings": [], "verdict": "approve"})]:
                self.assertTrue(gate.apply(node, outcome, {"ticket": "7", **extra}, t, n)[1]["applied"], node)
            self.assertEqual([e["focus"] for e in written if e["kind"] == "review"], ["review-low"])  # the node names it
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

    def test_the_last_child_done_finishes_its_split_parent(self):
        import config
        import gate
        import nodes as nodes_mod
        from gate_port import MemoryTracker
        before = config._team
        config.set_team(self.root)
        try:
            n = nodes_mod.load()
            t = MemoryTracker({"9": {"stage": "split"}, "7": {"stage": "approved", "pr_state": "merged"},
                               "8": {"stage": "done"}})
            state = {"7": "open", "8": "closed"}
            t.parent = lambda x: "9" if str(x) in ("7", "8") else None
            t.children = lambda x: [{"number": k, "state": s} for k, s in state.items()] if str(x) == "9" else []
            plain = t.apply
            t.apply = lambda e: (state.__setitem__("7", "closed") if e["kind"] == "stage" and e["ticket"] == "7" else None) \
                or plain(e)
            out = gate.decide("finish", "7", t, n)[1]
            self.assertEqual((out["outcome"], out["parent"]["outcome"]), ("done", "children-done"))
            self.assertEqual(t.facts("9")["stage"], "done")
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
            self.assertEqual(gate.comment_marker("reply-dev"), "<!-- factory:from dev reply -->")
        finally:
            config.set_team(before)

    def test_mentionable_agents_get_a_generated_reply_step(self):
        defn = definition.load(self.root)
        nodes = defn["workflow"]["nodes"]
        self.assertEqual(set(nodes["mentions"]["outcomes"]), {"reply-dev", "reply-reviewer", "none"})
        self.assertEqual(nodes["mentions"]["outcomes"]["reply-dev"]["if"], {"mention": "dev"})
        self.assertEqual((nodes["reply-dev"]["agent"], nodes["reply-dev"]["output"]), ("dev", "read-only"))
        self.assertIn("Someone asked you", definition.render(defn, "reply-reviewer"))
        self.edit("workflow.yaml", lambda w: w["nodes"].update({"reply-x": {"agent": "dev", "triggers": ["manual"],
                                                                            "outcomes": {"done": {}}}}))
        self.assertError("reserved-node: nodes.reply-x")

    def test_fix_and_update_merge_main_on_a_conflicting_pr(self):
        import config
        import gate
        import nodes as nodes_mod
        from gate_port import MemoryTracker
        before = config._team
        config.set_team(self.root)
        try:
            n = nodes_mod.load()
            for conflict, want in ((True, True), (False, False)):
                t = MemoryTracker({"7": {"stage": "changes-requested", "pr": 5, "head": "h1", "conflict": conflict}})
                for node in ("fix", "update"):
                    with self.subTest(node=node, conflict=conflict):
                        code, out = gate.check(node, {"id": "e1", "ticket": "7"}, t, n, config.load())
                        self.assertEqual((code, out["merge_main"]), (gate.GO, want))
        finally:
            config.set_team(before)

    def test_a_failed_review_write_leaves_labels_and_stage_as_they_were(self):
        import config
        import gate
        import nodes as nodes_mod
        from gate_port import MemoryTracker
        before = config._team
        config.set_team(ROOT / "tests/fixtures/slop")  # its review outcomes replace the risk label (#998)
        try:
            n = nodes_mod.load()
            t = MemoryTracker({"7": {"stage": "in-review", "pr": 5, "head": "h1", "ci": "green", "attrs": ["risk:low"],
                                     "pr_labels": ["factory:review-low"]}})
            apply = t.apply

            def failing(e):
                if e["kind"] == "review":
                    raise RuntimeError("HTTP 403: Resource not accessible by integration")
                return apply(e)
            t.apply = failing
            with self.assertRaises(RuntimeError):
                gate.apply("review-low", "approve-risky", {"ticket": "7", "summary": "ok"}, t, n)
            f = t.facts("7")
            self.assertEqual((f["stage"], "risk:low" in f["attrs"]), ("in-review", True))
        finally:
            config.set_team(before)

    def test_a_person_asks_dev_on_the_pr_and_dev_answers_there(self):
        import config
        import gate
        import nodes as nodes_mod
        from gate_port import MemoryTracker
        before = config._team
        config.set_team(self.root)
        try:
            n = nodes_mod.load()
            t = MemoryTracker({"7": {"stage": "in-review", "pr": 5, "head": "h1"}})
            t.pr_comments = lambda ticket: [{"author": "alice", "body": "@factory:dev why the retry?", "at": "2"}]
            status, out = gate.decide("mentions", "7", t, n)
            self.assertEqual(out["outcome"], "reply-dev")
            self.assertIn("factory:reply-dev", t.facts("7")["attrs"])
            begin = gate.check("reply-dev", {"id": "e1", "ticket": "7"}, t, n, config.load())[1]
            self.assertEqual({k: begin["mention"][k] for k in ("by", "on")}, {"by": "alice", "on": "pr"})
            sent = []
            apply = t.apply
            t.apply = lambda e: sent.append(e) or apply(e)
            gate.apply("reply-dev", "replied", {"ticket": "7", "reply": "It covers the flaky API."}, t, n)
            reply = next(e for e in sent if e["kind"] == "comment")
            self.assertEqual(reply.get("on"), "pr")                                   # where the question was
            self.assertTrue(reply["value"].startswith("@alice It covers the flaky API."))
            self.assertIn("<!-- factory:from dev reply -->", reply["value"])
            self.assertNotIn("factory:reply-dev", t.facts("7")["attrs"])
            t.pr_comments = lambda ticket: [{"author": "bot", "body": reply["value"], "at": "3"}]
            self.assertEqual(gate.decide("mentions", "7", t, n)[1]["outcome"], "none")  # a reply asks no one
        finally:
            config.set_team(before)

    def test_ask_is_an_outcome_of_every_mentionable_node_and_the_answer_returns_to_the_asker(self):
        import config
        import gate
        import nodes as nodes_mod
        from gate_port import MemoryTracker
        before = config._team
        config.set_team(self.root)
        try:
            n = nodes_mod.load()
            agents = {k: v.get("agent") for k, v in definition.load(self.root)["workflow"]["nodes"].items()}
            for name, node in n["nodes"].items():
                self.assertEqual("ask" in node["outcomes"], agents[name] in ("dev", "reviewer") and not name.startswith("reply-"), name)
            self.assertEqual(n["nodes"]["implement-medium"]["outcomes"]["ask"]["to"], ["reviewer"])
            t = MemoryTracker({"7": {"stage": "in-dev"}})
            gate.apply("implement-medium", "ask", {"ticket": "7", "to": "reviewer", "text": "Is X in scope?"}, t, n)
            ask = next(json.loads(v) for k, v in t.tickets["7"]["log"] if k == "comment")
            self.assertTrue(ask.startswith("[factory] **dev (developer):** @factory:reviewer Is X in scope?\n"))
            self.assertEqual(t.facts("7")["stage"], "in-dev")
            t.tickets["7"]["comments"] = [{"author": "bot", "body": ask, "at": "1"}]
            self.assertEqual(gate.decide("mentions", "7", t, n)[1]["outcome"], "reply-reviewer")   # the target wakes
            t.tickets["7"]["log"].clear()
            with self.assertRaises(gate.Bad):
                gate.apply("reply-reviewer", "ask", {"ticket": "7", "to": "dev", "text": "?"}, t, n)
            gate.apply("reply-reviewer", "replied", {"ticket": "7", "reply": "Yes."}, t, n)
            answer = next(json.loads(v) for k, v in t.tickets["7"]["log"] if k == "comment")
            t.tickets["7"]["comments"].append({"author": "bot", "body": answer, "at": "2"})
            self.assertEqual(gate.decide("mentions", "7", t, n)[1]["outcome"], "reply-dev")        # the asker wakes
        finally:
            config.set_team(before)

    def test_scheduled_nodes_are_routines(self):
        n = definition.to_project(definition.load(self.root))[1]["nodes"]
        self.assertTrue(all(n[k].get("routine") for k in ("retro", "reconcile", "doctor-engine", "doctor-team")))
        self.assertFalse(n["intake"].get("routine"))

    def test_doctor_reads_skills_from_the_definition(self):
        import config
        import doctor
        import nodes as nodes_mod
        before = config._team
        config.set_team(self.root)
        try:
            tools, skills = doctor.env_needs("engine", nodes_mod.load())
            self.assertIn("tdd", skills)
            self.assertIn("factory-step", skills)
            self.assertNotIn("retro", skills)        # retro runs in `knowledge`
        finally:
            config.set_team(before)

    def test_old_command_templates_never_match_for_a_definition_team(self):
        import config
        import vocab
        before = config._team
        config.set_team(self.root)
        try:
            for name in ("implement-<tier>", "fix-<tier>", "update-<tier>", "review-<focus>", "rereview-<focus>"):
                self.assertFalse("factory:fix".startswith(vocab.prefix(name)), name)
                self.assertTrue(vocab.cmd(name, "x").startswith(vocab.UNDECLARED))
            self.assertEqual(vocab.cmd("review-low"), "factory:review-low")   # a declared one still resolves
        finally:
            config.set_team(before)

    def test_cli_validate(self):
        run = subprocess.run(["uv", "run", "--quiet", str(ROOT / "cli.py"), "validate"], capture_output=True,
                             text=True, env={**os.environ, "FACTORY_TEAM": str(self.root)})
        self.assertEqual((run.returncode, run.stdout.strip()), (0, "factory: ok"), run.stderr)

    def test_cli_validate_rejects_redundant_label_group_unlabel(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["ready"]["effects"].extend(
            [{"unlabel": "risk:high"}, {"label": "risk:low"}]))
        run = subprocess.run(["uv", "run", "--quiet", str(ROOT / "cli.py"), "validate"], capture_output=True,
                             text=True, env={**os.environ, "FACTORY_TEAM": str(self.root)})
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("nodes.intake.ready", run.stdout)
        self.assertIn("keep only the label", run.stdout)


class SlopBugPathTest(unittest.TestCase):
    """#161: a bug report goes triage -> investigating, then to in-dev, not-actionable or needs-answers."""

    def run_flow(self, outcome, reason):
        import config
        import gate
        import nodes as nodes_mod
        from gate_port import MemoryTracker
        before = config._team
        config.set_team(ROOT / "tests/fixtures/slop")
        try:
            n = nodes_mod.load()
            t = MemoryTracker({"7": {"stage": "triage"}})
            self.assertTrue(gate.apply("intake", "bug", {"ticket": "7"}, t, n)[1]["applied"])
            self.assertEqual(t.facts("7")["stage"], "investigating")
            code, _ = gate.apply("investigate", outcome, {"ticket": "7", "reason": reason}, t, n)
            self.assertEqual(code, gate.GO)
            return t
        finally:
            config.set_team(before)

    def test_a_bug_with_a_failing_test_goes_to_in_dev(self):
        t = self.run_flow("bug", "[factory] Failing test: tests/test_x.py::test_empty; cause: x.py:10")
        self.assertEqual(t.facts("7")["stage"], "in-dev")
        self.assertIn("factory:implement-medium", t.facts("7")["attrs"])   # the developer starts on it
        self.assertIn("tests/test_x.py::test_empty", str(t.tickets["7"].get("log")))

    def test_intended_behaviour_is_not_actionable_with_the_evidence(self):
        t = self.run_flow("intended", "[factory] Intended: docs/x.md:12 says empty input is rejected")
        self.assertEqual(t.facts("7")["stage"], "not-actionable")
        self.assertIn("docs/x.md:12", str(t.tickets["7"].get("log")))

    def test_cannot_reproduce_asks_the_reporter(self):
        t = self.run_flow("cannot-reproduce", "[factory] Not reproduced. 1. Which version?")
        self.assertEqual(t.facts("7")["stage"], "needs-answers")


if __name__ == "__main__":
    unittest.main()

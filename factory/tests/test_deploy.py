#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/deploy.py. Run: uv run factory/tests/test_deploy.py
Regenerate the fixture: UPDATE_FIXTURE=1 uv run factory/tests/test_deploy.py"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
cfg.set_team(Path(__file__).resolve().parent / "fixtures/team")
import deploy
import gate_github
import nodes

FIX = HERE / "fixtures/deploy"


FAKE_SERVER = r"""
import json, sys
state, kind = sys.argv[1], sys.argv[2]  # state file: {"items": [...], "calls": [...]}
TOOLS = {"automation": {"list_automations", "get_automation", "create_automation", "update_automation", "delete_automation"},
         "environment": {"list_environments", "get_environment", "create_environment", "edit_environment", "delete_environment"}}
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    db = json.load(open(state))
    if m["method"] == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}}
    else:
        tool, a = m["params"]["name"], m["params"]["arguments"]
        db["calls"].append([tool, a])
        if tool not in TOOLS[kind]:
            raise ValueError(f"unexpected {kind} tool: {tool}")
        if tool == "list_automations":
            assert a == {"privacy": "private"}, a
            out = {"personal": [{"automationId": i["id"], "name": i["name"]} for i in db["items"]],
                   "project": []}
        elif tool == "list_environments":
            assert list(a) == ["repositoryUrl"] and a["repositoryUrl"].startswith("https://github.com/"), a
            out = [{"id": i["id"], "name": i["name"]} for i in db["items"]]
        elif tool == "get_automation":
            i = next(i for i in db["items"] if i["id"] == a["automationId"])
            out = {**{k: v for k, v in i.items() if k != "id"}, "automationId": i["id"],
                   "triggers": [{**t, "triggerId": "t", "isEnabled": True, "automationSnapshotId": "s"}
                                for t in i.get("triggers", [])]}
        elif tool == "get_environment":
            out = next(i for i in db["items"] if i["id"] == a["id"])
        elif tool.startswith("create_"):
            db["items"].append({**a, "id": f"{kind}-{len(db['items'])}"})
            out = {}
        elif tool in ("update_automation", "edit_environment"):
            key = "automationId" if kind == "automation" else "id"
            db["items"] = [{**i, **{k: v for k, v in a.items() if k != key}} if i["id"] == a[key] else i for i in db["items"]]
            out = {}
        else:
            db["items"] = [i for i in db["items"] if i["id"] != a.get("automationId", a.get("id"))]
            out = {}
        res = {"content": [{"type": "text", "text": json.dumps(out)}]}
    json.dump(db, open(state, "w"))
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}), flush=True)
"""


RECORDED_SERVER = r"""
import json, os, sys
rec = json.load(open(sys.argv[1]))
server = sys.argv[2]  # air-automations or air-cloud
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        res = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}}
    else:
        tool, a = m["params"]["name"], m["params"]["arguments"]
        if tool == "get_environment":
            recorded = rec.get("air-cloud.get_environment", {}).get(a["id"])
            if recorded:
                res = recorded
            else:  # Older recordings only contain the list response.
                items = json.loads(rec["air-cloud.list_environments"]["content"][0]["text"])["items"]
                res = {"content": [{"type": "text", "text": json.dumps(next(i for i in items if i["id"] == a["id"]))}]}
        else:
            res = rec[f"{server}.{tool}"]
            if os.environ.get("WRAP_STRUCTURED"):  # a server that also wraps the text in structuredContent
                res = {**res, "structuredContent": {"result": res["content"][0]["text"]}}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": res}), flush=True)
"""


class RecordedTest(unittest.TestCase):
    """`read_state` on responses recorded from the real Air MCP servers: tools/call text content holding JSON."""

    def test_read_state_decodes_recorded_responses(self):
        self.read_recorded()

    def test_read_state_ignores_wrapped_structured_content(self):
        with mock.patch.dict(os.environ, {"WRAP_STRUCTURED": "1"}):
            self.read_recorded()

    def test_unchanged_environment_is_unchanged(self):
        contract = json.loads((HERE / "contracts/air-mcp.json").read_text())
        responses = contract["servers"]["air-cloud"]["responses"]
        plan = deploy.plan_air(nodes.load(), {**cfg.load(), "team_repository":
                                              "https://github.com/zaharchenko-evgeny/slop-eval-factory"})
        plan = {"prefix": plan["prefix"], "environments": plan["environments"], "automations": []}
        envs = [deploy.environment_detail(json.loads(responses[f"get_environment:{name}"]["content"][0]["text"]))
                for name in ("read", "write", "ops")]
        self.assertEqual(deploy.diff(plan, [], envs), [])

    def read_recorded(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "server.py").write_text(RECORDED_SERVER)
        servers = [deploy.Mcp(f"{sys.executable} {tmp / 'server.py'} {FIX / 'mcp-recorded.json'} {k}")
                   for k in ("air-automations", "air-cloud")]
        try:
            automations, envs = deploy.read_state(*servers, "[Slop factory] ", "https://github.com/zaharchenko-evgeny/offline-eval-factory")
        finally:
            for s in servers:
                s.close()
        self.assertEqual([a["id"] for a in automations], ["b007905e-dad5-4ce9-b5aa-f8788b0dc100"] * 2)
        self.assertEqual(automations[0]["triggers"], [{"triggerType": "SCHEDULED", "cronExpression": "0 6,18 * * *", "timezone": "UTC"}])
        self.assertEqual([e["name"] for e in envs], ["[Slop factory] default", "[Slop factory] read", "[Slop factory] write", "[Slop factory] ops"])


class ApplyTest(unittest.TestCase):
    """`deploy --apply` against a fake MCP server over stdio that records the calls."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "server.py").write_text(FAKE_SERVER)
        self.plan = {"prefix": "[F] ", "environments": [{"name": "[F] read", "startup": "x", "repository": "https://github.com/o/r"}],
                     "automations": [{"name": "[F] a", "prompt": "new", "environmentConfigId": "[F] read"}]}
        self.state = {k: self.tmp / f"{k}.json" for k in ("automation", "environment")}

    def seed(self, automations, envs):
        for k, items in (("automation", automations), ("environment", envs)):
            self.state[k].write_text(json.dumps({"items": items, "calls": []}))

    def run_deploy(self, mode, allow_delete=False):
        servers = [deploy.Mcp(f"{sys.executable} {self.tmp / 'server.py'} {self.state[k]} {k}") for k in self.state]
        try:
            return deploy.deploy(mode, allow_delete, self.plan, *servers)
        finally:
            for s in servers:
                s.close()

    def calls(self, kind):
        return [c for c in json.loads(self.state[kind].read_text())["calls"]
                if c[0].startswith(("create_", "update_", "edit_", "delete_"))]

    def test_fake_server_matches_cached_tool_names(self):
        self.seed([{"id": "other", "name": "[Other] automation"},
                   {"id": "a1", "name": "[F] a", "prompt": "new", "environmentConfigId": "e1"}],
                  [{"id": "other-env", "name": "[Other] environment"},
                   {"id": "e1", "name": "[F] read", "startup": "x", "repository": "https://github.com/o/r"}])
        automations, cloud = [deploy.Mcp(f"{sys.executable} {self.tmp / 'server.py'} {self.state[k]} {k}")
                              for k in self.state]
        try:
            deploy.read_state(automations, cloud, "[F]", "https://github.com/o/r")
        finally:
            automations.close()
            cloud.close()
        calls = {k: [x[0] for x in json.loads(self.state[k].read_text())["calls"]] for k in self.state}
        self.assertEqual(calls["automation"], ["list_automations", "get_automation"])
        self.assertEqual(calls["environment"], ["list_environments", "get_environment"])
        details = {k: [x[1].get("id", x[1].get("automationId")) for x in json.loads(self.state[k].read_text())["calls"] if x[0].startswith("get_")]
                   for k in self.state}
        self.assertEqual(details, {"automation": ["a1"], "environment": ["e1"]})

    def test_apply_updates_in_place(self):
        self.plan["automations"][0]["spec"] = {"node": "intake"}
        self.seed([{"id": "a1", "name": "[F] a", "prompt": "old", "environmentConfigId": "e1"}],
                  [{"id": "e1", "name": "[F] read", "startup": "x", "repository": "https://github.com/o/r"}])
        self.assertEqual(self.run_deploy("--apply"), 0)
        self.assertEqual(self.calls("automation"), [["update_automation", {
            "prompt": "new", "automationId": "a1"}]])
        self.assertNotIn("spec", self.calls("automation")[0][1])
        self.assertEqual(self.calls("environment"), [])

    def test_apply_renames_in_place(self):
        self.plan["renames"] = {"[F] old": "[F] a"}
        self.seed([{"id": "a1", "name": "[F] old", "prompt": "new", "environmentConfigId": "e1"},
                   {"id": "a3", "name": "[Other] keep"}],
                  [{"id": "e1", "name": "[F] read", "startup": "x", "repository": "https://github.com/o/r"}])
        self.assertEqual(self.run_deploy("--apply", allow_delete=True), 0)
        self.assertEqual(self.calls("automation"), [["update_automation", {
            "name": "[F] a", "automationId": "a1"}]])

    def test_apply_edits_environment(self):
        self.seed([{"id": "a1", "name": "[F] a", "prompt": "new", "environmentConfigId": "e1"}],
                  [{"id": "e1", "name": "[F] read", "startup": "old", "repository": "https://github.com/o/r"}])
        self.assertEqual(self.run_deploy("--apply"), 0)
        self.assertEqual(self.calls("environment"), [["edit_environment", {"startup": "x", "id": "e1"}]])

    def test_environment_update_preserves_unplanned_live_domains(self):
        self.plan["environments"][0]["vm"] = "new"
        self.seed([], [{"id": "e1", "name": "[F] read", "startup": "x",
                        "repository": "https://github.com/o/r", "vm": "old",
                        "domains": ["github.com", "*.blob.core.windows.net"]}])
        self.assertEqual(self.run_deploy("--apply"), 0)
        self.assertEqual(self.calls("environment"), [["edit_environment", {"vm": "new", "id": "e1"}]])
        live = json.loads(self.state["environment"].read_text())["items"][0]
        self.assertEqual(live["domains"], ["github.com", "*.blob.core.windows.net"])

    def test_main_rename_flag_updates_in_place(self):
        self.seed([{"id": "a1", "name": "[Slop factory] old-intake"}], [])
        env = {"FACTORY_MCP_AIR_AUTOMATIONS": f"{sys.executable} {self.tmp / 'server.py'} {self.state['automation']} automation",
               "FACTORY_MCP_AIR_CLOUD": f"{sys.executable} {self.tmp / 'server.py'} {self.state['environment']} environment"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
            self.assertEqual(deploy.main(["--dry-run", "--rename", "[Slop factory] old-intake=[Slop factory] intake"]), 0)
            self.assertEqual(deploy.main(["--dry-run", "--rename", "nope"]), 2)
        lines = out.getvalue().splitlines()
        self.assertTrue(any(l.startswith("update automation [Slop factory] intake") for l in lines))
        self.assertNotIn("delete automation [Slop factory] old-intake", lines)
        self.assertNotIn("create automation [Slop factory] intake", lines)

    def test_apply_refuses_delete_without_flag(self):
        self.seed([{"id": "a1", "name": "[F] a", "prompt": "new", "environmentConfigId": "e1"},
                   {"id": "a2", "name": "[F] gone"}, {"id": "a3", "name": "[Other] keep"}],
                  [{"id": "e1", "name": "[F] read", "startup": "x", "repository": "https://github.com/o/r"}])
        self.assertEqual(self.run_deploy("--apply"), 1)
        self.assertEqual(self.calls("automation"), [])
        self.assertEqual(self.run_deploy("--apply", allow_delete=True), 0)
        self.assertEqual(self.calls("automation"), [["delete_automation", {"automationId": "a2"}]])

    def test_apply_verifies_after_write(self):
        self.seed([], [])
        self.assertEqual(self.run_deploy("--dry-run"), 0)
        self.assertEqual(self.calls("environment") + self.calls("automation"), [])
        self.assertEqual(self.run_deploy("--apply"), 0)
        self.assertEqual([c[0] for c in self.calls("environment")], ["create_environment"])
        self.assertEqual(self.run_deploy("--apply"), 0)  # now unchanged: no further write
        self.assertEqual(len(self.calls("automation")), 1)
        # the server drops the update: the verify step reports the difference
        self.seed([{"id": "a1", "name": "[F] a", "prompt": "old", "environmentConfigId": "e1"}],
                  [{"id": "e1", "name": "[F] read", "startup": "x", "repository": "https://github.com/o/r"}])
        (self.tmp / "server.py").write_text(FAKE_SERVER.replace('{**i, **{k: v for k, v in a.items() if k != key}}', '{**i}'))
        self.assertEqual(self.run_deploy("--apply"), 1)

    def test_main_closes_reminder_after_successful_changed_apply(self):
        plan = {"prefix": "[F] ", "environments": [], "automations": [], "files": {}}
        servers = [mock.Mock(), mock.Mock()]
        changed = [("update", "automation", "a", None, None, [])]
        writes = []

        def run(args):
            writes.append(args)
            if args[:2] == ["api", "--paginate"]:
                return json.dumps({"number": 42, "title": "Deploy needed"})
            return "{}"

        tracker = gate_github.GitHubGateTracker("o/r", {}, "bot", run=run)
        with (mock.patch.dict(os.environ, {"FACTORY_MCP_AIR_AUTOMATIONS": "automation",
                                          "FACTORY_MCP_AIR_CLOUD": "cloud"}),
              mock.patch.object(deploy.config_mod, "pop_team", side_effect=lambda argv: argv),
              mock.patch.object(deploy, "plan_air", return_value=plan),
              mock.patch.object(deploy, "Mcp", side_effect=servers),
              mock.patch.object(deploy, "read_state", return_value=([], [])),
              mock.patch.object(deploy, "diff", side_effect=[changed, []]),
              mock.patch.object(deploy, "apply_diff") as apply,
              mock.patch.object(deploy, "write_files", return_value=0),
              mock.patch("gate_github.GitHubGateTracker.from_config", return_value=tracker)):
            self.assertEqual(deploy.main(["--apply"]), 0)
        apply.assert_called_once()
        self.assertTrue(any(x[:2] == ["issue", "close"] and "42" in x for x in writes))
        self.assertTrue(any(x[:2] == ["issue", "comment"] and "factory:deploy-applied" in " ".join(x) for x in writes))


class PlanTest(unittest.TestCase):
    def test_air_matches_fixture(self):
        plan = deploy.plan_air(yaml.safe_load((FIX / "nodes.yaml").read_text()),
                               yaml.safe_load((FIX / "factory.yaml").read_text()))
        got = json.dumps(plan, indent=2, sort_keys=True) + "\n"
        if os.environ.get("UPDATE_FIXTURE"):
            (FIX / "air-plan.json").write_text(got)
        self.assertEqual(got, (FIX / "air-plan.json").read_text())

    def test_environments_render_team_repository(self):
        url = "https://github.com/o/team"
        plan = deploy.plan_air(nodes.load(), {**cfg.load(), "team_repository": url})
        self.assertTrue(plan["environments"])
        for e in plan["environments"]:
            self.assertEqual(e["variables"], {"FACTORY_ENV": e["variables"]["FACTORY_ENV"], "FACTORY_TEAM_REPOSITORY": url})
        plain = deploy.plan_air(nodes.load(), {**cfg.load(), "team_repository": ""})
        self.assertTrue(all("FACTORY_TEAM_REPOSITORY" not in e["variables"] for e in plain["environments"]))

    def test_automations_have_no_additional_repositories(self):
        plan = deploy.plan_air(nodes.load(), {**cfg.load(), "team_repository": "https://github.com/o/team"})
        self.assertTrue(all("additionalRepositories" not in a for a in plan["automations"]))

    def test_nodes_get_environment_by_env(self):
        plan = deploy.plan_air(nodes.load(), cfg.load())
        env = {x["name"]: x["environmentConfigId"] for x in plan["automations"]}
        for name, want in (("intake", "read"), ("join", "read"), ("finish", "read"), ("review-full", "read"),
                           ("implement-mid", "write"), ("fix-cheap", "write"), ("update-strong", "write")):
            self.assertEqual(env[f"[Slop factory] {name}"], f"[Slop factory] {want}")
        self.assertEqual({e["name"]: e["variables"] for e in plan["environments"]},
                         {"[Slop factory] read": {"FACTORY_ENV": "read"}, "[Slop factory] write": {"FACTORY_ENV": "write"},
                          "[Slop factory] ops": {"FACTORY_ENV": "ops"}})  # ops: only a disabled node, still planned

    def test_local_target_is_rejected(self):
        self.assertEqual(deploy.main(["plan", "--target", "local"]), 2)

    def test_plan_lists_every_label_the_gate_writes(self):
        plan = deploy.plan_air(nodes.load(), cfg.load())
        for want in ("factory:ready", "factory:implement-cheap", "factory:fix-strong", "complexity:small", "risk:high"):
            self.assertIn(want, plan["labels"])
        self.assertFalse([x for x in plan["labels"] if "<" in x])

    def test_script_nodes_only_run_the_sweep(self):
        plan = json.loads((FIX / "air-plan.json").read_text())
        a = {x["name"]: x for x in plan["automations"]}
        self.assertIn("Run `factory sweep` in the foreground", a["[Slop factory] sweep"]["prompt"])
        self.assertIn("`factory begin intake --issue <the issue number>`", a["[Slop factory] intake"]["prompt"])

    def test_every_script_prompt_waits_for_command_exit(self):
        plans = (deploy.plan_air(nodes.load(), cfg.load()), json.loads((FIX / "air-plan.json").read_text()))
        for plan in plans:
            for automation in plan["automations"]:
                if automation["name"].startswith(("[Slop factory] sweep", "[Slop factory] join",
                                                   "[Slop factory] finish", "[Slop factory] merge",
                                                   "[Slop factory] reopen",
                                                   "[Slop factory] notify")):
                    self.assertIn("wait until it exits", automation["prompt"])

    def test_no_prompt_asks_for_json(self):
        plans = (deploy.plan_air(nodes.load(), cfg.load()), json.loads((FIX / "air-plan.json").read_text()))
        for plan in plans:
            for x in plan["automations"]:
                prompt = x["prompt"].replace("never write the payload or any JSON", "")
                for word in ("--event", "stdin", "heredoc", "payload"):
                    self.assertNotIn(word, prompt, x["name"])
            prompts = {x["name"]: x["prompt"] for x in plan["automations"]}
            self.assertIn("`factory begin implement-mid --issue <the issue number>`", prompts["[Slop factory] implement-mid"])

    def test_pull_request_nodes_begin_with_the_pr_number(self):
        a = {x["name"]: x["prompt"] for x in deploy.plan_air(nodes.load(), cfg.load())["automations"]}
        self.assertIn("`factory begin fix-mid --pr <the pull request number>`", a["[Slop factory] fix-mid"])

    def test_review_automations_trigger_on_focus_rerequest_labels(self):
        for a in json.loads((FIX / "air-plan.json").read_text())["automations"]:
            if a["name"].startswith("[Slop factory] review-"):
                self.assertIn({"triggerType": "EVENT", "source": "github", "eventType": "pull_request.labeled",
                               "filters": {"label": f"factory:rereview-{a['name'].rsplit('-', 1)[1]}",
                                           "pr_author": "jetbrains-air[bot]"}}, a["triggers"])

    def test_reconcile_is_an_hourly_air_schedule_only(self):
        plan = deploy.plan_air(nodes.load(), cfg.load())
        self.assertNotIn("[Slop factory] recover", {x["name"] for x in plan["automations"]})
        a = next(x for x in plan["automations"] if x["name"] == "[Slop factory] reconcile")
        # no event wake: one pass reads every active ticket, so label bursts must not start it
        self.assertEqual(a["triggers"], [{"triggerType": "SCHEDULED", "cronExpression": "17 * * * *", "timezone": "UTC"}])
        self.assertEqual(a["triggerVcs"], [{"outputBehaviour": "read-only"}])
        self.assertEqual(a["environmentConfigId"], "[Slop factory] ops")
        self.assertIn("Run `factory reconcile` in the foreground", a["prompt"])
        self.assertIn("wait until it exits", a["prompt"])
        self.assertEqual(plan["files"], {})
        self.assertFalse((ROOT / ".github/workflows/factory-reconcile.yml").exists())

    def test_deploy_writes_no_file_outside_the_factory_repository(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(deploy.config_mod, "team_root", return_value=Path(d)):
            with contextlib.redirect_stdout(io.StringIO()):
                deploy.write_files({".github/workflows/factory-reconcile.yml": "x"}, True)
            self.assertEqual([str(p.relative_to(d)) for p in Path(d).rglob("*") if p.is_file()],
                             [".github/workflows/factory-reconcile.yml"])

    def test_air_shape_and_no_github_token(self):
        plan = json.loads((FIX / "air-plan.json").read_text())
        a = {x["name"]: x for x in plan["automations"]}
        self.assertEqual(a["[Slop factory] sweep"]["triggers"],
                         [{"triggerType": "SCHEDULED", "cronExpression": "*/10 * * * *", "timezone": "UTC"}])
        t = a["[Slop factory] intake"]["triggers"][0]
        self.assertEqual((t["triggerType"], t["source"]), ("EVENT", "github"))
        self.assertEqual(set(a["[Slop factory] intake"]["agentAndModel"]), {"id", "name", "isDefault", "agentId"})
        self.assertEqual(set(a["[Slop factory] intake"]["repository"]), {"fullName", "cloneUrl", "browseUrl", "provider"})
        self.assertEqual(a["[Slop factory] intake"]["triggerVcs"], [{"outputBehaviour": "read-only"}])
        for e in deploy.plan_air(nodes.load(), cfg.load())["environments"]:
            self.assertNotIn("GITHUB_TOKEN", e["secrets"])
            if e["name"] in ("[Slop factory] read", "[Slop factory] write"):
                self.assertIn("*.blob.core.windows.net", e["domains"])
        join = next(x for x in deploy.plan_air(nodes.load(), cfg.load())
                    ["automations"] if x["name"] == "[Slop factory] join")
        self.assertEqual([(x["eventType"], x["filters"].get("label")) for x in join["triggers"]],
                         [("pull_request_review.submitted", None), ("pull_request.labeled", "factory:ci-red")])
        reopen = next(x for x in deploy.plan_air(nodes.load(), cfg.load())
                       ["automations"] if x["name"] == "[Slop factory] reopen")
        self.assertEqual([(x["eventType"], x["filters"].get("label"), x["filters"].get("pr_author"))
                          for x in reopen["triggers"]],
                         [("pull_request.synchronize", None, "jetbrains-air[bot]"),
                          ("pull_request.labeled", "factory:ci-red", "jetbrains-air[bot]")])
        assert not any(x["triggerType"] == "SCHEDULED" for a in deploy.plan_air(nodes.load(), cfg.load())["automations"] if a["name"] not in ("[Slop factory] retro", "[Slop factory] distill", "[Slop factory] doctor", "[Slop factory] doctor-write", "[Slop factory] doctor-ops", "[Slop factory] reconcile") for x in a["triggers"]), "no polling"

    def test_one_triggervcs_entry_per_trigger_and_a_spec_per_automation(self):
        for a in deploy.plan_air(nodes.load(), cfg.load())["automations"]:
            self.assertEqual(len(a["triggerVcs"]), len(a["triggers"]), a["name"])
            self.assertNotIn("```json", a["prompt"])  # no JSON in a prompt: the run compiles its spec
        a = {x["name"]: x for x in deploy.plan_air(nodes.load(), cfg.load())["automations"]}
        spec = a["[Slop factory] implement-mid"]["spec"]
        self.assertEqual((spec["node"], spec["tier"], spec["engine"]), ("implement", "mid", cfg.load()["engine"]))
        self.assertEqual(set(spec["outcomes"]), {"done", "error", "blocked", "failed", "env-broken"})
        self.assertIn("- `error`: the work needs the next tier", a["[Slop factory] implement-mid"]["prompt"])
        self.assertEqual(set(a["[Slop factory] join"]["spec"]["runs"]), {"finish", "reopen", "merge", "start", "notify"})
        notify = a["[Slop factory] notify"]
        self.assertEqual({t["filters"]["label"] for t in notify["triggers"]}, {"factory:blocked", "factory:needs-answers"})
        self.assertIn("factory notify --issue", notify["prompt"])

    def test_compile_automation_gives_the_gate_options(self):
        config, graph = cfg.load(), nodes.load()
        got = deploy.compile_automation("review-behaviour", graph, config)
        self.assertEqual((got["node"], got["focus"], got["tier"]), ("review", "behaviour", "review"))
        self.assertEqual(deploy.compile_automation("fix-mid", graph, config)["tier"], "mid")
        self.assertIsNone(deploy.compile_automation("intake", graph, config)["tier"])
        self.assertRaises(KeyError, deploy.compile_automation, "nope", graph, config)

    def test_agent_prompt_sections(self):
        a = {x["name"]: x["prompt"] for x in deploy.plan_air(nodes.load(), cfg.load())["automations"]}
        for name, role, intro in (("review-behaviour", "reviewer", "You review one branch independently"),
                                  ("implement-mid", "developer", "You implement one ticket in its worktree")):
            prompt = a[f"[Slop factory] {name}"]
            marks = [prompt.index(m) for m in (intro, "## Your skills", "## This step", "## How you start and finish")]
            self.assertEqual(marks, sorted(marks), name)
            self.assertTrue(prompt.startswith(f"You are the {role} of Slop factory"))
            self.assertIn((ROOT / f"roles/{role}/role.md").read_text().strip(), prompt)
            self.assertIn(f"`factory begin {name} --", prompt)
            self.assertIn("`factory end <handoff_path>`", prompt)
            for word in ("gate.py", "--node", "--tier", "--focus", "read `role_file`"):
                self.assertNotIn(word, prompt)
        self.assertIn("(behaviour focus)", a["[Slop factory] review-behaviour"])
        self.assertIn("- ponytail: Lazy senior dev mode", a["[Slop factory] implement-mid"])
        intake = a["[Slop factory] intake"]  # `bug` is disabled in the graph
        self.assertIn("- `grill`:", intake)
        self.assertNotIn("- `bug`:", intake)

    def test_scheduled_routine_prompts_take_no_number(self):
        a = {x["name"]: x["prompt"] for x in deploy.plan_air(nodes.load(), cfg.load())["automations"]}
        for name in ("retro", "distill"):
            prompt = a[f"[Slop factory] {name}"]
            self.assertIn(f"`factory begin {name}`", prompt)
            self.assertNotIn("<the issue number>", prompt)

    def test_pull_request_scripts_run_from_main(self):
        a = {x["name"]: x["prompt"] for x in deploy.plan_air(nodes.load(), cfg.load())["automations"]}
        self.assertIn("`factory sweep --main --issue <the number after the leading # of the PR title, or else the body's Closes #<n>>`", a["[Slop factory] join"])
        self.assertIn("`factory sweep --main --issue <the number after the leading # of the PR title, or else the body's Closes #<n>>`", a["[Slop factory] finish"])
        self.assertIn("`factory sweep --main --issue <the number after the leading # of the PR title, or else the body's Closes #<n>>`", a["[Slop factory] reopen"])

    def test_no_review_automation_triggers_on_ci_green(self):
        automations = deploy.plan_air(nodes.load(), cfg.load())["automations"]
        reviews = [a for a in automations if a["name"].startswith("[Slop factory] review-")]
        self.assertTrue(reviews)
        self.assertTrue(all("factory:ci-green" not in json.dumps(a["triggers"]) for a in reviews))
        self.assertTrue(all(any(t["filters"].get("label", "").startswith("factory:review-")
                               for t in a["triggers"]) for a in reviews))

    def test_event_script_nodes_scope_to_one_ticket(self):
        scripts = {"join", "start", "finish", "reopen", "merge"}
        for automation in deploy.plan_air(nodes.load(), cfg.load())["automations"]:
            name = automation["name"].removeprefix("[Slop factory] ")
            if name in scripts:
                self.assertIn("factory sweep --main --issue", automation["prompt"], name)
            elif "factory sweep" in automation["prompt"]:
                self.assertTrue("--issue" in automation["prompt"] or "--event" in automation["prompt"], name)

    def test_real_plan_is_prefixed_and_covers_pilot(self):
        config = cfg.load()
        n = nodes.load()
        plan = deploy.plan_air(n, config)
        names = {a["name"] for a in plan["automations"]}
        self.assertEqual(plan["prefix"], "[Slop factory] ")
        self.assertTrue(all(x.startswith(plan["prefix"]) for x in names))
        pilot = {f"[Slop factory] {x}" for x in (
            "intake", "start", "join", "finish", "reopen", "merge", "notify", "retro", "distill", "doctor", "doctor-write", "doctor-ops",
            "reconcile",
            *(f"{b}-{t}" for b in ("implement", "fix", "update") for t in ("cheap", "mid", "strong")),
            *(f"review-{f}" for f in ("full", "behaviour", "structure-security", "knowledge")))}
        self.assertEqual(names, pilot)
        self.assertEqual({n for n, v in n["nodes"].items() if v.get("pilot")},
                         {"intake", "start", "implement", "review", "join", "fix", "update", "finish", "reopen", "merge", "notify"})
        self.assertEqual(deploy.plan_air(n, config), plan)


if __name__ == "__main__":
    unittest.main()

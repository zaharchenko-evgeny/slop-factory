#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Tests for factory/deploy.py. Run: uv run factory/tests/test_deploy.py
Regenerate the fixture: UPDATE_FIXTURE=1 uv run factory/tests/test_deploy.py"""
import base64
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "factory"))
sys.path.insert(0, str(HERE))
import config as cfg  # noqa: E402
cfg.set_team(Path(__file__).resolve().parent / "fixtures/team")
import deploy
from factory_plugins.air import deploy as air  # noqa: E402
import nodes
from factory_plugins.github import tracker as github_tracker  # noqa: E402
from factory_plugins.github import wake as github_wake  # noqa: E402
from factory_plugins.youtrack import provision as youtrack_provision  # noqa: E402
from factory_plugins.youtrack import wake as youtrack_wake  # noqa: E402
import definition  # noqa: E402
import workflow  # noqa: E402

FIX = HERE / "fixtures/deploy"



def definition_plan(edit_factory=None, edit_workflow=None, source="fixtures/slop"):
    """The Air plan of a copy of a definition fixture, after `edit_factory` / `edit_workflow` changed its parsed
    factory.yaml / workflow.yaml (ADR-0021)."""
    before = cfg.team_root()
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copytree(HERE / source, tmp, dirs_exist_ok=True)
        for name, edit in ((definition.FACTORY, edit_factory), (definition.WORKFLOW, edit_workflow)):
            if edit:
                data = yaml.safe_load((Path(tmp) / name).read_text())
                edit(data)
                (Path(tmp) / name).write_text(yaml.safe_dump(data, sort_keys=False))
        cfg.set_team(tmp)
        try:
            return air.plan_air(nodes.load(), cfg.load())
        finally:
            cfg.set_team(before)


def plan_labels(plan):
    """Every label of a plan's `label_plan`, in any repository."""
    return sorted({x for labels in plan["label_plan"].values() for x in labels})


def youtrack_plan(edit, edit_nodes=None):
    """The Air plan of fixtures/slop's definition config after `edit` made it a YouTrack team (and `edit_nodes`
    changed its compiled nodes): the compiled config (states, commands, tracker) is where the stage and command
    stores live (ADR-0021)."""
    before = cfg.team_root()
    cfg.set_team(HERE / "fixtures/slop")
    try:
        conf = copy.deepcopy(cfg.load())
        conf["tracker"] = {"adapter": "youtrack", "url": "https://y.example", "project": "LME"}
        edit(conf)
        compiled = copy.deepcopy(nodes.load())
        if edit_nodes:
            edit_nodes(compiled)
        return air.plan_air(compiled, conf)
    finally:
        cfg.set_team(before)


def tag_store(conf):
    conf["states"]["ready"]["store"] = {"tag": "factory:ready"}
    conf["commands"]["implement-medium"]["store"] = {"tag": "factory:implement-medium"}


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
        if kind == "environment" and tool in ("create_environment", "edit_environment") and "variables" in a:
            assert isinstance(a["variables"], list) and all(v.get("value") for v in a["variables"]), a
        if tool == "list_automations":
            assert a == {"privacy": "private"}, a
            out = {"personal": [{"automationId": i["id"], "name": i["name"]} for i in db["items"]],
                   "project": []}
        elif tool == "list_environments":
            assert list(a) == ["repositoryUrl"] and a["repositoryUrl"].startswith("https://github.com/"), a
            out = [{"id": i["id"], "name": i["name"]} for i in db["items"]
                   if i.get("repository", a["repositoryUrl"]) == a["repositoryUrl"]]
        elif tool == "get_automation":
            i = next(i for i in db["items"] if i["id"] == a["automationId"])
            out = {**{k: v for k, v in i.items() if k != "id"}, "automationId": i["id"],
                   "triggers": [{**t, "triggerId": "t", "isEnabled": True, "automationSnapshotId": "s"}
                                for t in i.get("triggers", [])]}
        elif tool == "get_environment":
            out = next(i for i in db["items"] if i["id"] == a["id"])
        elif tool.startswith("create_"):
            assert kind != "environment" or all(i["name"] != a["name"] for i in db["items"]), a  # Air names are unique
            created = {**{("repository" if k == "repositoryUrl" else k): v for k, v in a.items()},
                       "id": f"{kind}-{len(db['items'])}"}
            db["items"].append(created)
            out = {"automationId": created["id"]} if kind == "automation" else {"id": created["id"]}
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

    def test_settings_environments_remove_team_repository_variable_and_replace_moved_ops(self):
        contract = json.loads((HERE / "contracts/air-mcp.json").read_text())
        responses = contract["servers"]["air-cloud"]["responses"]
        plan = air.plan_air(nodes.load(), {**cfg.load(), "team_repository":
                                              "https://github.com/zaharchenko-evgeny/slop-eval-factory"})
        plan = {"prefix": plan["prefix"], "environments": plan["environments"], "automations": []}
        envs = [air.environment_detail(json.loads(responses[f"get_environment:{name}"]["content"][0]["text"]))
                for name in ("read", "write", "ops")]
        changes = air.diff(plan, [], envs)
        by_name = {change[2]: change for change in changes}
        self.assertEqual(by_name["[Slop factory] ops"][5], ["repository"])
        for name in ("read", "write"):
            change = by_name[f"[Slop factory] {name}"]
            self.assertEqual((change[0], change[5]), ("update", ["variables", "domains"]))  # + FACTORY_ENV_REPOSITORY
            self.assertIn("FACTORY_TEAM_REPOSITORY", change[4]["variables"])

    def read_recorded(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "server.py").write_text(RECORDED_SERVER)
        servers = [air.Mcp(f"{sys.executable} {tmp / 'server.py'} {FIX / 'mcp-recorded.json'} {k}")
                   for k in ("air-automations", "air-cloud")]
        try:
            automations, envs = air.read_state(*servers, "[Slop factory] ", "https://github.com/zaharchenko-evgeny/offline-eval-factory")
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

    def run_deploy(self, mode, allow_delete=False, backend=None):
        servers = [air.Mcp(f"{sys.executable} {self.tmp / 'server.py'} {self.state[k]} {k}") for k in self.state]
        try:
            return air.deploy(mode, allow_delete, self.plan, *servers, backend=backend)
        finally:
            for s in servers:
                s.close()

    def test_dry_run_lists_declared_secret_missing_in_air_and_succeeds(self):
        self.plan["environments"][0]["secrets"] = ["HAVE", "YOUTRACK_TOKEN"]
        self.seed([], [{"id": "e1", "name": "[F] read", "startup": "x", "repository": "https://github.com/o/r",
                        "personalSecrets": [{"key": "HAVE", "description": "", "secretVersion": 1, "value": "***"}]}])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_deploy("--dry-run"), 0)
        self.assertIn("set by a person: read secret YOUTRACK_TOKEN", out.getvalue())
        self.assertNotIn("secret HAVE", out.getvalue())

    def test_extra_personal_secret_plans_no_environment_update(self):
        self.plan["environments"][0]["secrets"] = ["HAVE", "MISSING"]
        self.seed([], [{"id": "e1", "name": "[F] read", "startup": "x", "repository": "https://github.com/o/r",
                        "personalSecrets": [{"key": "HAVE"}, {"key": "PERSONAL_EXTRA"}]}])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_deploy("--dry-run"), 0)
        self.assertNotIn("(secrets)", out.getvalue())
        self.assertIn("set by a person: read secret MISSING", out.getvalue())

    def test_apply_leaves_variable_air_cannot_delete_to_a_person_and_succeeds(self):
        self.plan["environments"][0]["variables"] = {"FACTORY_ENV": "read"}
        self.seed([], [{"id": "e1", "name": "[F] read", "startup": "x", "repository": "https://github.com/o/r",
                        "variables": [{"key": "FACTORY_ENV", "value": "read"},
                                      {"key": "FACTORY_TEAM_REPOSITORY", "value": "o/r"}]}])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.run_deploy("--apply"), 0)
        self.assertIn("remove by hand in Air: [F] read variable FACTORY_TEAM_REPOSITORY", out.getvalue())
        self.assertEqual(self.calls("environment"), [])

    def backend(self):
        path, backend = self.state["automation"], mock.Mock()
        backend.calls = []
        backend.get_automation.side_effect = lambda automation_id: next(
            item for item in json.loads(path.read_text())["items"] if item["id"] == automation_id)
        def set_additional(automation_id, repositories):
            backend.calls.append((automation_id, repositories))
            state = json.loads(path.read_text())
            state["items"] = [{**item, "additionalRepositories": repositories} if item["id"] == automation_id else item
                               for item in state["items"]]
            path.write_text(json.dumps(state))
        backend.set_additional.side_effect = set_additional
        return backend

    def calls(self, kind):
        return [c for c in json.loads(self.state[kind].read_text())["calls"]
                if c[0].startswith(("create_", "update_", "edit_", "delete_"))]

    def test_fake_server_matches_cached_tool_names(self):
        self.seed([{"id": "other", "name": "[Other] automation"},
                   {"id": "a1", "name": "[F] a", "prompt": "new", "environmentConfigId": "e1"}],
                  [{"id": "other-env", "name": "[Other] environment"},
                   {"id": "e1", "name": "[F] read", "startup": "x", "repository": "https://github.com/o/r"}])
        automations, cloud = [air.Mcp(f"{sys.executable} {self.tmp / 'server.py'} {self.state[k]} {k}")
                              for k in self.state]
        try:
            air.read_state(automations, cloud, "[F]", "https://github.com/o/r")
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

    def test_apply_writes_changed_repositories_and_skips_unchanged(self):
        wanted = [{"repository": {"fullName": "o/team"}, "branch": "main"}]
        env = [{"id": "e1", "name": "[F] read", "startup": "x", "repository": "https://github.com/o/r"}]
        cases = [("create", [], [], "automation-0", True),
                 ("update", [{"id": "a1", "name": "[F] a", "prompt": "new", "environmentConfigId": "e1",
                              "additionalRepositories": [{"repository": {"fullName": "o/old"}, "branch": "main"}]}],
                  env, "a1", True),
                 ("unchanged", [{"id": "a1", "name": "[F] a", "prompt": "new", "environmentConfigId": "e1",
                                 "additionalRepositories": [{"repository": {"fullName": "o/team", "id": "extra"},
                                                             "branch": "main", "snapshotId": "extra"}]}],
                  env, "a1", False)]
        for action, existing, environments, automation_id, write in cases:
            with self.subTest(action=action):
                self.plan["automations"][0]["additionalRepositories"] = wanted
                self.seed(existing, environments)
                backend = self.backend()
                self.assertEqual(self.run_deploy("--apply", backend=backend), 0)
                live = json.loads(self.state["automation"].read_text())["items"][0]
                self.assertTrue(air.same("additionalRepositories", live["additionalRepositories"], wanted))
                if action == "unchanged":
                    self.assertEqual(live["additionalRepositories"][0]["snapshotId"], "extra")
                else:
                    self.assertEqual(live["additionalRepositories"], wanted)
                self.assertEqual(backend.calls, [(automation_id, wanted)] if write else [])
                calls = self.calls("automation")
                self.assertEqual([call[0] for call in calls], ["create_automation"] if action == "create" else [])
                if action == "create":
                    self.assertNotIn("additionalRepositories", calls[0][1])

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

    def test_apply_serializes_variables_and_removes_team_repository(self):
        self.plan["environments"][0]["variables"] = {"FACTORY_ENV": "read"}
        self.seed([], [{"id": "e1", "name": "[F] read", "startup": "x", "repository": "https://github.com/o/r",
                       "variables": {"FACTORY_ENV": "write", "FACTORY_TEAM_REPOSITORY": "https://github.com/o/team"}}])
        self.assertEqual(self.run_deploy("--apply"), 0)
        self.assertEqual(self.calls("environment"), [["edit_environment", {
            "variables": [{"key": "FACTORY_ENV", "value": "read"}], "id": "e1"}]])
        live = json.loads(self.state["environment"].read_text())["items"][0]
        self.assertEqual(air.environment_detail(live)["variables"], {"FACTORY_ENV": "read"})
        self.assertEqual(self.run_deploy("--dry-run"), 0)

    def test_air_environment_with_team_repository_is_not_a_change(self):
        self.plan["environments"][0]["variables"] = {"FACTORY_ENV": "read"}
        live = {"name": "[F] read", "startup": "x", "repository": "https://github.com/o/r",
                "variables": {"FACTORY_ENV": "read", "FACTORY_TEAM_REPOSITORY": "https://github.com/o/team"}}
        changes = air.diff(self.plan, [], [live])
        self.assertEqual([c for c in changes if c[1] == "environment"], [])

    def test_slop_fixture_environments_have_only_factory_env_and_its_repository(self):
        slop = HERE / "fixtures/slop"
        before = cfg.team_root()
        try:
            cfg.set_team(slop)
            plan = air.plan_air(nodes.load(), cfg.load())
        finally:
            cfg.set_team(before)
        self.assertTrue(plan["environments"])
        for e in plan["environments"]:
            self.assertEqual(set(e["variables"]), {"FACTORY_ENV", "FACTORY_ENV_REPOSITORY"})

    def test_declared_variable_is_planned_beside_factory_env(self):
        data = nodes.load()
        data["environments"]["read"] = {**data["environments"]["read"], "variables": {"YOUTRACK_URL": "https://y.example"},
                                        "secrets": ["YOUTRACK_TOKEN"]}
        env = next(e for e in air.plan_air(data, cfg.load())["environments"] if e["name"].endswith(" read"))
        self.assertEqual(env["variables"], {"YOUTRACK_URL": "https://y.example", "FACTORY_ENV": "read",
                                            "FACTORY_ENV_REPOSITORY": "zaharchenko-evgeny/slop-factory"})
        self.assertEqual(env["secrets"], ["YOUTRACK_TOKEN"])

    def test_apply_sends_trigger_destinations_with_changed_triggers(self):
        manual = {"triggerType": "WEBHOOK"}
        label = {"triggerType": "EVENT", "source": "github", "eventType": "issues.labeled"}
        self.plan["automations"][0].update({"triggers": [label], "triggerVcs": [{"outputBehaviour": "read-only"}]})
        self.seed([{"id": "a1", "name": "[F] a", "prompt": "new", "environmentConfigId": "e1", "triggers": [manual],
                    "triggerVcs": [{"outputBehaviour": "read-only"}]}],
                  [{"id": "e1", "name": "[F] read", "startup": "x", "repository": "https://github.com/o/r"}])
        self.assertEqual(self.run_deploy("--apply"), 0)
        self.assertEqual(self.calls("automation"), [["update_automation", {
            "triggers": [label], "triggerVcs": [{"outputBehaviour": "read-only"}], "automationId": "a1"}]])

    def test_apply_replaces_environment_moved_to_another_repository(self):
        self.plan["environments"][0].update({"repository": "https://github.com/o/team", "variables": {"FACTORY_ENV": "read"},
                                            "domains": ["pypi.org"]})
        self.plan["automations"][0]["repository"] = {"fullName": "o/r", "browseUrl": "https://github.com/o/r"}
        self.seed([{"id": "a1", "name": "[F] a", "prompt": "new", "environmentConfigId": "e1",
                    "repository": {"fullName": "o/r"}}],
                  [{"id": "e1", "name": "[F] read", "startup": "x", "repository": "https://github.com/o/r",
                    "domains": ["github.com"], "networkAccess": "RESTRICTED",
                    "variables": {"FACTORY_ENV": "read", "FACTORY_TEAM_REPOSITORY": "https://github.com/o/team"}}])
        self.assertEqual(self.run_deploy("--apply"), 0)
        self.assertEqual([call for call in self.calls("environment") if not call[0].startswith(("list_", "get_"))], [
            ["edit_environment", {"id": "e1", "name": "retired [F] read"}],
            ["create_environment", {"name": "[F] read", "repositoryUrl": "https://github.com/o/team",
                                    "allowedDomains": ["github.com", "pypi.org"],
                                    "variables": [{"key": "FACTORY_ENV", "value": "read"}],
                                    "networkAccess": "RESTRICTED"}]])
        live = {i["id"]: i["name"] for i in json.loads(self.state["environment"].read_text())["items"]}
        self.assertEqual(live, {"e1": "retired [F] read", "environment-1": "[F] read"})
        self.assertEqual(self.calls("automation"), [["update_automation", {"environmentConfigId": "environment-1",
                                                                         "automationId": "a1"}]])
        self.assertEqual(self.run_deploy("--dry-run"), 0)

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
        self.assertEqual(self.calls("automation"), [["delete_automation", {"automationId": "a2", "confirmName": "[F] gone"}]])

    def test_apply_leaves_an_environment_delete_to_a_person(self):
        self.seed([], [{"id": "e1", "name": "[F] read", "startup": "x", "repository": "https://github.com/o/r"},
                       {"id": "e9", "name": "[F] gone", "startup": "x", "repository": "https://github.com/o/r"}])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = self.run_deploy("--apply", allow_delete=True)
        self.assertNotIn("delete_environment", [c[0] for c in self.calls("environment")])
        self.assertIn("remove by hand in Air: environment [F] gone", err.getvalue())
        self.assertEqual(code, 0)

    def test_team_repository_automation_runs_from_the_tracker_checkout(self):
        slop, before = HERE / "fixtures/slop", cfg.team_root()
        try:
            cfg.set_team(slop)
            plan = air.plan_air(nodes.load(), cfg.load())
        finally:
            cfg.set_team(before)
        ops = [a for a in plan["automations"] if a["repository"]["fullName"].endswith("/slop-eval-factory")]
        self.assertTrue(ops)
        for a in ops:
            self.assertIn("Run every command from `/workspaces/slop-factory`", a["prompt"], a["name"])

    def test_ci_result_labels_wake_on_the_pull_request(self):
        """#790: CI adds factory:ci-green/ci-red to the PR, so their wakes are pull_request.labeled."""
        slop, before = HERE / "fixtures/slop", cfg.team_root()
        try:
            cfg.set_team(slop)
            plan = air.plan_air(nodes.load(), cfg.load())
        finally:
            cfg.set_team(before)
        a = {x["name"]: x for x in plan["automations"]}
        for name, label in (("review-low", "factory:ci-green"), ("review-high", "factory:ci-green"), ("ci-red", "factory:ci-red")):
            for repo in ("slop-factory", "slop-eval-factory"):  # one copy per ticket repository (ADR-0024)
                events = [t["eventType"] for t in a[f"[Slop factory] {name} {repo}"]["triggers"]
                          if t["filters"].get("label") == label]
                self.assertEqual(events, ["pull_request.labeled"], f"{name} {repo}")

    def test_apply_prints_webhook_registration_when_unchanged(self):
        state = ([{"name": "[F] hook", "webhooks": [{"id": "t1", "url": "https://air/hook/t1"}]}], [])
        out = io.StringIO()
        with mock.patch.object(air, "read_state", return_value=state), mock.patch.object(air, "diff", return_value=[]), \
                contextlib.redirect_stdout(out):
            code = air.deploy("--apply", False, {"prefix": "[F]", "environments": [], "automations": []}, None, None)
        self.assertEqual(code, 0)
        self.assertIn("webhook [F] hook: trigger t1 https://air/hook/t1", out.getvalue())

    def test_apply_creates_environment_with_air_arguments(self):
        self.plan["environments"][0].update({"repository": "https://github.com/o/r", "variables": {"FACTORY_ENV": "read"},
                                            "domains": ["pypi.org"]})
        self.seed([], [])
        self.assertEqual(self.run_deploy("--apply"), 0)
        created = [c for c in self.calls("environment") if c[0] == "create_environment"]
        self.assertEqual(created, [["create_environment", {"name": "[F] read", "repositoryUrl": "https://github.com/o/r",
                                                           "allowedDomains": ["pypi.org"],
                                                           "variables": [{"key": "FACTORY_ENV", "value": "read"}]}]])

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

    def test_main_apply_writes_no_tickets(self):
        plan = {"prefix": "[F] ", "environments": [], "automations": [], "files": {}}
        servers = [mock.Mock(), mock.Mock()]
        changed = [("update", "automation", "a", None, None, [])]
        with (mock.patch.dict(os.environ, {"FACTORY_MCP_AIR_AUTOMATIONS": "automation",
                                          "FACTORY_MCP_AIR_CLOUD": "cloud"}),
              mock.patch.object(deploy.config_mod, "pop_team", side_effect=lambda argv: argv),
              mock.patch.object(air, "plan_air", return_value=plan),
              mock.patch.object(air, "Mcp", side_effect=servers),
              mock.patch.object(air, "read_state", return_value=([], [])),
              mock.patch.object(air, "diff", side_effect=[changed, []]),
              mock.patch.object(air, "apply_diff") as apply,
              mock.patch.object(deploy, "write_files", return_value=0),
              mock.patch.object(deploy, "sync_wake_files", return_value=0),
              mock.patch("factory_plugins.github.tracker.GitHubTracker.from_config") as issues,
              mock.patch("factory_plugins.github.gate.GitHubGateTracker.from_config") as gate_issues):
            self.assertEqual(deploy.main(["--apply"]), 0)
        apply.assert_called_once()
        issues.assert_not_called()
        gate_issues.assert_not_called()


class CommentWakeDeployTest(unittest.TestCase):
    def test_apply_installs_files_once_in_every_configured_github_repository(self):
        self.install()

    def install(self):
        """Deploy with comments off (writes nothing), then twice with comments on (writes each file once)."""
        files = {}
        writes = []

        def gh(args):
            endpoint = next((x for x in args if x.startswith("repos/") and "/contents/" in x), None)
            if not endpoint:
                raise AssertionError(args)
            if "-X" not in args:
                if endpoint not in files:
                    raise RuntimeError("HTTP 404: Not Found")
                return json.dumps({"sha": "sha", "content": base64.b64encode(files[endpoint].encode()).decode()})
            content = next(x.split("=", 1)[1] for x in args if x.startswith("content="))
            files[endpoint] = base64.b64decode(content).decode()
            writes.append(endpoint)
            return "{}"

        settings = {"plugins": ["comments"], "repositories": [
            {"name": "main", "url": "https://github.com/o/main", "role": "primary"},
            {"name": "app", "url": "https://github.com/o/app", "role": "project"},
        ]}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(deploy.sync_wake_files({**settings, "plugins": []}, gh, True), 0)
            self.assertEqual(writes, [])  # ADR-0025: the module is off
            self.assertEqual(deploy.sync_wake_files(settings, gh, True), 0)
            self.assertEqual(deploy.sync_wake_files(settings, gh, True), 0)
        self.assertEqual(len(writes), 4)
        self.assertEqual(set(files), {
            f"repos/o/{repo}/contents/{path}"
            for repo in ("main", "app") for path in github_wake.COMMENT_WAKE_FILES
        })
        return settings, gh, files, writes

    def test_turning_comments_off_removes_the_installed_files(self):
        """#1057: a deploy with the comments plugin off deletes the comment wake files an earlier deploy installed."""
        settings, gh, files, writes = self.install()
        deletes = []

        def gh_with_delete(args):
            if "DELETE" in args:
                endpoint = next(x for x in args if x.startswith("repos/"))
                self.assertIn("sha=sha", args)
                deletes.append(endpoint)
                files.pop(endpoint)
                return "{}"
            return gh(args)
        off = {**settings, "plugins": []}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(deploy.sync_wake_files(off, gh_with_delete, False), 0)  # dry run: reports only
            self.assertEqual(deletes, [])
            self.assertEqual(deploy.sync_wake_files(off, gh_with_delete, True), 0)
            self.assertEqual(deploy.sync_wake_files(off, gh_with_delete, True), 0)  # nothing left to remove
        self.assertEqual(len(deletes), 4)
        self.assertEqual(files, {})
        self.assertIn("remove project file o/app/scripts/comment-wake-label.sh", output.getvalue())

    def test_wake_rules_follow_the_comments_plugin(self):
        self.assertEqual(github_wake.wake_rules(["comments", "github"]),
                         {"install": list(github_wake.COMMENT_WAKE_FILES), "remove": []})
        self.assertEqual(github_wake.wake_rules(["github"]),
                         {"install": [], "remove": list(github_wake.COMMENT_WAKE_FILES)})


class BackendTest(unittest.TestCase):
    def _response(self, body):
        r = mock.MagicMock()
        r.__enter__.return_value = r
        r.read.return_value = body
        return r

    def _forbidden(self):
        return air.urllib.error.HTTPError("https://backend.test/automations", 403, "Forbidden", {}, None)

    def test_set_additional_retries_the_write_while_a_new_automation_settles(self):
        """#1122: Air answers 403 to the write on an automation created a moment ago; a later try succeeds."""
        record = json.dumps({"automationId": "a1", "name": "[F] a"}).encode()
        backend = air.Backend("https://backend.test", token="t")
        with mock.patch.object(air.urllib.request, "urlopen", side_effect=[
                self._response(record), self._forbidden(), self._response(b"{}")]) as request, \
                mock.patch.object(air.time, "sleep") as sleep:
            backend.set_additional("a1", [{"repository": {"fullName": "o/team"}, "branch": "main"}])
        self.assertEqual([c.args[0].get_method() for c in request.call_args_list], ["GET", "POST", "POST"])
        self.assertEqual(sleep.call_count, 1)

    def test_set_additional_raises_a_lasting_refusal(self):
        record = json.dumps({"automationId": "a1"}).encode()
        backend = air.Backend("https://backend.test", token="t")
        with mock.patch.object(air.urllib.request, "urlopen", side_effect=[
                self._response(record), self._forbidden(), self._forbidden(), self._forbidden()]), \
                mock.patch.object(air.time, "sleep"), self.assertRaises(air.urllib.error.HTTPError):
            backend.set_additional("a1", [], attempts=3)

    def test_set_additional_preserves_the_full_automation_record(self):
        existing = {"automationId": "a1", "name": "[F] a", "repository": {"fullName": "o/project"},
                    "branch": "main", "prompt": "keep this", "environmentConfigId": "e1",
                    "agentAndModel": {"id": "model"}, "triggerVcs": [{"outputBehaviour": "create-pr"}],
                    "kind": "automation", "executor": {"type": "agent"}, "botDevSettings": {"branch": "main"},
                    "paused": False,
                    "triggers": [{"triggerId": "t1", "isEnabled": False, "automationSnapshotId": "snapshot",
                                  "triggerType": "EVENT", "source": "github", "eventType": "issues.labeled",
                                  "filters": {"label": "factory:ready"}}],
                    "outboundWebhooks": [], "mcpConnectors": [], "outputDescription": None,
                    "projectId": "project",
                    "additionalRepositories": [{"repository": {"fullName": "o/old"}, "branch": "main"}],
                    "automationSnapshotId": "server snapshot", "latestSnapshotVersion": 7,
                    "stats": {"serverField": True}}
        target = [{"repository": {"fullName": "o/team"}, "branch": "main"}]
        get_response, post_response = mock.MagicMock(), mock.MagicMock()
        get_response.__enter__.return_value = get_response
        get_response.read.return_value = json.dumps(existing).encode()
        post_response.__enter__.return_value = post_response
        post_response.read.return_value = b"{}"
        runner = mock.Mock(return_value=mock.Mock(stdout="access-token\n"))
        with mock.patch.object(air.urllib.request, "urlopen", side_effect=[get_response, post_response]) as request:
            backend = air.Backend("https://backend.test/air-automations-backend/api/v1", run=runner)
            backend.set_additional("a1", target)
        self.assertEqual(request.call_count, 2)
        get_req, post_req = (call.args[0] for call in request.call_args_list)
        self.assertEqual([(r.full_url, r.get_method()) for r in (get_req, post_req)], [
            ("https://backend.test/air-automations-backend/api/v1/automations/a1", "GET"),
            ("https://backend.test/air-automations-backend/api/v1/automations", "POST")])
        self.assertEqual(post_req.get_header("Authorization"), "Bearer access-token")
        body = json.loads(post_req.data)
        expected = {key: existing[key] for key in air.Backend.SPEC_FIELDS if key != "outputDescription"}
        expected["triggers"] = [{key: value for key, value in trigger.items()
                                  if key not in ("triggerId", "automationSnapshotId")}
                                 for trigger in existing["triggers"]]
        expected.update({"id": "a1", "additionalRepositories": target})
        self.assertEqual(body, expected)
        self.assertEqual(runner.call_args.args[0][1:], ["token", "--env", "prod", "--audience",
                                                        "air-automations-backend"])
        env = runner.call_args.kwargs["env"]
        self.assertEqual((env["JCP_MCP_API_URL"], env["JCP_MCP_CLIENT_ID"], env["JCP_MCP_REDIRECT_PATH"],
                          env["JCP_MCP_CONFIG_DIR"]),
                         ("https://api.jetbrains.cloud", "pi-lot", "/space/auth",
                          str(Path.home() / ".config/jcp-mcp-bridge-pilot")))
        with mock.patch.dict(os.environ, {"FACTORY_AIR_AUTOMATIONS_API_URL": "https://evil.example",
                                          "JCP_MCP_API_URL": "https://evil.example"}):
            self.assertEqual(air.Backend().base_url, air.Backend.API_BASE_URL)


class ProjectPlanTest(unittest.TestCase):
    def test_slop_plan_deploys_the_team_workflow(self):
        """Slop factory's definition (fixtures/slop) deploys every node of its own workflow, on their schedules,
        with every repository it lists."""
        slop, before = HERE / "fixtures/slop", cfg.team_root()
        project, wf = definition.to_project(definition.load(slop))
        try:
            cfg.set_team(slop)
            plan = air.plan_air(nodes.load(), cfg.load())
        finally:
            cfg.set_team(before)
        prefix = f"[{project['project']['name']}] "
        repo_names = [r["name"] for r in project["repositories"]]
        names = {a["name"].removeprefix(prefix).removesuffix(next((f" {r}" for r in repo_names if a["name"].endswith(f" {r}")), "")): a
                 for a in plan["automations"]}  # per-repository automations end with their repository's name
        deployed = [n for n, node in wf["nodes"].items()
                    if node.get("enabled") is not False and node["role"] != "human" and not node.get("runner")]
        self.assertTrue(deployed)
        for n in deployed:
            self.assertTrue(any(a == n or a.startswith(f"{n}-") for a in names), n)
        for a in names:
            self.assertTrue(any(a == n or a.startswith(f"{n}-") for n in deployed), a)
        for n in deployed:
            for wake in wf["nodes"][n]["wakes_on"]:
                if wake.startswith("schedule "):
                    cron = wf["schedules"][wake.removeprefix("schedule ")]["cron"]
                    self.assertIn(cron, [t.get("cronExpression") for t in names[n]["triggers"]], n)
        listed = {u.removeprefix("https://github.com/").removesuffix(".git")
                  for u in [project["team_repository"], *(r["url"] for r in project["repositories"])]}
        for a in plan["automations"]:
            used = {a["repository"]["fullName"], *(r["repository"]["fullName"] for r in a.get("additionalRepositories", []))}
            self.assertEqual(used, listed, a["name"])
        self.assertEqual({e["name"].removeprefix(prefix).split(" ")[0] for e in plan["environments"]},
                         set(project["environments"]))

    def test_slop_fixture_is_migrate_project_output(self):
        """The generator check of fixtures/slop: `factory migrate project` of the team commit in its SOURCE file
        reproduces it. CI checks that commit out into $FACTORY_SLOP_TEAM; locally, point it at a team checkout."""
        team = os.environ.get("FACTORY_SLOP_TEAM")
        if not team:
            if os.environ.get("GITHUB_ACTIONS"):
                self.fail("FACTORY_SLOP_TEAM is unset: CI must check out the commit in fixtures/slop/SOURCE")
            self.skipTest("FACTORY_SLOP_TEAM is unset")
        slop = HERE / "fixtures/slop"
        with tempfile.TemporaryDirectory() as tmp:
            written = workflow.copy_project(team, tmp)
            for path in written:
                rel = Path(path).relative_to(tmp)
                self.assertEqual((slop / rel).read_text(), Path(path).read_text(), f"fixtures/slop/{rel}")
        self.assertEqual(sorted(str(p.relative_to(slop)) for p in slop.rglob("*") if p.is_file()),
                         sorted(str(Path(p).relative_to(tmp)) for p in written))

    @staticmethod
    @contextlib.contextmanager
    def two_repository_team(web=True):
        """ADR-0024: fixtures/v2 with the ticket repositories o/api and, with `web`, o/web in its engine environment;
        the router listens to the tracker and the engine's developers write every repository. Yields its root."""
        before = cfg.team_root()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "team"
            shutil.copytree(HERE / "fixtures/v2", root)
            f, w = (yaml.safe_load((root / n).read_text()) for n in ("factory.yaml", "workflow.yaml"))
            repos = ["o/api", "o/web"] if web else ["o/api"]
            f["tracker"]["repositories"] = repos
            f["environments"]["engine"]["repositories"] = [{"repo": r} for r in repos]
            w["nodes"]["intake"]["listens"] = "tracker"
            for n in w["nodes"].values():
                if n.get("writes") and not n.get("environment"):
                    n["writes"] = "all"
            for n, d in (("factory.yaml", f), ("workflow.yaml", w)):
                (root / n).write_text(yaml.safe_dump(d, sort_keys=False))
            try:
                cfg.set_team(root)
                yield root
            finally:
                cfg.set_team(before)

    def test_issues_token_is_deployed_only_where_a_project_repository_is_foreign(self):
        """#1126: Air's GH_TOKEN writes the environment's own repository, so the copy of `engine` on o/web, the only
        project repository, needs no issues token; every environment names its repository for the gate."""
        with self.two_repository_team():
            c = cfg.load()
            c = {**c, "issues_token_env": "FACTORY_ISSUES_TOKEN",
                 "repositories": [r for r in c["repositories"] if r["url"].startswith("https://github.com/o/")]}
            plan = air.plan_air(nodes.load(), c)
        envs = {e["name"]: e for e in plan["environments"]}
        self.assertIn("FACTORY_ISSUES_TOKEN", envs["[Slop factory] engine"]["secrets"])
        self.assertNotIn("FACTORY_ISSUES_TOKEN", envs["[Slop factory] engine web"]["secrets"])
        self.assertEqual({e["variables"]["FACTORY_ENV_REPOSITORY"] for e in envs.values()
                          if e["name"].startswith("[Slop factory] engine")}, {"o/api", "o/web"})
        for e in envs.values():
            self.assertEqual(e["variables"]["FACTORY_ENV_REPOSITORY"], definition.full_name(e["repository"]), e["name"])

    def test_definition_with_two_ticket_repositories_plans_per_repository_automations(self):
        """#895, ADR-0024: a node that listens to both ticket repositories runs once per repository, with that
        repository as its main and the other one checked out beside it; the router (`listens: tracker`) stays one."""
        with self.two_repository_team():
            plan = air.plan_air(nodes.load(), cfg.load())
        by_name = {a["name"]: a for a in plan["automations"]}
        for repo, other in (("api", "web"), ("web", "api")):
            for node in ("implement-small", "implement-medium", "implement-large", "review-low", "review-high", "fix"):
                self.assertIn(f"[Slop factory] {node} {repo}", by_name)
            a = by_name[f"[Slop factory] implement-medium {repo}"]
            self.assertEqual(a["repository"]["fullName"], f"o/{repo}")
            self.assertEqual([r["repository"]["fullName"] for r in a["additionalRepositories"]], [f"o/{other}"])
            self.assertEqual(a["triggerVcs"], [{"outputBehaviour": "create-pr"}])
            self.assertEqual(by_name[f"[Slop factory] review-low {repo}"]["triggerVcs"], [{"outputBehaviour": "read-only"}])
            self.assertNotIn("repo_label", a["triggers"][0]["filters"])  # Air wakes a copy only for its own repository
        self.assertNotIn("[Slop factory] implement-medium", by_name)
        self.assertEqual([n for n in by_name if n.startswith("[Slop factory] intake")], ["[Slop factory] intake"])

    def test_every_automation_runs_in_an_environment_of_its_repository(self):
        """#1112: Air rejects an automation whose repository is not its environment's, so a copy in a second ticket
        repository (`reply-dev slop-eval-factory`) runs in that repository's variant of its environment."""
        plan = definition_plan()
        envs = {e["name"]: e for e in plan["environments"]}
        for a in plan["automations"]:
            env = envs[a["environmentConfigId"]]
            self.assertEqual(definition.full_name(env["repository"]), a["repository"]["fullName"], a["name"])
        variant = envs["[Slop factory] engine slop-eval-factory"]
        engine = envs["[Slop factory] engine"]
        own = ("name", "repository", "variables", "secrets")  # #1126: its repository, and no issues token for it
        self.assertEqual({k: v for k, v in variant.items() if k not in own},
                         {k: v for k, v in engine.items() if k not in own})
        self.assertEqual(variant["variables"], {**engine["variables"],
                                                "FACTORY_ENV_REPOSITORY": "zaharchenko-evgeny/slop-eval-factory"})

    def test_mentionable_agents_get_a_reply_node(self):
        """With the comments module on, fixtures/v2's `mentionable: true` agents (ADR-0021) deploy a reply node;
        the others do not, and no mention router deploys (#893). With it off (ADR-0025) none deploy. fixtures/slop
        has it off (slop-eval-factory#93) and deploys none."""
        def comments_on(f):
            f["plugins"] = ["comments"]

        def comments_off(f):
            f.pop("plugins", None)
            f.pop("modules", None)
        self.assertFalse({a["spec"]["node"] for a in definition_plan()["automations"]
                          if a["spec"]["node"].startswith("reply-")})
        plan = definition_plan(edit_factory=comments_on, source="fixtures/v2")
        self.assertEqual({a["spec"]["node"] for a in plan["automations"]},
                         {a["spec"]["node"] for a in definition_plan(source="fixtures/v2")["automations"]})
        nodes_ = {a["spec"]["node"] for a in plan["automations"]}
        self.assertTrue({"reply-dev", "reply-reviewer"} <= nodes_)
        self.assertFalse({"mentions", "reply-dev-cheap", "reply-router", "reply-approver"} & nodes_)
        self.assertIn("factory:comment", plan_labels(plan))

        def none_mentionable(f):
            comments_on(f)
            for spec in f["agents"].values():
                spec.pop("mentionable", None)
        for edit in (none_mentionable, comments_off):
            plan = definition_plan(edit_factory=edit, source="fixtures/v2")
            nodes_ = {a["spec"]["node"] for a in plan["automations"]}
            self.assertFalse({n for n in nodes_ if n.startswith("reply-")})
        self.assertIn("factory:comment", plan_labels(plan))  # kept while off: a wake workflow installed earlier still adds it

    def test_mcp_connectors_come_from_node_then_agent_then_role(self):
        def edit_factory(f):
            f["roles"]["developer"]["mcp"] = {"youtrack": {"connector": "youtrack"}}
            f["agents"]["reviewer"]["mcp"] = {"slack": {"connector": "slack"}}

        def edit_workflow(w):
            w["nodes"]["fix"]["mcp"] = {"youtrack": None, "slack": {"connector": "slack"}}  # null removes the role's
        a = {x["spec"]["node"]: x for x in definition_plan(edit_factory, edit_workflow)["automations"]}
        self.assertEqual(a["implement-medium"]["mcpConnectors"], ["youtrack"])
        self.assertIn("## Your MCP servers\n\n- youtrack (Air connector youtrack)\n", a["implement-medium"]["prompt"])
        self.assertEqual(a["fix"]["mcpConnectors"], ["slack"])
        self.assertEqual(a["review-high"]["mcpConnectors"], ["slack"])
        self.assertEqual(a["review-low"]["mcpConnectors"], [])
        self.assertNotIn("Your MCP servers", a["review-low"]["prompt"])

    def test_labels_and_triggers_follow_the_stage_label_prefix(self):
        def edit(w):
            w["stages"]["label_prefix"] = "work:"
            for n in w["nodes"].values():
                for t in n.get("triggers") or []:
                    if isinstance(t, dict) and "label" in t:
                        t["label"] = t["label"].replace("factory:", "work:", 1)
        plan = definition_plan(edit_workflow=edit)
        for want in ("work:ready", "work:in-dev"):
            self.assertIn(want, plan_labels(plan))
        self.assertNotIn("factory:ready", plan_labels(plan))
        a = {x["spec"]["node"]: x for x in plan["automations"]}
        self.assertEqual({t["filters"]["label"] for t in a["implement-medium"]["triggers"]}, {"work:implement-medium"})
        self.assertFalse([x for x in plan_labels(plan) if "<" in x])

    def test_environment_repositories_come_from_the_definition(self):
        def edit(f):
            f["environments"]["knowledge"]["repositories"] = [{"repo": "o/other"}]

        def writes_other(w):  # a node that runs in o/other (#1112: else it runs in a repository variant)
            w["nodes"]["retro"]["writes"] = ["o/other"]
        plan = definition_plan(edit_factory=edit, edit_workflow=writes_other)
        envs = {e["name"]: e["repository"] for e in plan["environments"]}
        self.assertEqual(envs["[Slop factory] knowledge"], "https://github.com/o/other")
        self.assertEqual(envs["[Slop factory] engine"], "https://github.com/zaharchenko-evgeny/slop-factory")

    def test_youtrack_tracker_renders_ticket_triggers_as_youtrack_events(self):
        def edit_nodes(compiled):
            compiled["nodes"]["implement-medium"]["triggers"] = ["youtrack.tagAdded factory:implement-medium"]
        a = {x["spec"]["node"]: x for x in youtrack_plan(tag_store, edit_nodes)["automations"]}
        self.assertIn({"triggerType": "EVENT", "source": "youtrack", "eventType": "issueUpdated",
                       "filters": {"tagAdded": "factory:implement-medium"}}, a["implement-medium"]["triggers"])
        self.assertEqual({t["source"] for t in a["fix"]["triggers"]}, {"github"})
        self.assertEqual(deploy.trigger("youtrack.fieldChanged State=In Progress", None)["filters"], {"State": "In Progress"})

    def test_youtrack_stage_tag_is_per_repository_and_only_that_node_listens(self):
        """#896: with `tracker.repository_field`, `implement api` listens to `factory:ready-api`, `implement web` to `-web`."""
        def edit(conf):
            tag_store(conf)
            conf["tracker"] = {"adapter": "youtrack", "url": "https://y.example", "project": "LME",
                               "repository_field": "Repository"}
        with self.linked_team(edit, self.youtrack_ready) as (_, _, plan):
            pass
        listens = {a["name"]: [t["filters"] for t in a["triggers"]] for a in plan["automations"]
                   if any(t.get("filters", {}).get("tagAdded", "").startswith("factory:ready") for t in a["triggers"])}
        self.assertEqual(listens, {"[Slop factory] implement-medium api": [{"tagAdded": "factory:ready-api"}],
                                   "[Slop factory] implement-medium web": [{"tagAdded": "factory:ready-web"}]})
        self.assertTrue({"factory:ready-api", "factory:ready-web"} <= set(plan["tags"]))
        self.assertNotIn("factory:ready", plan["tags"])

    @staticmethod
    def linked_projects(conf):
        """#981: the two-repository team (o/api, o/web) as a YouTrack team with one linked project each."""
        tag_store(conf)
        conf["tracker"] = {"adapter": "youtrack", "url": "https://y.example", "project": "LME",
                           "projects": {"API": "o/api", "WEB": "o/web"}}

    @staticmethod
    @contextlib.contextmanager
    def linked_team(edit_conf, edit_nodes, web=True):
        """The team set (deploy reads its definition) by `two_repository_team`: (conf, compiled, plan)."""
        with ProjectPlanTest.two_repository_team(web):
            conf, compiled = copy.deepcopy(cfg.load()), copy.deepcopy(nodes.load())
            edit_conf(conf)
            edit_nodes(compiled)
            yield conf, compiled, air.plan_air(compiled, conf)

    @staticmethod
    def youtrack_ready(compiled):
        compiled["nodes"]["implement-medium"]["triggers"] = ["youtrack.tagAdded factory:ready"]

    @staticmethod
    def project_filters(plan):
        return {a["name"]: t["filters"]["projectShortName"] for a in plan["automations"] for t in a["triggers"]
                if "projectShortName" in t.get("filters", {})}

    def test_copy_trigger_limited_to_linked_project(self):
        with self.linked_team(self.linked_projects, self.youtrack_ready) as (_, _, plan):
            listens = {a["name"]: [t["filters"] for t in a["triggers"]] for a in plan["automations"]
                       if any(t.get("filters", {}).get("tagAdded") == "factory:ready" for t in a["triggers"])}
        self.assertEqual(listens, {
            "[Slop factory] implement-medium api": [{"tagAdded": "factory:ready", "projectShortName": "API"}],
            "[Slop factory] implement-medium web": [{"tagAdded": "factory:ready", "projectShortName": "WEB"}]})
        self.assertNotIn("factory:ready-api", plan["tags"])  # shared tags as today
        self.assertEqual(youtrack_provision.trigger_filters({"tracker": {"projects": {"API": "o/api"}}}, None), {})
        self.assertEqual(youtrack_provision.trigger_filters(  # repository names compare case-insensitively
            {"tracker": {"projects": {"API": "O/Api"}}}, {"url": "https://github.com/o/api"}), {"youtrack": {"projectShortName": "API"}})

    def test_unlinked_project_is_reported(self):
        def check(conf, compiled):
            return [e.split(":", 2)[:2] for e in deploy.output_errors(compiled, conf)]
        with self.linked_team(self.linked_projects, self.youtrack_ready) as (conf, compiled, _):
            self.assertEqual(deploy.output_errors(compiled, conf), [])
            conf["tracker"]["projects"]["OPS"] = "o/ops"
            conf["tracker"]["projects"]["TEAM"] = "O/API"  # one trigger cannot select two projects (Air ANDs filters)
            self.assertEqual(deploy.output_errors(compiled, conf), [
                "unknown-repository: tracker.projects.OPS: o/ops is not a repository",
                "duplicate-repository: tracker.projects.TEAM: O/API is already linked to API"])
            del conf["tracker"]["projects"]["OPS"], conf["tracker"]["projects"]["TEAM"]
            conf.pop("targets")  # no surface: the projects are still checked
            for k in compiled["nodes"]:  # no node listens to YouTrack
                compiled["nodes"][k]["triggers"] = [t for t in compiled["nodes"][k].get("triggers") or [] if not str(t).startswith("youtrack.")]
            self.assertEqual(check(conf, compiled), [["unlinked-project", " tracker.projects.API"],
                                                     ["unlinked-project", " tracker.projects.WEB"]])

    def test_linked_project_of_a_single_repository_team_is_unlinked(self):
        """One ticket repository: no per-repository copy, so no trigger is limited to the project and nobody listens."""
        def edit(conf):
            self.linked_projects(conf)
            conf["tracker"]["projects"] = {"API": "o/api"}
        with self.linked_team(edit, self.youtrack_ready, web=False) as (conf, compiled, plan):
            self.assertEqual(self.project_filters(plan), {})
            self.assertEqual(deploy.output_errors(compiled, conf),
                             ["unlinked-project: tracker.projects.API: no node listens to project API (o/api)"])

    def test_linked_project_with_environment_trackers_is_unlinked(self):
        """#1069: with environment trackers a default-environment node runs once per project, not per repository."""
        with self.linked_team(self.linked_projects, self.youtrack_ready) as (conf, compiled, _):
            env = deploy.default_environment(compiled, deploy.team_definition())
            conf["environments"] = {env: {"tracker": {"scope": {"field": "Team", "value": "Engine"}}}}
            self.assertEqual(self.project_filters(air.plan_air(compiled, conf)), {})
            self.assertEqual([e.split(":")[0] for e in deploy.output_errors(compiled, conf)], ["unlinked-project"] * 2)

    def test_hand_added_connector_is_a_removal(self):
        planned = {"name": "[F] a", "mcpConnectors": []}
        plan = {"prefix": "[F] ", "environments": [], "automations": [planned]}
        self.assertEqual(air.diff(plan, [{"id": "a1", "name": "[F] a", "mcpConnectors": ["slack"]}], []),
                         [("update", "automation", "[F] a", planned, {"id": "a1", "name": "[F] a", "mcpConnectors": ["slack"]},
                           ["mcpConnectors"])])
        self.assertEqual(air.diff(plan, [{"id": "a1", "name": "[F] a"}], []), [])  # a record without the field has none

class PlanTest(unittest.TestCase):
    def test_copy_prompt_names_its_repository(self):
        plan = definition_plan()
        copies = [a for a in plan["automations"] if a["name"].endswith(" slop-eval-factory")]
        tracker = [a for a in plan["automations"] if a["name"].endswith(" slop-factory")]
        self.assertTrue(copies)
        commands = []
        for automation in copies:
            prompt = automation["prompt"]
            if "factory begin " in prompt or "factory sweep" in prompt:
                command = next(line for line in prompt.splitlines() if "factory begin " in line or "factory sweep" in line)
                commands.append((automation["name"], command))
        self.assertTrue(commands)
        for name, command in commands:
            self.assertIn("FACTORY_NODE_REPO=zaharchenko-evgeny/slop-eval-factory", command, name)
        self.assertTrue(tracker)
        self.assertTrue(all("FACTORY_NODE_REPO" not in a["prompt"] for a in tracker))

    def test_routines_target_team_repository(self):
        url = "https://github.com/o/team"
        plan = air.plan_air(nodes.load(), {**cfg.load(), "team_repository": url})
        automations = {a["name"]: a for a in plan["automations"]}
        environments = {e["name"]: e for e in plan["environments"]}
        for name in ("retro", "distill"):
            self.assertEqual(automations[f"[Slop factory] {name}"]["repository"]["browseUrl"], url)
        self.assertEqual(environments["[Slop factory] ops"]["repository"], url)
        self.assertEqual(automations["[Slop factory] review-knowledge"]["repository"]["browseUrl"], url)

    def test_air_matches_fixture(self):
        plan = air.plan_air(yaml.safe_load((FIX / "nodes.yaml").read_text()),
                               yaml.safe_load((FIX / "factory.yaml").read_text()))
        got = json.dumps(plan, indent=2, sort_keys=True) + "\n"
        if os.environ.get("UPDATE_FIXTURE"):
            (FIX / "air-plan.json").write_text(got)
        self.assertEqual(got, (FIX / "air-plan.json").read_text())

    def test_plan_unchanged_without_agents(self):
        """Without `agents:` and `agent:` keys the plan is the committed one: no spec names an agent (ADR-0020)."""
        config = yaml.safe_load((FIX / "factory.yaml").read_text())
        self.assertNotIn("agents", config)
        plan = air.plan_air(yaml.safe_load((FIX / "nodes.yaml").read_text()), config)
        self.assertEqual(json.dumps(plan, indent=2, sort_keys=True) + "\n", (FIX / "air-plan.json").read_text())
        self.assertFalse([a["name"] for a in plan["automations"] if {"agent", "persona", "mentions"} & set(a["spec"])])

    def test_environments_only_export_the_environment_name_and_repository(self):
        url = "https://github.com/o/team"
        plan = air.plan_air(nodes.load(), {**cfg.load(), "team_repository": url})
        self.assertTrue(plan["environments"])
        for e in plan["environments"]:
            self.assertEqual(e["variables"], {"FACTORY_ENV": e["variables"]["FACTORY_ENV"],
                                              "FACTORY_ENV_REPOSITORY": definition.full_name(e["repository"])})
        plain = air.plan_air(nodes.load(), {**cfg.load(), "team_repository": ""})
        self.assertTrue(all("FACTORY_TEAM_REPOSITORY" not in e["variables"] for e in plain["environments"]))

    def test_every_automation_runs_in_an_environment_of_its_repository(self):
        plan = air.plan_air(nodes.load(), {**cfg.load(), "team_repository": "https://github.com/o/team"})
        env_repos = {e["name"]: e["repository"] for e in plan["environments"]}
        mismatched = [a["name"] for a in plan["automations"]
                      if env_repos[a["environmentConfigId"]] != a["repository"]["browseUrl"]]
        self.assertEqual(mismatched, [])
        self.assertTrue(any(a["repository"]["fullName"] == "o/team" for a in plan["automations"]))

    def test_team_and_project_automations_get_the_other_repositories(self):
        config = {**cfg.load(), "team_repository": "https://github.com/o/team"}
        team = {"repository": deploy.repository({"url": config["team_repository"]}), "branch": "main"}
        project = config["repositories"][0]
        project_repository = {"repository": deploy.repository(project), "branch": project["default_branch"]}
        plan = air.plan_air(nodes.load(), config)
        team_automations = [a for a in plan["automations"] if a["repository"]["fullName"] == "o/team"]
        project_automations = [a for a in plan["automations"] if a["repository"]["fullName"] != "o/team"]
        self.assertTrue(team_automations and all(a.get("additionalRepositories") == [project_repository]
                                                for a in team_automations))
        self.assertTrue(project_automations and all(a.get("additionalRepositories") == [team] for a in project_automations))
        planned = [{"repository": {"fullName": "o/team"}, "branch": "main"}]
        live = [{"repository": {"fullName": "o/team", "id": "server-field"}, "branch": "main",
                 "snapshotId": "server-field"}]
        self.assertTrue(air.same("additionalRepositories", live, planned))
        self.assertTrue(air.same("additionalRepositories", None, []))
        self.assertFalse(air.same("additionalRepositories", live, [{"repository": {"fullName": "o/team"},
                                                                        "branch": "develop"}]))

    def test_three_repositories_check_out_the_other_projects_and_the_read_only_team(self):
        base = cfg.load()
        other = {"name": "other", "url": "https://github.com/o/other", "default_branch": "develop", "role": "project"}
        plan = air.plan_air(nodes.load(), {**base, "team_repository": "https://github.com/o/team",
                                              "repositories": [*base["repositories"], other]})
        want = [{"repository": deploy.repository(other), "branch": "develop"},
                {"repository": deploy.repository({"url": "https://github.com/o/team"}), "branch": "main"}]
        project = [a for a in plan["automations"] if a["repository"]["fullName"] != "o/team"]
        team = [a for a in plan["automations"] if a["repository"]["fullName"] == "o/team"]
        self.assertTrue(project and all(a["repository"]["fullName"] == "zaharchenko-evgeny/slop-factory"
                                        and a["additionalRepositories"] == want for a in project))
        self.assertTrue(all("checked out beside it: `/workspaces/other`" in a["prompt"] for a in project))
        names = {a["name"] for a in team}
        for name in ("retro", "distill", "review-knowledge"):
            self.assertIn(f"[Slop factory] {name}", names)
        team_projects = [{"repository": deploy.repository(r), "branch": r["default_branch"]}
                         for r in [*base["repositories"], other]]
        self.assertTrue(all(a.get("additionalRepositories") == team_projects
                            and a["environmentConfigId"] == "[Slop factory] ops" for a in team))

    def test_team_repository_listed_as_a_project_is_one_writable_checkout(self):
        base = cfg.load()
        teamrepo = {"name": "team", "url": "https://github.com/o/team", "default_branch": "trunk", "role": "project"}
        plan = air.plan_air(nodes.load(), {**base, "team_repository": teamrepo["url"],
                                              "repositories": [*base["repositories"], teamrepo]})
        want = [{"repository": deploy.repository(teamrepo), "branch": "trunk"}]
        project = [a for a in plan["automations"] if a["repository"]["fullName"] != "o/team"]
        self.assertTrue(project and all(a["additionalRepositories"] == want for a in project))
        for a in project:
            self.assertIn("`/workspaces/team` is the team repository and a project repository", a["prompt"])
            self.assertIn("`$HOME/factory-team`", a["prompt"])
            self.assertNotIn("is read-only", a["prompt"])

    def test_prompts_enter_the_main_project_checkout_only_when_it_is_not_the_team(self):
        plan = air.plan_air(nodes.load(), {**cfg.load(), "team_repository": "https://github.com/o/team"})
        project = [a for a in plan["automations"] if a["repository"]["fullName"] != "o/team"]
        team = [a for a in plan["automations"] if a["repository"]["fullName"] == "o/team"]
        self.assertTrue(project and all("cd /workspaces/slop-factory" in a["prompt"] for a in project))
        self.assertTrue(team and all("cd /workspaces/" not in a["prompt"] for a in team))
        self.assertIn("(file: `skills/tdd/SKILL.md`)", next(a["prompt"] for a in project
                                                             if a["name"] == "[Slop factory] implement-mid"))

    def test_nodes_get_environment_by_env(self):
        plan = air.plan_air(nodes.load(), cfg.load())
        env = {x["name"]: x["environmentConfigId"] for x in plan["automations"]}
        for name, want in (("intake", "read"), ("join", "read"), ("finish", "read"), ("review-full", "read"),
                           ("implement-mid", "write"), ("fix-cheap", "write"), ("update-strong", "write")):
            self.assertEqual(env[f"[Slop factory] {name}"], f"[Slop factory] {want}")
        self.assertEqual({e["name"]: e["variables"]["FACTORY_ENV"] for e in plan["environments"]},
                         {"[Slop factory] read": "read", "[Slop factory] write": "write",
                          "[Slop factory] ops": "ops"})  # ops: only a disabled node, still planned

    def test_local_target_is_rejected(self):
        self.assertEqual(deploy.main(["plan", "--target", "local"]), 2)

    def test_plan_lists_every_label_the_gate_writes(self):
        plan = air.plan_air(nodes.load(), cfg.load())
        for want in ("factory:ready", "factory:implement-cheap", "factory:fix-strong",
                     "complexity:small", "risk:high"):
            self.assertIn(want, plan_labels(plan))
        self.assertFalse([x for x in plan_labels(plan) if "<" in x])

    def test_script_nodes_only_run_the_sweep(self):
        plan = json.loads((FIX / "air-plan.json").read_text())
        a = {x["name"]: x for x in plan["automations"]}
        self.assertIn("Run `factory sweep` in the foreground", a["[Slop factory] sweep"]["prompt"])
        self.assertIn("`factory begin intake --issue <the issue number>`", a["[Slop factory] intake"]["prompt"])

    def test_doctor_prompts_use_the_installed_command_and_explain_ticket_effects(self):
        plan = air.plan_air(nodes.load(), cfg.load())
        automations = {a["name"]: a for a in plan["automations"]}
        for name in ("doctor", "doctor-write", "doctor-ops"):
            prompt = automations[f"[Slop factory] {name}"]["prompt"]
            if name == "doctor-ops":
                self.assertIn("Run the installed `factory doctor --engine` command exactly once", prompt)
                self.assertIn("in the foreground with only the `--engine` argument", prompt)
                self.assertIn("Do not use `uv run`, `--help` or add other arguments", prompt)
            else:
                self.assertIn("Run the installed `factory doctor` command exactly once", prompt)
                self.assertIn("in the foreground with no arguments", prompt)
                self.assertIn("Do not use `uv run`, `--help` or add arguments", prompt)
            self.assertIn("every JSON probe line", prompt)
            self.assertIn("exit code", prompt)
            self.assertIn("temporary probe issue", prompt)
            self.assertIn("`Doctor: <probe> fails`", prompt)
            self.assertIn("must not create, edit or close tickets yourself", prompt)

    def test_every_script_prompt_waits_for_command_exit(self):
        plans = (air.plan_air(nodes.load(), cfg.load()), json.loads((FIX / "air-plan.json").read_text()))
        for plan in plans:
            for automation in plan["automations"]:
                if automation["name"].startswith(("[Slop factory] sweep", "[Slop factory] join",
                                                   "[Slop factory] finish", "[Slop factory] merge",
                                                   "[Slop factory] reopen",
                                                   "[Slop factory] notify")):
                    self.assertIn("wait until it exits", automation["prompt"])

    def test_every_script_prompt_forbids_uv_run(self):
        plans = (air.plan_air(nodes.load(), cfg.load()), json.loads((FIX / "air-plan.json").read_text()))
        for plan in plans:
            for automation in plan["automations"]:
                if automation["name"].startswith(("[Slop factory] sweep", "[Slop factory] join",
                                                   "[Slop factory] finish", "[Slop factory] merge",
                                                   "[Slop factory] reopen", "[Slop factory] start",
                                                   "[Slop factory] notify", "[Slop factory] reconcile",
                                                   "[Slop factory] doctor")):
                    self.assertIn("never `uv run`", automation["prompt"], automation["name"])

    def test_no_prompt_asks_for_json(self):
        plans = (air.plan_air(nodes.load(), cfg.load()), json.loads((FIX / "air-plan.json").read_text()))
        for plan in plans:
            for x in plan["automations"]:
                prompt = x["prompt"].replace("never write the payload or any JSON", "")
                for word in ("--event", "stdin", "heredoc", "payload"):
                    self.assertNotIn(word, prompt, x["name"])
            prompts = {x["name"]: x["prompt"] for x in plan["automations"]}
            self.assertIn("`factory begin implement-mid --issue <the issue number>`", prompts["[Slop factory] implement-mid"])

    def test_pull_request_nodes_begin_with_the_pr_number(self):
        a = {x["name"]: x["prompt"] for x in air.plan_air(nodes.load(), cfg.load())["automations"]}
        self.assertIn("`factory begin fix-mid --pr <the pull request number>`", a["[Slop factory] fix-mid"])

    def test_review_automations_trigger_on_focus_rerequest_labels(self):
        for a in json.loads((FIX / "air-plan.json").read_text())["automations"]:
            if a["name"].startswith("[Slop factory] review-"):
                self.assertIn({"triggerType": "EVENT", "source": "github", "eventType": "pull_request.labeled",
                               "filters": {"label": f"factory:rereview-{a['name'].rsplit('-', 1)[1]}",
                                           "pr_author": "jetbrains-air[bot]"}}, a["triggers"])

    def test_reconcile_is_an_hourly_air_schedule_only(self):
        plan = air.plan_air(nodes.load(), cfg.load())
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

    def test_ci_label_sync_runs_in_the_factory_repository_with_no_project_workflow(self):
        base = cfg.load()
        other = {"name": "other", "url": "https://github.com/o/other", "default_branch": "main", "role": "project"}
        plan = air.plan_air(nodes.load(), {**base, "repositories": [*base["repositories"], other]})
        a = next(x for x in plan["automations"] if x["name"] == "[Slop factory] reconcile")  # each pass runs gate.ci_sync
        self.assertEqual(a["repository"]["fullName"], "zaharchenko-evgeny/slop-factory")
        self.assertEqual(plan["files"], {})  # no workflow or check file under any repository's .github/

    def three_repositories(self, **extra):
        base = cfg.load()
        repos = [*base["repositories"], *({"name": n, "url": f"https://github.com/o/{n}", "default_branch": "main",
                                           "role": "project"} for n in ("api", "client"))]
        return air.plan_air(nodes.load(), {**base, "team_repository": "", "repositories": repos, **extra})

    def test_three_repositories_deploy_one_node_set_without_a_write_token(self):
        """ADR-0024 (#983): a descriptor team plans one automation per node, with the other repositories checked out
        beside it; no copy per repository, no `repo:` filter and no write token."""
        plan = self.three_repositories(team_repository="https://github.com/o/team")
        by_name = {a["name"]: a for a in plan["automations"]}
        self.assertEqual({r["repository"]["fullName"] for r in by_name["[Slop factory] intake"]["additionalRepositories"]},
                         {"o/api", "o/client", "o/team"})
        self.assertIn("[Slop factory] implement-mid", by_name)
        self.assertFalse([n for n in by_name if n.endswith((" api", " client"))])
        self.assertFalse([a["name"] for a in plan["automations"] for t in a["triggers"] if "repo_label" in t.get("filters", {})])
        self.assertFalse([e["name"] for e in plan["environments"] if "FACTORY_WRITE_TOKEN" in e["secrets"]])
        for node in ("implement-mid", "fix-mid", "update-mid"):
            self.assertNotIn("FACTORY_WRITE_TOKEN", by_name[f"[Slop factory] {node}"]["prompt"])
        self.assertEqual(plan["files"], {})  # no .github/ file in any project repository

    def test_scoped_knowledge_review_uses_team_environment(self):
        plan = self.three_repositories(team_repository="https://github.com/o/team")
        envs = {e["name"]: e["repository"] for e in plan["environments"]}
        for a in plan["automations"]:  # Air: the automation repository must be its environment's repository
            self.assertEqual(a["repository"]["browseUrl"], envs[a["environmentConfigId"]], a["name"])
        knowledge = [a for a in plan["automations"] if "review-knowledge" in a["name"]]
        self.assertTrue(knowledge)
        self.assertTrue(all(a["repository"]["browseUrl"] == "https://github.com/o/team" for a in knowledge))

    def test_issues_token_is_deployed_to_every_environment_and_adds_no_write_token(self):
        plan = self.three_repositories(issues_token_env="FACTORY_ISSUES_TOKEN")
        envs = {e["name"]: e["secrets"] for e in plan["environments"]}
        self.assertIn("FACTORY_ISSUES_TOKEN", envs["[Slop factory] read"])
        for name, secrets in envs.items():
            self.assertIn("FACTORY_ISSUES_TOKEN", secrets, name)
            self.assertNotIn("FACTORY_WRITE_TOKEN", secrets, name)

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
        for e in air.plan_air(nodes.load(), cfg.load())["environments"]:
            self.assertNotIn("GITHUB_TOKEN", e["secrets"])
            if e["name"] in ("[Slop factory] read", "[Slop factory] write"):
                self.assertIn("*.blob.core.windows.net", e["domains"])
        join = next(x for x in air.plan_air(nodes.load(), cfg.load())
                    ["automations"] if x["name"] == "[Slop factory] join")
        self.assertEqual([(x["eventType"], x["filters"].get("label")) for x in join["triggers"]],
                         [("pull_request_review.submitted", None), ("pull_request.labeled", "factory:ci-red")])
        reopen = next(x for x in air.plan_air(nodes.load(), cfg.load())
                       ["automations"] if x["name"] == "[Slop factory] reopen")
        self.assertEqual([(x["eventType"], x["filters"].get("label"), x["filters"].get("pr_author"))
                          for x in reopen["triggers"]],
                         [("pull_request.synchronize", None, "jetbrains-air[bot]"),
                          ("pull_request.labeled", "factory:ci-red", "jetbrains-air[bot]")])
        assert not any(x["triggerType"] == "SCHEDULED" for a in air.plan_air(nodes.load(), cfg.load())["automations"] if a["name"] not in ("[Slop factory] retro", "[Slop factory] distill", "[Slop factory] doctor", "[Slop factory] doctor-write", "[Slop factory] doctor-ops", "[Slop factory] reconcile") for x in a["triggers"]), "no polling"

    def test_one_triggervcs_entry_per_trigger_and_a_spec_per_automation(self):
        for a in air.plan_air(nodes.load(), cfg.load())["automations"]:
            self.assertEqual(len(a["triggerVcs"]), len(a["triggers"]), a["name"])
            self.assertNotIn("```json", a["prompt"])  # no JSON in a prompt: the run compiles its spec
        a = {x["name"]: x for x in air.plan_air(nodes.load(), cfg.load())["automations"]}
        spec = a["[Slop factory] implement-mid"]["spec"]
        self.assertEqual((spec["node"], spec["tier"], spec["engine"]), ("implement", "mid", cfg.load()["engine"]))
        self.assertEqual(set(spec["outcomes"]), {"done", "error", "blocked", "failed", "env-broken"})
        self.assertIn("- `error`: the work needs the next tier", a["[Slop factory] implement-mid"]["prompt"])
        self.assertEqual(set(a["[Slop factory] join"]["spec"]["runs"]), {"finish", "reopen", "merge", "start"})

    def test_compile_automation_gives_the_gate_options(self):
        config, graph = cfg.load(), nodes.load()
        got = deploy.compile_automation("review-behaviour", graph, config)
        self.assertEqual((got["node"], got["focus"], got["tier"]), ("review", "behaviour", "review"))
        self.assertEqual(deploy.compile_automation("fix-mid", graph, config)["tier"], "mid")
        self.assertIsNone(deploy.compile_automation("intake", graph, config)["tier"])
        self.assertRaises(KeyError, deploy.compile_automation, "nope", graph, config)

    def test_agent_prompt_sections(self):
        a = {x["name"]: x["prompt"] for x in air.plan_air(nodes.load(), cfg.load())["automations"]}
        for name, role, intro in (("review-behaviour", "reviewer", "You review one branch independently"),
                                  ("implement-mid", "developer", "You implement one ticket in its worktree")):
            prompt = a[f"[Slop factory] {name}"]
            marks = [prompt.index(m) for m in (intro, "## Your skills", "## This step", "## How you start and finish")]
            self.assertEqual(marks, sorted(marks), name)
            self.assertTrue(prompt.startswith(f"You are the {role} of Slop factory"))
            self.assertIn((ROOT / f"engine/template-v1/roles/{role}/role.md").read_text().strip(), prompt)
            self.assertIn(f"`factory begin {name} --", prompt)
            self.assertIn("`factory end <outcome>`", prompt)
            self.assertIn("`factory-step` skill", prompt)
            self.assertNotIn("handoff_path", prompt)
            for word in ("gate.py", "--node", "--tier", "--focus", "read `role_file`"):
                self.assertNotIn(word, prompt)
        self.assertIn("(behaviour focus)", a["[Slop factory] review-behaviour"])
        self.assertIn("- ponytail: Lazy senior dev mode", a["[Slop factory] implement-mid"])
        intake = a["[Slop factory] intake"]
        self.assertIn("- `grill`:", intake)
        self.assertIn("- `bug`:", intake)

    def test_webhook_node_plans_a_webhook_trigger_with_the_node_output(self):
        graph = nodes.load()
        graph["nodes"]["hook"] = {**graph["nodes"]["intake"], "triggers": ["webhook dry-run-finished"], "output": "create-pr"}
        a = next(x for x in air.plan_air(graph, cfg.load())["automations"] if x["name"] == "[Slop factory] hook")
        self.assertEqual(a["triggers"], [{"triggerType": "WEBHOOK"}])
        self.assertEqual(a["triggerVcs"], [{"outputBehaviour": "create-pr"}])
        self.assertIn("`factory begin hook --issue <the `issue` field of the trigger payload>`", a["prompt"])

    def test_scheduled_routine_prompts_take_no_number(self):
        a = {x["name"]: x["prompt"] for x in air.plan_air(nodes.load(), cfg.load())["automations"]}
        for name in ("retro", "distill"):
            prompt = a[f"[Slop factory] {name}"]
            self.assertIn(f"`factory begin {name}`", prompt)
            self.assertNotIn("<the issue number>", prompt)

    def test_pull_request_scripts_run_from_main(self):
        a = {x["name"]: x["prompt"] for x in air.plan_air(nodes.load(), cfg.load())["automations"]}
        self.assertIn("`factory sweep --main --issue <the number after the leading # of the PR title, or else the body's Closes #<n>>`", a["[Slop factory] join"])
        self.assertIn("`factory sweep --main --issue <the number after the leading # of the PR title, or else the body's Closes #<n>>`", a["[Slop factory] finish"])
        self.assertIn("`factory sweep --main --issue <the number after the leading # of the PR title, or else the body's Closes #<n>>`", a["[Slop factory] reopen"])

    def test_no_review_automation_triggers_on_ci_green(self):
        automations = air.plan_air(nodes.load(), cfg.load())["automations"]
        reviews = [a for a in automations if a["name"].startswith("[Slop factory] review-")]
        self.assertTrue(reviews)
        self.assertTrue(all("factory:ci-green" not in json.dumps(a["triggers"]) for a in reviews))
        self.assertTrue(all(any(t["filters"].get("label", "").startswith("factory:review-")
                               for t in a["triggers"]) for a in reviews))

    def test_event_script_nodes_scope_to_one_ticket(self):
        scripts = {"join", "start", "finish", "reopen", "merge"}
        for automation in air.plan_air(nodes.load(), cfg.load())["automations"]:
            name = automation["name"].removeprefix("[Slop factory] ")
            if name in scripts:
                self.assertIn("factory sweep --main --issue", automation["prompt"], name)
            elif "factory sweep" in automation["prompt"]:
                self.assertTrue("--issue" in automation["prompt"] or "--event" in automation["prompt"], name)

    def test_real_plan_is_prefixed_and_covers_pilot(self):
        config = cfg.load()
        n = nodes.load()
        plan = air.plan_air(n, config)
        names = {a["name"] for a in plan["automations"]}
        self.assertEqual(plan["prefix"], "[Slop factory] ")
        self.assertTrue(all(x.startswith(plan["prefix"]) for x in names))
        pilot = {f"[Slop factory] {x}" for x in (
            "intake", "start", "investigate", "join", "finish", "reopen", "merge", "retro", "distill", "doctor", "doctor-write", "doctor-ops",
            "reconcile",
            *(f"{b}-{t}" for b in ("implement", "fix", "update") for t in ("cheap", "mid", "strong")),
            *(f"review-{f}" for f in ("full", "behaviour", "structure-security", "knowledge")))}
        self.assertEqual(names, pilot)
        self.assertEqual({n for n, v in n["nodes"].items() if v.get("pilot")},
                         {"intake", "start", "implement", "review", "join", "fix", "update", "finish", "reopen", "merge"})
        self.assertEqual(air.plan_air(n, config), plan)



class DefinitionPlanTest(unittest.TestCase):
    """A plan from the ADR-0021 layout (factory/tests/fixtures/v2)."""

    @classmethod
    def setUpClass(cls):
        before = cfg._team
        cfg.set_team(Path(__file__).resolve().parent / "fixtures/v2")
        try:
            cls.plan = air.plan_air(nodes.load(), cfg.load())
        finally:
            cfg.set_team(before)
        cls.by = {a["name"]: a for a in cls.plan["automations"]}
        for a in cls.plan["automations"]:  # also by node name: a node's first automation (repository scopes aside)
            cls.by.setdefault(" ".join(a["name"].split(" ")[:3]), a)

    def test_each_variant_runs_its_agents_model(self):
        model = {k: a["agentAndModel"]["id"] for k, a in self.by.items()}
        self.assertEqual(model["[Slop factory] implement-small"], "gpt-6-luna")
        self.assertEqual(model["[Slop factory] implement-medium"], "claude-sonnet-5-5[1m]")
        self.assertEqual(model["[Slop factory] review-high"], "gpt-6.1-sol")
        self.assertEqual(model["[Slop factory] review-low"], "gpt-6-luna")

    def test_mcp_connectors_go_to_the_automation_and_custom_servers_stay_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            team = Path(tmp) / "team"
            shutil.copytree(Path(__file__).resolve().parent / "fixtures/v2", team)
            path = team / "factory.yaml"
            data = yaml.safe_load(path.read_text())
            data["defaults"]["env"]["IDE_PORT"] = {"required": False}
            data["roles"]["developer"]["mcp"] = {
                "youtrack": {"connector": "internal-youtrack"},
                "idea": {"command": "npx", "args": ["-y", "@jetbrains/mcp-proxy"], "env": {"IDE_PORT": "${IDE_PORT}"}}}
            path.write_text(yaml.safe_dump(data, sort_keys=False))
            before = cfg._team
            cfg.set_team(team)
            try:
                plan = air.plan_air(nodes.load(), cfg.load())
            finally:
                cfg.set_team(before)
        by = {a["name"]: a for a in plan["automations"]}
        dev = by["[Slop factory] implement-medium"]
        self.assertEqual(dev["mcpConnectors"], ["internal-youtrack"])
        self.assertIn("## Your MCP servers\n\n- youtrack (Air connector internal-youtrack)\n"
                      "- idea (custom, from .mcp.json)\n", dev["prompt"])
        self.assertEqual(by["[Slop factory] intake"]["mcpConnectors"], [])
        self.assertNotIn("Your MCP servers", by["[Slop factory] intake"]["prompt"])
        # a node with custom servers runs in an environment of its own that names the node
        self.assertEqual(dev["environmentConfigId"], "[Slop factory] engine implement-medium")
        envs = {e["name"]: e["variables"] for e in plan["environments"]}
        self.assertEqual(envs["[Slop factory] engine implement-medium"],
                         {"FACTORY_ENV": "engine", "FACTORY_NODE": "implement-medium",
                          "FACTORY_ENV_REPOSITORY": "zaharchenko-evgeny/slop-factory"})
        self.assertNotIn("FACTORY_NODE", envs["[Slop factory] engine"])

    def test_prompts_are_rendered(self):
        for name, a in self.by.items():
            self.assertNotIn("{{", a["prompt"], name)
            self.assertNotIn("{%", a["prompt"], name)
        intake = self.by["[Slop factory] intake"]["prompt"]
        self.assertIn("Split it above 400 changed lines or\n3 repositories.", intake)
        self.assertIn("- factory-step: `~/.agents/skills/factory-step/SKILL.md`\n- to-tickets:", intake)

    def test_no_node_waits_for_a_ci_event(self):
        events = {t.get("eventType") for a in self.plan["automations"] for t in a["triggers"]}
        self.assertNotIn("workflow_run.failed", events)   # reviewers wait for CI themselves (factory ci --wait)

    def test_ci_result_labels_are_kept(self):
        plan = self.plan
        self.assertTrue({"factory:ci-green", "factory:ci-red"} <= set(plan_labels(plan)))  # deploy never removes them

    def test_comment_label_is_kept(self):
        self.assertIn("factory:comment", plan_labels(self.plan))  # scripts/comment-wake-label.sh adds it

    def test_a_pr_woken_node_begins_with_its_pr(self):
        def arg(name):
            p = self.by[f"[Slop factory] {name}"]["prompt"]
            return p[p.find("factory begin"):].split("`")[0]
        self.assertEqual(arg("review-low"), "factory begin review-low --pr <the pull request number>")
        self.assertEqual(arg("automerge"), "factory begin automerge --pr <the pull request number>")
        self.assertEqual(arg("intake"), "factory begin intake --issue <the issue number>")
        merge = self.by["[Slop factory] merge"]["prompt"]
        self.assertIn("factory decide --node merge --pr <the pull request number>", merge)  # PR label wakes it

    def test_a_decide_agent_only_runs_the_command(self):
        merge = self.by["[Slop factory] merge"]["prompt"]
        self.assertIn("You are the clerk of Slop factory.", merge)
        self.assertIn("factory decide --node merge --pr", merge)
        self.assertNotIn("factory begin clerk", merge)

    def test_environments_and_labels(self):
        envs = {e["name"]: e for e in self.plan["environments"]}
        self.assertEqual(envs["[Slop factory] engine"]["secrets"], [])          # no Air automation writes the factory
        # #1112: the knowledge and team nodes run in slop-factory (no `writes`), so in its variant of their environment
        self.assertEqual(sorted(envs), ["[Slop factory] engine", "[Slop factory] knowledge",
                                        "[Slop factory] knowledge slop-factory", "[Slop factory] team slop-factory"])
        for a in self.plan["automations"]:
            self.assertEqual(definition.full_name(envs[a["environmentConfigId"]]["repository"]),
                             a["repository"]["fullName"], a["name"])
        names = {a["name"] for a in self.plan["automations"]}
        self.assertIn("[Slop factory] fix", names)                                  # no per-repository copies
        self.assertNotIn("[Slop factory] fix slop-eval-factory", names)
        self.assertEqual(envs["[Slop factory] knowledge"]["repository"],
                         "https://github.com/zaharchenko-evgeny/slop-eval-factory")
        for label in ("factory:implement-small", "factory:review-high", "factory:knowledge-review", "factory:done",
                      "factory:automerge", "risk:low", "complexity:large",
                      "factory:lesson", "factory:comment"):
            self.assertIn(label, plan_labels(self.plan))
        for label in ("factory:reply-dev", "factory:reply-reviewer", "factory:needs-human"):  # generated reply and needs-human labels
            self.assertIn(label, plan_labels(self.plan))
        self.assertEqual(self.plan["owned_label_prefixes"], ["complexity:", "factory:", "risk:"])



def fake_labels(by_repo):
    """A `gh` runner over {repo: {name: (color, description)}}; records label writes."""
    writes = []

    def run(args, **kw):
        repo = args[args.index("-R") + 1]
        if args[:2] == ["label", "list"]:
            return json.dumps([{"name": n, "color": c, "description": d} for n, (c, d) in by_repo[repo].items()])
        writes.append((args[1], repo, args[2]))
        return ""
    run.writes = writes
    return run


class MainlessEnvironmentPlanTest(unittest.TestCase):
    """ADR-0024 (#979): an environment with no `main` plans one automation per listened repository."""

    @classmethod
    def setUpClass(cls):
        def edit_factory(f):
            f["tracker"] = {"adapter": "github-issues", "repositories": ["o/api", "o/web"]}
            f["environments"]["engine"]["repositories"] = [{"repo": "o/api"}, {"repo": "o/web"}, {"repo": "o/team"}]
            f["environments"]["knowledge"]["repositories"] = [{"repo": "o/team"}]

        def edit_workflow(w):
            for name, n in w["nodes"].items():
                if name.startswith("implement-") or name in ("fix", "update"):
                    n["writes"] = ["o/api", "o/web"]
            w["nodes"]["intake"]["listens"] = "tracker"
            w["nodes"]["retro"]["writes"] = ["o/team"]
        cls.plan = definition_plan(edit_factory, edit_workflow, source="fixtures/v2")
        cls.automations = cls.plan["automations"]

    def copies(self, node):
        return [a for a in self.automations if a["spec"]["node"] == node]

    def test_event_node_gets_one_copy_per_listened_repository(self):
        copies = self.copies("implement-medium")
        self.assertEqual(sorted(a["name"] for a in copies),
                         ["[Slop factory] implement-medium api", "[Slop factory] implement-medium web"])
        self.assertEqual(sorted(a["repository"]["fullName"] for a in copies), ["o/api", "o/web"])
        for a in copies:
            self.assertEqual(a["triggers"], [{"triggerType": "EVENT", "source": "github", "eventType": "issues.labeled",
                                              "filters": {"label": "factory:implement-medium"}}])
            self.assertEqual([v["outputBehaviour"] for v in a["triggerVcs"]], ["create-pr"])
            others = {"o/api", "o/web", "o/team"} - {a["repository"]["fullName"]}
            self.assertEqual({x["repository"]["fullName"] for x in a["additionalRepositories"]}, others)
        self.assertEqual({a["repository"]["fullName"]: a["environmentConfigId"] for a in copies},
                         {"o/api": "[Slop factory] engine", "o/web": "[Slop factory] engine web"})

    def test_read_only_copy_has_read_only_output(self):
        copies = self.copies("review-low")
        self.assertEqual(sorted(a["repository"]["fullName"] for a in copies), ["o/api", "o/web"])
        for a in copies:
            self.assertEqual([v["outputBehaviour"] for v in a["triggerVcs"]], ["read-only"])

    def test_tracker_node_is_one_automation_on_the_tracker(self):
        (a,) = self.copies("intake")
        self.assertEqual(a["name"], "[Slop factory] intake")
        self.assertEqual(a["repository"]["fullName"], "o/api")

    def test_scheduled_writer_runs_in_its_written_repository(self):
        (a,) = self.copies("retro")
        self.assertEqual(a["name"], "[Slop factory] retro")
        self.assertEqual(a["repository"]["fullName"], "o/team")
        self.assertEqual([v["outputBehaviour"] for v in a["triggerVcs"]], ["push-to-new-branch"])

    def test_a_copy_runs_in_an_environment_of_its_repository(self):
        """#1112: Air runs an automation only in an environment of its own repository."""
        envs = {e["name"]: e for e in self.plan["environments"]}
        self.assertEqual(len(envs), len(self.plan["environments"]))
        for a in self.automations:
            self.assertEqual(definition.full_name(envs[a["environmentConfigId"]]["repository"]),
                             a["repository"]["fullName"], a["name"])
        self.assertEqual(envs["[Slop factory] engine web"]["variables"],
                         {**envs["[Slop factory] engine"]["variables"], "FACTORY_ENV_REPOSITORY": "o/web"})

    def test_node_repo_is_the_copys_repository_in_the_prompt(self):
        def edit_factory(f):
            f["tracker"] = {"adapter": "github-issues", "repositories": ["o/api", "o/web"]}
            f["environments"]["engine"]["repositories"] = [{"repo": "o/api"}, {"repo": "o/web"}]
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(HERE / "fixtures/v2", tmp, dirs_exist_ok=True)
            body = Path(tmp) / "roles/developer/role.md"
            body.write_text(body.read_text() + "\nCopy repository: {{ node.repo }}\n")
            for name, edit in ((definition.FACTORY, edit_factory), (definition.WORKFLOW, None)):
                if edit:
                    data = yaml.safe_load((Path(tmp) / name).read_text())
                    edit(data)
                    (Path(tmp) / name).write_text(yaml.safe_dump(data, sort_keys=False))
            before = cfg.team_root()
            cfg.set_team(tmp)
            try:
                plan = air.plan_air(nodes.load(), cfg.load())
            finally:
                cfg.set_team(before)
        prompts = {a["repository"]["fullName"]: a["prompt"] for a in plan["automations"]
                   if a["spec"]["node"] == "implement-medium"}
        self.assertEqual(set(prompts), {"o/api", "o/web"})
        for repo, prompt in prompts.items():
            self.assertIn(f"Copy repository: {repo}", prompt)


class LabelsTest(unittest.TestCase):
    PLAN = {"labels": ["complexity:medium", "factory:ready", "risk:low"], "label_repositories": ["o/tracker", "o/code"],
            "owned_label_prefixes": ["complexity:", "factory:", "risk:"], "label_specs": {
                "complexity:medium": {"color": "fbca04", "description": deploy.COMPLEXITY_DESCRIPTION},
                "factory:ready": {"color": "c2e0c6"}, "risk:low": {"color": "e6f4ea"}}}
    HAVE = {"factory:ready": ("c2e0c6", ""), "complexity:medium": ("fbca04", deploy.COMPLEXITY_DESCRIPTION),
            "risk:low": ("e6f4ea", ""), "factory:old": ("ededed", ""), "bug": ("ff0000", "")}

    def test_each_repository_is_reconciled_independently(self):
        run = fake_labels({"o/tracker": self.HAVE, "o/code": {"factory:ready": ("00ff00", "x")}})
        got = deploy.reconcile_labels(self.PLAN, run=run)
        self.assertEqual([(a, r, n) for a, r, n, *_ in got],
                         [("remove", "o/tracker", "factory:old"),
                          ("create", "o/code", "complexity:medium"), ("update", "o/code", "factory:ready"),
                          ("create", "o/code", "risk:low")])
        self.assertEqual(got[-2][4], ["color"])

    def test_label_without_planned_description_converges(self):
        plan = {"labels": ["factory:ready"], "label_repositories": ["o/tracker"],
                "owned_label_prefixes": ["factory:"], "label_specs": {"factory:ready": {"color": "c2e0c6"}}}
        run = fake_labels({"o/tracker": {"factory:ready": ("c2e0c6", "Factory: run triage")}})

        self.assertEqual(deploy.reconcile_labels(plan, run=run), [])
        self.assertEqual(deploy.reconcile_labels(plan, run=run), [])

    def run_deploy(self, mode, allow_delete, have):
        run = fake_labels({"o/tracker": have, "o/code": have})
        plan = {**self.PLAN, "prefix": "p", "environments": [], "automations": []}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(air, "read_state", return_value=([], [])), \
                mock.patch("factory_plugins.github.tracker.gh", run):
            code = air.deploy(mode, allow_delete, plan, None, None)
        return code, out.getvalue(), err.getvalue(), run.writes

    def test_dry_run_reports_without_writing(self):
        code, out, _, writes = self.run_deploy("--dry-run", False, {**self.HAVE, "risk:low": ("ff0000", "")})
        self.assertEqual(code, 0)
        self.assertIn("update label risk:low in o/tracker (color)", out)
        self.assertIn("remove label factory:old in o/code", out)
        self.assertEqual(writes, [])

    def test_apply_updates_but_refuses_removal_without_allow_delete(self):
        have = {**self.HAVE, "risk:low": ("ff0000", ""), "complexity:medium": ("ededed", "")}
        code, _, err, writes = self.run_deploy("--apply", False, have)
        self.assertEqual(code, 1)
        self.assertIn("refused: would remove label factory:old in o/tracker", err)
        self.assertEqual(writes, [("edit", r, n) for r in ("o/tracker", "o/code") for n in ("complexity:medium", "risk:low")])

    def test_apply_with_allow_delete_removes_owned_labels_only(self):
        code, _, _, writes = self.run_deploy("--apply", True, self.HAVE)
        self.assertEqual(code, 0)
        self.assertEqual(writes, [("delete", "o/tracker", "factory:old"), ("delete", "o/code", "factory:old")])

    def test_apply_writes_project_labels_with_the_write_token(self):
        tokens = []

        def run(args, **kw):
            tokens.append((args[1], args[args.index("-R") + 1], kw.get("token")))
            return "[]" if args[:2] == ["label", "list"] else ""
        wrapped = github_tracker.ProjectWriteRun(run, ["o/code"], {"FACTORY_WRITE_TOKEN": "t"})
        plan = {**self.PLAN, "label_repositories": ["o/tracker", "o/code"], "prefix": "p", "environments": [], "automations": []}
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(air, "read_state", return_value=([], [])):
            air.deploy("--apply", False, plan, None, None, run=wrapped)
        creates = {(r, t) for v, r, t in tokens if v == "create"}
        self.assertEqual(creates, {("o/tracker", None), ("o/code", "t")})


def slop_specs(edit=None):
    """(labels, label_specs) of the slop fixture's config, after `edit` changed a copy of it."""
    before = cfg.team_root()
    try:
        cfg.set_team(HERE / "fixtures/slop")
        conf = copy.deepcopy(cfg.load())
        if edit:
            edit(conf)
        return deploy.labels(nodes.load(), conf), deploy.label_specs(nodes.load(), conf)
    finally:
        cfg.set_team(before)


class LabelColorTest(unittest.TestCase):
    def test_label_specs_color_every_stage_and_command_label_by_role(self):
        names, specs = slop_specs()
        self.assertEqual([n for n in names if not (specs.get(n) or {}).get("color")], [])
        self.assertEqual(specs["factory:in-dev"]["color"], "1d76db")
        self.assertEqual(specs["factory:implement-medium"]["color"], "c5def5")
        self.assertEqual(specs["factory:ci-red"]["color"], "d73a4a")
        self.assertEqual(specs["risk:high"]["color"], "f9d0c4")
        self.assertEqual(specs["complexity:small"]["description"], deploy.COMPLEXITY_DESCRIPTION)

    def test_label_specs_color_a_renamed_stage_and_an_added_command_by_role(self):
        def edit(conf):
            conf["states"]["in-dev"]["store"]["label"] = conf["tracker"]["stages"]["in-dev"] = "team:coding"
            conf["commands"]["build"] = {"store": {"label": "team:build"}}
        names, specs = slop_specs(edit)
        self.assertEqual(specs["team:coding"]["color"], "1d76db")  # stage `in-dev` keeps its color
        self.assertEqual(specs["team:build"]["color"], "c5def5")
        self.assertIn("team:build", names)
        self.assertNotIn("factory:in-dev", names)

    def test_label_color_override(self):
        extra = yaml.safe_load((HERE / "fixtures/label_colors/factory.yaml").read_text())
        _, specs = slop_specs(lambda conf: conf.update(cfg.merge(conf, extra)))
        self.assertEqual(specs["factory:blocked"]["color"], "000000")
        self.assertEqual(specs["factory:approved"]["color"], "0e8a16")  # the other defaults stay

    def test_label_color_random(self):
        plan = {"labels": ["team:odd"], "label_repositories": ["o/a", "o/b"], "owned_label_prefixes": ["team:"],
                "label_specs": {}}
        created = deploy.reconcile_labels(plan, run=fake_labels({"o/a": {}, "o/b": {}}))
        self.assertEqual([c[0] for c in created], ["create", "create"])
        colors = {c[3]["color"] for c in created}
        self.assertEqual(len(colors), 1)  # one color per label, in every repository
        self.assertRegex(colors.pop(), r"^[0-9a-f]{6}$")
        for color in ("ededed", "123456"):  # an existing unmapped label of any color is left alone
            have = fake_labels({"o/a": {"team:odd": (color, "")}, "o/b": {"team:odd": (color, "")}})
            self.assertEqual(deploy.reconcile_labels(plan, run=have), [])


class LabelPlanTest(unittest.TestCase):
    """ADR-0024: labels per repository for a definition with `tracker.repositories`."""
    TRACKER, WEB, DOCS = "zaharchenko-evgeny/slop-factory", "o/web", "o/docs"

    def plan(self, tickets=None):
        def edit(f):
            f["environments"]["engine"]["repositories"] += [{"repo": self.WEB}, {"repo": self.DOCS}]
            f["tracker"].pop("repositories", None)  # the Slop team lists its own (ADR-0024)
            if tickets:
                f["tracker"]["repositories"] = tickets
        return definition_plan(edit)

    def test_every_ticket_repository_gets_stage_and_pr_labels(self):
        plan = self.plan([self.TRACKER, self.WEB])
        self.assertNotIn("labels", plan)
        self.assertNotIn("label_repositories", plan)
        want = {"factory:in-dev", "risk:high", "complexity:medium", "factory:needs-human", "factory:hold",
                "factory:implement-medium", "factory:ci-green", "factory:ci-red", "factory:review-high", "factory:review"}
        for repo in (self.TRACKER, self.WEB):
            self.assertLessEqual(want, set(plan["label_plan"][repo]), repo)

    def test_map_label_only_in_tracker(self):
        plan = self.plan([self.TRACKER, self.WEB])
        self.assertIn("factory:map", plan["label_plan"][self.TRACKER])
        self.assertNotIn("factory:map", plan["label_plan"][self.WEB])
        swapped = self.plan([self.WEB, self.TRACKER])  # the first ticket repository is the tracker
        self.assertIn("factory:map", swapped["label_plan"][self.WEB])
        self.assertNotIn("factory:map", swapped["label_plan"][self.TRACKER])

    def test_repository_without_tickets_gets_no_labels(self):
        plan = self.plan([self.TRACKER, self.WEB])
        self.assertEqual(plan["label_plan"][self.DOCS], [])
        self.assertEqual(plan["label_plan"]["zaharchenko-evgeny/slop-eval-factory"], [])

    def test_plan_without_tracker_repositories_labels_only_the_tracker(self):
        """ADR-0024 (#983): every plan carries `label_plan`; without `tracker.repositories` only the tracker gets labels,
        the map label included."""
        plan = self.plan()
        self.assertNotIn("label_repositories", plan)
        self.assertLessEqual({"factory:needs-human", "factory:map"}, set(plan["label_plan"][self.TRACKER]))
        self.assertEqual({r: v for r, v in plan["label_plan"].items() if r != self.TRACKER},
                         {self.WEB: [], self.DOCS: [], "zaharchenko-evgeny/slop-eval-factory": []})

    def test_reconcile_removes_labels_from_a_repository_no_longer_holding_tickets(self):
        plan = self.plan([self.TRACKER])
        have = {"factory:ready": ("c2e0c6", ""), "factory:hold": ("ededed", ""), "bug": ("ff0000", "")}
        run = fake_labels({self.TRACKER: {}, self.WEB: have, self.DOCS: {}, "zaharchenko-evgeny/slop-eval-factory": {}})
        got = deploy.reconcile_labels(plan, run=run)
        self.assertEqual([(a, n) for a, r, n, *_ in got if r == self.WEB],
                         [("remove", "factory:hold"), ("remove", "factory:ready")])  # `bug` is not factory-owned
        self.assertEqual([a for a, r, *_ in got if r == self.TRACKER].count("remove"), 0)
        self.assertIn(("create", self.TRACKER, "factory:map"), [x[:3] for x in got])


class FakeTags:
    """A YouTrack runner over a tag inventory {name: id}; records writes."""

    def __init__(self, tags, shared=()):
        self.tags, self.shared, self.writes = dict(tags), set(shared), []

    def __call__(self, method, path, body=None):
        if method == "GET" and path.startswith("/api/issues"):  # tags another project's issues carry
            return [{"idReadable": "X-1"}] if any(urllib.parse.quote(f"{{{t}}}") in path for t in self.shared) else []
        if method == "GET":
            return [{"id": i, "name": n} for n, i in self.tags.items()]
        self.writes.append((method, path, body))
        return {}


class YouTrackTagsTest(unittest.TestCase):
    TAGS = ["factory:implement-mid", "factory:ready"]
    PLAN = {"tags": TAGS, "owned_tag_prefixes": ["factory:"], "tag_tracker": {"url": "https://y.example", "project": "LME"},
            "labels": [], "label_repositories": [], "owned_label_prefixes": []}

    def test_required_tags_come_from_the_compiled_definition(self):
        plan = youtrack_plan(tag_store)
        self.assertIn("factory:ready", plan["tags"])
        self.assertIn("factory:implement-medium", plan["tags"])
        self.assertFalse([t for t in plan["tags"] if "<" in t])
        self.assertEqual(plan["owned_tag_prefixes"], ["factory:"])

    def run_deploy(self, mode, allow_delete, have, shared=()):
        fake = FakeTags(have, shared)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(air, "read_state", return_value=([], [])):
            code = air.deploy(mode, allow_delete, {**self.PLAN, "prefix": "p", "environments": [], "automations": []},
                                 None, None, yt_run=fake)
        return code, out.getvalue(), err.getvalue(), fake.writes

    def test_dry_run_reports_without_writing(self):
        code, out, _, writes = self.run_deploy("--dry-run", False, {"factory:ready": "1", "factory:old": "2", "bug": "3"})
        self.assertEqual(code, 0)
        self.assertIn("create tag factory:implement-mid in YouTrack", out)
        self.assertIn("remove tag factory:old in YouTrack", out)
        self.assertNotIn("tag bug", out)
        self.assertEqual(writes, [])

    COMMENT_CONFIG = {"plugins": ["comments"], "repositories": [],
                      "tracker": {"adapter": "youtrack", "url": "https://yt.example", "token_env": "TRACKER_TOKEN"}}

    def upload(self, apply, config=None, runner=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = youtrack_wake.sync_wake_files(config or self.COMMENT_CONFIG, None, apply, workflow_run=runner,
                                                 environ={"TRACKER_TOKEN": "secret-value"})
        return code, out.getvalue(), err.getvalue()

    def test_youtrack_comment_rule_dry_run_lists_upload_and_never_runs_it(self):
        runner = mock.Mock()
        code, out, _ = self.upload(False, runner=runner)
        self.assertEqual(code, 0)
        self.assertIn("npx --yes @jetbrains/youtrack-scripting upload " + str(youtrack_wake.WORKFLOW) +
                      " --host https://yt.example --token $TRACKER_TOKEN", out)
        self.assertNotIn("secret-value", out)
        runner.assert_not_called()
        self.assertEqual(youtrack_wake.WORKFLOW, ROOT / "factory/factory_plugins/youtrack/comment-wake")
        self.assertTrue((youtrack_wake.WORKFLOW / "comment-wake.js").is_file())
        out = io.StringIO()  # the core's `deploy.files` dispatch reaches the youtrack plugin's hook
        with contextlib.redirect_stdout(out):
            self.assertEqual(deploy.sync_wake_files(self.COMMENT_CONFIG, None, False), 0)
        self.assertIn("step YouTrack workflow upload: ", out.getvalue())

    def test_youtrack_comment_rule_needs_the_comments_plugin(self):
        runner = mock.Mock()
        code, out, _ = self.upload(True, config={**self.COMMENT_CONFIG, "plugins": []}, runner=runner)
        self.assertEqual((code, out), (0, ""))
        runner.assert_not_called()

    def test_youtrack_comment_rule_apply_runs_upload_with_named_secret(self):
        runner = mock.Mock()
        code, _, _ = self.upload(True, runner=runner)
        self.assertEqual(code, 0)
        runner.assert_called_once_with(["npx", "--yes", "@jetbrains/youtrack-scripting", "upload",
                                        str(youtrack_wake.WORKFLOW), "--host", "https://yt.example",
                                        "--token", "secret-value"], check=True)

    def test_youtrack_comment_rule_apply_without_token_fails(self):
        runner = mock.Mock()
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = youtrack_wake.sync_wake_files(self.COMMENT_CONFIG, None, True, workflow_run=runner, environ={})
        self.assertEqual(code, 1)
        self.assertIn("error: set TRACKER_TOKEN to upload the YouTrack workflow", err.getvalue())
        runner.assert_not_called()

    def test_youtrack_comment_rule_upload_failure_does_not_print_secret(self):
        runner = mock.Mock(side_effect=subprocess.CalledProcessError(1, ["npx", "--token", "secret-value"]))
        code, out, err = self.upload(True, runner=runner)
        self.assertEqual(code, 1)
        self.assertIn("YouTrack workflow upload failed with exit code 1", err)
        self.assertNotIn("secret-value", out + err)

    def test_apply_creates_missing_tags_and_refuses_removal_without_allow_delete(self):
        code, _, err, writes = self.run_deploy("--apply", False, {"factory:ready": "1", "factory:old": "2"})
        self.assertEqual(code, 1)
        self.assertIn("refused: would remove tag factory:old in YouTrack", err)
        self.assertEqual(writes, [("POST", "/api/tags?fields=id,name", {"name": "factory:implement-mid"})])

    def test_apply_with_allow_delete_removes_owned_tags_only(self):
        code, _, _, writes = self.run_deploy("--apply", True, {"factory:ready": "1", "factory:old": "2", "bug": "3"})
        self.assertEqual(code, 0)
        self.assertEqual([w[:2] for w in writes], [("POST", "/api/tags?fields=id,name"), ("DELETE", "/api/tags/2")])

    def test_tag_used_by_another_project_is_never_removed(self):
        code, out, _, writes = self.run_deploy("--apply", True, {"factory:ready": "1", "factory:shared": "2"}, shared=["factory:shared"])
        self.assertEqual(code, 0)
        self.assertNotIn("factory:shared", out)
        self.assertEqual([w[:2] for w in writes], [("POST", "/api/tags?fields=id,name")])


class FakeFieldValues:
    """A YouTrack runner over a project whose State bundle holds {value: [id, isResolved]}; records writes."""

    def __init__(self, values, field="State"):
        self.values, self.field, self.writes = dict(values), field, []

    def __call__(self, method, path, body=None):
        if method != "GET":
            self.writes.append((method, path, body))
            return {}
        if path.startswith("/api/admin/projects?"):
            return [{"id": "0-1", "shortName": "LME"}]
        return [{"field": {"name": self.field}, "bundle": {"id": "b1", "values": [
            {"id": i, "name": n, "isResolved": r} for n, (i, r) in self.values.items()]}}]


class YouTrackFieldValuesTest(unittest.TestCase):
    PLAN = {"fields": {"State": {"factory:ready": False, "factory:done": True}}, "owned_field_prefixes": ["factory:"],
            "tag_tracker": {"url": "https://y.example", "project": "LME"},
            "labels": [], "label_repositories": [], "owned_label_prefixes": []}

    def run_deploy(self, mode, allow_delete, values, field="State"):
        fake = FakeFieldValues(values, field)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(air, "read_state", return_value=([], [])):
            code = air.deploy(mode, allow_delete, {**self.PLAN, "prefix": "p", "environments": [], "automations": []},
                                 None, None, yt_run=fake)
        return code, out.getvalue(), err.getvalue(), fake.writes

    HAVE = {"factory:ready": ("1", False), "factory:done": ("2", False), "factory:old": ("3", False), "Open": ("4", False)}

    def test_plan_collects_field_values_with_final_states_resolved(self):
        def edit(conf):
            conf["states"]["ready"]["store"] = {"field": {"name": "State", "value": "factory:ready"}}
            conf["states"]["done"]["store"] = {"field": {"name": "State", "value": "factory:done"}}
        plan = youtrack_plan(edit)
        self.assertEqual(plan["fields"], {"State": {"factory:ready": False, "factory:done": True}})
        self.assertEqual(plan["owned_field_prefixes"], ["factory:"])

    def test_dry_run_reports_missing_changed_and_obsolete_values_without_writing(self):
        have = {k: v for k, v in self.HAVE.items() if k != "factory:ready"}
        code, out, _, writes = self.run_deploy("--dry-run", False, have)
        self.assertEqual(code, 0)
        self.assertIn("create State value factory:ready in YouTrack", out)
        self.assertIn("update State value factory:done in YouTrack", out)
        self.assertIn("remove State value factory:old in YouTrack", out)
        self.assertNotIn("Open", out)
        self.assertEqual(writes, [])

    def test_apply_writes_values_and_refuses_removal_without_allow_delete(self):
        have = {k: v for k, v in self.HAVE.items() if k != "factory:ready"}
        code, _, err, writes = self.run_deploy("--apply", False, have)
        self.assertEqual(code, 1)
        self.assertIn("refused: would remove State value factory:old in YouTrack", err)
        self.assertEqual([w[:2] for w in writes], [
            ("POST", "/api/admin/customFieldSettings/bundles/state/b1/values?fields=id,name"),
            ("POST", "/api/admin/customFieldSettings/bundles/state/b1/values/2?fields=id,name")])
        self.assertEqual(writes[0][2], {"name": "factory:ready", "isResolved": False})
        self.assertEqual(writes[1][2], {"isResolved": True})

    def test_apply_with_allow_delete_removes_owned_values_only(self):
        code, _, _, writes = self.run_deploy("--apply", True, self.HAVE)
        self.assertEqual(code, 0)
        self.assertEqual([w[:2] for w in writes], [
            ("POST", "/api/admin/customFieldSettings/bundles/state/b1/values/2?fields=id,name"),
            ("DELETE", "/api/admin/customFieldSettings/bundles/state/b1/values/3")])

    def test_missing_field_is_an_error_and_nothing_is_written(self):
        code, _, err, writes = self.run_deploy("--apply", True, self.HAVE, field="Other")
        self.assertEqual(code, 1)
        self.assertIn("field 'State' does not exist", err)
        self.assertEqual(writes, [])


class MissingMcpEnvTest(unittest.TestCase):
    def test_deploy_without_operator_vars_exits_2_not_keyerror(self):
        err = io.StringIO()
        env = {k: v for k, v in os.environ.items() if not k.startswith("FACTORY_MCP_AIR_")}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(deploy, "check_fixture_drift", lambda c: 0), \
                contextlib.redirect_stderr(err):
            self.assertEqual(deploy.main(["--dry-run"]), 2)
        self.assertIn("FACTORY_MCP_AIR_AUTOMATIONS", err.getvalue())


if __name__ == "__main__":
    unittest.main()

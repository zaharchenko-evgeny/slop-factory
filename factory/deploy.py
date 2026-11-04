#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Render the nodes of engine/nodes.yaml as deployable specs (ADR-0014). Output is JSON.

Usage: deploy.py plan --target air
       deploy.py (--dry-run | --apply [--allow-delete]) [--rename 'OLD NAME=NEW NAME']...
--dry-run prints the diff against the existing automations and environments; --apply also writes it and re-lists to
verify. The two Air MCP servers are started over stdio from the commands in FACTORY_MCP_AIR_AUTOMATIONS and
FACTORY_MCP_AIR_CLOUD (they use the operator's local OAuth). Each --rename updates the existing item named OLD in
place (same id) to NEW instead of creating NEW and deleting OLD.
air: {"automations": [...], "environments": [...]}: one automation per enabled node (per tier or focus
     where the node has them) and one environment per environment in use, for the factory-deploy skill.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod
import nodes as nodes_mod
import vocab

ROOT = Path(__file__).resolve().parents[1]
LEGACY_ENVIRONMENT_VARIABLES = {"FACTORY_TEAM_REPOSITORY"}
PR_TRIGGERED_NODES = {"review", "join", "fix", "update", "finish", "reopen"}
BOT_FILTERED = ("review-", "join", "reopen")  # start only for the bot's own PRs until `adopt` (#160) removes the filter


OUTCOME_WORDS = {
    "approve": "the change is ready",
    "changes": "list blocking findings",
    "proceed": "the ticket is ready for implementation",
    "grill": "list the questions that block work",
    "split": "propose independent ticket slices",
    "done": "the work is complete",
    "error": "the work needs the next tier",
    "blocked": "explain what blocks the work",
    "failed": "explain why the work failed",
    "not-actionable": "explain why the request cannot be acted on",
    "bug": "hand off the confirmed bug for implementation",
    "waiting": "the review round is still waiting",
    "approved": "all required reviewers approved",
}


def node_spec(name: str, n: dict, config: dict, tier: str | None, focus: str | None, nodes: dict, top: bool = True) -> dict:
    """What one automation's run needs from the node graph (ADR-0015): the node, its condition, its outcomes."""
    spec = {"engine": config.get("engine"), "node": name, "role": n["role"], "tier": tier, "focus": focus,
            "when": n.get("when"), "outcomes": n["outcomes"]}
    if n.get("routine"):
        spec["routine"] = True  # outside any ticket: `factory begin` needs no trigger payload
    if n.get("tiers"):
        spec["tiers"] = n["tiers"]  # the ordered tiers the gate escalates through
    if any((o or {}).get("else") for o in n["outcomes"].values()):
        spec["else_tier"] = nodes["nodes"]["fix"]["tiers"][0]  # the fix tier a red CI hands the ticket to
    if top and n["role"] == "script":  # a sweep applies the other script nodes (join, finish) too
        spec["runs"] = {k: node_spec(k, v, config, None, None, nodes, False) for k, v in nodes["nodes"].items()
                        if v["role"] == "script" and k != name and v.get("enabled") is not False and not v.get("routine")}
    return spec


def project_name(config: dict) -> str:
    return config["project"]["name"]


def flow_triggers(node: dict) -> str:
    words = []
    for trigger_text in node.get("triggers", []):
        event, _, label = str(trigger_text).partition(" ")
        event = (event.replace("issues.labeled", "ticket labeled")
                 .replace("pull_request.labeled", "pull request labeled")
                 .replace("pull_request_review.submitted", "pull request review submitted")
                 .replace("pull_request", "pull request").replace("workflow_run", "workflow run"))
        words.append(f"{event} {label}".strip())
    return " or ".join(words) or "manual invocation"


def trigger(text: str, tier: str | None, pr_author: str | None = None, focus: str | None = None) -> dict:
    """`issues.labeled factory:ready` → an EVENT trigger of create_automation (air-automations)."""
    event, _, arg = str(text).partition(" ")
    arg = arg.replace("<tier>", tier or "")
    arg = arg.replace("<focus>", focus or "")
    labeled = event.startswith("issues.") or event.startswith("pull_request.labeled")
    filters = {"label": arg} if labeled and arg else {}
    if pr_author and event.startswith("pull_request"):
        filters["pr_author"] = pr_author
    return {"triggerType": "EVENT", "source": "github", "eventType": event, "filters": filters}


def repository(r: dict) -> dict:
    """The `repository` input of create_automation from a factory.descriptor.yaml repository."""
    url = r["url"].rstrip("/")
    return {"fullName": "/".join(url.split("/")[-2:]), "cloneUrl": url + ".git", "browseUrl": url,
            "provider": "github" if "github.com" in url else "other"}


# A script node runs one command and judges nothing: the gate's sweep decides every scripted step (join, waiting
# reviewers, merged to done) from facts. `--main` runs it from main's graph (a PR-triggered node never trusts the PR).
SCRIPT_PROMPT = ("This step runs one command and judges nothing: it changes no file, label, comment or review "
                 "itself. Run `factory sweep{args}` in the foreground with no time limit; "
                 "wait until it exits, "
                 "and report its JSON output.")
PR_NUMBER = " --main --issue <the number after the leading # of the PR title, or else the body's Closes #<n>>"
SCRIPT_ARGS = {"join": PR_NUMBER, "start": " --main --issue <the issue number>", "finish": PR_NUMBER,
               "reopen": PR_NUMBER, "merge": PR_NUMBER,
               "doctor": " doctor", "doctor-write": " doctor", "doctor-ops": " doctor",
               "notify": " notify --issue <the issue number>"}


def checkout_instructions(main_repository: dict | None, config: dict) -> str:
    primary = next(r for r in config["repositories"] if r.get("role") == "primary")
    team = repository({"url": config.get("team_repository") or primary["url"]})
    if not main_repository or main_repository["fullName"] == team["fullName"]:
        return ""
    project = main_repository["fullName"].rsplit("/", 1)[-1]
    return (f"## Project checkout\n\nBefore running project commands, `cd /workspaces/{project}`. "
            "The factory repository is checked out beside it and is read-only.\n\n")


def script_prompt(name: str, main_repository: dict | None = None, config: dict | None = None) -> str:
    checkout = checkout_instructions(main_repository, config) if config else ""
    if name.startswith("doctor"):
        prompt = "Run `factory doctor` once and report its JSON lines. It changes no ticket itself."
    elif name == "reconcile":
        prompt = ("This step runs one command and judges nothing: it changes no file, label, comment or review itself. "
                  "Run `factory reconcile` in the foreground with no time limit; wait until it exits, and report its JSON output.")
    elif name == "notify":
        prompt = ("Run `factory notify --issue <the issue number>` in the foreground with no "
                  "time limit, wait until it exits, and report its output.")
    else:
        args = SCRIPT_ARGS.get(name, "")
        prompt = SCRIPT_PROMPT.format(args=args)
        if "--issue" in args:
            prompt += " Copy only that one number; never write the payload or any JSON."
    return checkout + prompt


def skill_lines(role: str) -> str:
    """One line per skill of the role: its name, its front-matter description and its file."""
    skills = config_mod.resolve(f"roles/{role}/skills.txt")
    lines = []
    for skill in skills.read_text().split() if skills.exists() else []:
        path = config_mod.resolve(f"skills/{skill}/SKILL.md")
        text = path.read_text()
        meta = yaml.safe_load(text.split("---")[1]) if text.startswith("---") else {}
        desc = " ".join(str(meta.get("description", "")).split())
        shown = path.relative_to(config_mod.team_root()).as_posix()
        lines.append(f"- {skill}: {desc} (file: `{shown}`)")
    return "\n".join(lines)


def this_step(node: dict, focus: str | None) -> str:
    outcomes = "\n".join(f"- `{o}`: {OUTCOME_WORDS.get(o, o.replace('-', ' '))}"
                         for o, v in node["outcomes"].items() if (v or {}).get("enabled") is not False)
    return f"Woken by: {flow_triggers(node)}.\n\nOutcomes:\n{outcomes}"


def begin_arg(node: str, n: dict) -> str:
    """The number a node begins with; a scheduled routine has no ticket or PR, so it takes none."""
    if all(str(t).startswith("schedule ") for t in n["triggers"]):
        return ""
    return "--pr <the pull request number>" if node in PR_TRIGGERED_NODES else "--issue <the issue number>"


def agent_prompt(name: str, n: dict, config: dict, aname: str, focus: str | None,
                 main_repository: dict | None = None) -> str:
    role = n["role"]
    who = f"You are the {role} of {project_name(config)}" + (f" ({focus} focus)" if focus else "") + "."
    text = "\n\n".join(config_mod.resolve(f).read_text().strip() for f in (f"roles/{role}/role.md", "roles/_common.md"))
    checkout = checkout_instructions(main_repository, config)
    arg = begin_arg(name, n)
    arg = f" {arg}" if arg else ""
    return (f"{who}\n\n{checkout}{text}\n\n## Your skills\n\n{skill_lines(role)}\n\n## This step\n\n{this_step(n, focus)}\n\n"
            "## How you start and finish\n\n"
            f"1. Run `factory begin {aname}{arg}`.{' Copy only that one number; never write the payload or any JSON.' if arg else ''} "
            "It prints the ticket, the facts, the branch or PR head and the handoff path. "
            "If it exits 10 the node must not act: report the reason and stop.\n"
            "2. Do the role's work, write the handoff to the handoff path, then run `factory end <handoff_path>`.")


def variants(name: str, n: dict, roles: dict) -> list[tuple[str, str | None, str, str | None]]:
    """(automation suffix, tier of the node, model tier, focus) per tier or focus of the node."""
    role = roles.get(n["role"], {})
    if n.get("tiers"):
        return [(f"{name}-{t}", t, t, None) for t in n["tiers"]]
    if n.get("focuses"):
        return [(f"{name}-{f}", None, role["modes"][f]["tier"], f) for f in n["focuses"]]
    return [(name, None, n.get("tier") or role.get("tier") or "cheap", None)]


def compile_automation(automation: str, nodes: dict, config: dict) -> dict:
    """The node, gate options and spec of one automation (`review-behaviour`, `fix-mid`) from the node graph."""
    roles = config["roles"]
    for name, n in nodes["nodes"].items():
        if n.get("enabled") is False or n["role"] in ("human", "script"):
            continue
        for aname, tier_arg, tier, focus in variants(name, n, roles):
            if aname == automation:
                return {"node": name, "focus": focus, "tier": tier_arg or (tier if focus else None),
                        "spec": node_spec(name, n, config, tier_arg or tier, focus, nodes)}
    raise KeyError(f"unknown automation: {automation}")


def plan_air(nodes: dict, config: dict) -> dict:
    air, roles = config["targets"]["air"]["models"], config["roles"]
    repos = {r["name"]: r for r in config["repositories"]}
    primary = next(r for r in config["repositories"] if r.get("role") == "primary")
    team = {"name": "team", "url": config.get("team_repository") or primary["url"]}
    repos["team"] = team
    envs_def = nodes.get("environments") or {}
    scheds = nodes.get("schedules") or {}
    bot = config["targets"]["air"]["bot_login"]
    prefix = f"[{project_name(config)}] "
    automations = []
    used = list(dict.fromkeys(n["env"] for n in nodes["nodes"].values() if n["role"] != "human" and not n.get("runner")))
    for name, n in nodes["nodes"].items():
        if n.get("enabled") is False or n["role"] == "human" or n.get("runner"):
            continue
        env = n["env"]
        for aname, tier_arg, tier, focus in variants(name, n, roles):
            agent, _, model = air[tier].partition("/")
            trig = []
            spec = node_spec(name, n, config, tier_arg or tier, focus, nodes)
            for t in n["triggers"]:
                if str(t).startswith("schedule "):
                    trig.append({"triggerType": "SCHEDULED", "cronExpression": scheds[str(t)[9:]]["cron"],
                                 "timezone": "UTC"})
                elif str(t) != "manual":
                    trig.append(trigger(t, tier_arg, bot if aname.startswith(BOT_FILTERED) else None, focus))
            automation_repository = repository(team if focus == "knowledge" else repos.get(envs_def[env]["repository"]) or primary)
            automation = {
                "name": f"{prefix}{aname}",
                "triggers": trig,
                "triggerVcs": [{"outputBehaviour": n.get("output", "read-only")} for _ in trig],
                "spec": spec,
                "agentAndModel": {"id": model, "name": model, "isDefault": False, "agentId": agent},
                "repository": automation_repository,
                "environmentConfigId": f"{prefix}{env}",
                "prompt": script_prompt(name, automation_repository, config) if n["role"] == "script" else agent_prompt(
                    name, n, config, aname, focus, automation_repository),
            }
            if automation_repository["fullName"] != repository(team)["fullName"]:
                automation["additionalRepositories"] = [{"repository": repository(team), "branch": "main"}]
            automations.append(automation)
    envs = []
    for env in used:
        d = envs_def[env]
        repo = repos.get(d["repository"]) or primary
        envs.append({
            "name": f"{prefix}{env}",
            "repository": repo["url"],
            "startup": d["startup"],
            "variables": {"FACTORY_ENV": env},
            "secrets": d.get("secrets", []),
            "domains": d.get("domains", []),
            "vm": d.get("vm", "default"),
        })
    return {"prefix": prefix, "automations": automations, "environments": envs, "labels": labels(nodes, config),
            "files": actions_files(nodes, config)}


def actions_files(nodes: dict, config: dict) -> dict:
    """{path: text} of the GitHub Actions workflows the graph's `runner: actions` nodes render, in the factory repository only."""
    scheds, files = nodes.get("schedules") or {}, {}
    for name, n in nodes["nodes"].items():
        if n.get("runner") == "actions" and n.get("enabled") is not False:
            cron = next(scheds[str(t)[9:]]["cron"] for t in n["triggers"] if str(t).startswith("schedule "))
            wake = "".join(f"  # jactionlint ignore=dangerous-triggers\n  workflow_run:\n    workflows: [{str(t)[13:]}]\n    types: [completed]\n"
                           for t in n["triggers"] if str(t).startswith("workflow_run "))
            events = {}
            for t in n["triggers"]:
                if str(t).startswith(("issues.", "pull_request_review.")):
                    event, _, kind = str(t).partition(".")
                    events.setdefault(event, []).append(kind)
            wake = "".join(f"  {e}:\n    types: [{', '.join(k)}]\n" for e, k in events.items()) + wake
            files[f".github/workflows/factory-{name}.yml"] = ACTIONS_WORKFLOW.format(
                name=name, project=project_name(config), cron=cron, wake=wake, run=n["run"].replace("uv run factory/cli.py", "uv run --quiet factory/cli.py"))
    return files


# The token is the built-in github.token while the factory repository is also the target (#339); labels it sets wake Air.
ACTIONS_WORKFLOW = """# Generated by `factory deploy` from engine/nodes.yaml (node `{name}`). Do not edit.
name: factory-{name}

on:
  schedule:
    - cron: "{cron}"
{wake}  workflow_dispatch:

permissions: {{}}

concurrency:
  group: factory-{name}
  cancel-in-progress: false

jobs:
  {name}:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
      issues: write
      pull-requests: write
    env:
      GH_TOKEN: ${{{{ github.token }}}}
      GH_REPO: ${{{{ github.repository }}}}
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
        with: {{ persist-credentials: false }}
      - run: pipx install uv
      - run: {run}
"""


def labels(nodes: dict, config: dict) -> list[str]:
    """Every label the gate may write or a trigger filters on: stage labels, risk and complexity labels, and
    the command labels of the nodes with `<tier>` expanded over each node's tiers (a missing label makes
    `gh issue edit --add-label` fail, so the deploy creates them first)."""
    t = config.get("tracker") or {}
    out = set((t.get("stages") or {}).values()) | set((t.get("risk_labels") or {"low": "risk:low", "high": "risk:high"}).values())
    out.add(vocab.needs_human())
    out |= {f"complexity:{c}" for c in ("small", "medium", "large")}
    tiers = sorted({x for n in nodes["nodes"].values() for x in n.get("tiers") or []}) or ["cheap", "mid", "strong"]
    focuses = sorted({x for n in nodes["nodes"].values() for x in n.get("focuses") or []})
    found, namespace = [], ((t.get("stages") or {}).get("ready") or vocab.stage("ready")).partition(":")[0] + ":"
    if "states" in config:  # factory.project.yaml: the labels its state and command stores name (ADR-0016)
        facts = t.get("pr_fact_labels") or {}
        found += [s["store"].get("label") or facts.get(s["store"].get("pr")) for s in config["states"].values()]
        found += [c["store"].get("label") or c["store"].get("pr_label") for c in (config.get("commands") or {}).values()]
        found = [x for x in found if x]
    for n in nodes["nodes"].values() if "states" not in config else []:
        found += [t.split(" ", 1)[1] for t in n.get("triggers") or [] if t.endswith(">") or t.count(" ") == 1
                  and t.split(" ", 1)[1].startswith(namespace)]
        found += [v for o in (n.get("outcomes") or {}).values() for e in (o or {}).get("effects") or []
                  for k, v in e.items() if k == "label"]
    for x in found:
        if "<tier>" in x:
            out |= {x.replace("<tier>", t) for t in tiers}
        elif "<focus>" in x:
            out |= {x.replace("<focus>", f) for f in focuses}
        elif "<" not in x:
            out.add(x)
    out |= set((t.get("issue_labels") or {}).values())
    return sorted(out)


class Mcp:
    """A minimal MCP client over stdio (newline-delimited JSON-RPC): initialize, then tools/call."""

    def __init__(self, command: str):
        self.proc = subprocess.Popen(shlex.split(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "factory-deploy", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def rpc(self, method: str, params: dict):
        self.n += 1
        self._send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        for line in self.proc.stdout:
            msg = json.loads(line)
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg["result"]
        raise RuntimeError(f"{method}: server closed")

    def call(self, tool: str, args: dict):
        res = self.rpc("tools/call", {"name": tool, "arguments": args})
        if res.get("isError"):
            raise RuntimeError(f"{tool}: {res['content']}")
        text = "".join(c.get("text", "") for c in res.get("content", []))
        return json.loads(text) if text else res.get("structuredContent")  # the text is the tool's JSON; structuredContent may wrap it

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()


class Backend:
    """The fields not yet exposed by air-automations MCP, through the Air automation REST API."""

    SPEC_FIELDS = ("name", "repository", "branch", "prompt", "agentAndModel", "triggers", "triggerVcs", "kind",
                   "executor", "botDevSettings", "paused", "outputDescription", "outboundWebhooks", "mcpConnectors",
                   "environmentConfigId", "projectId")
    API_BASE_URL = "https://api.jetbrains.cloud/air-automations-backend/api/v1"

    def __init__(self, base_url: str | None = None, token: str | None = None, run=subprocess.run):
        self.base_url = (base_url or self.API_BASE_URL).rstrip("/")
        self._token = token
        self.run = run

    def token(self) -> str:
        if self._token is None:
            env = os.environ.copy()
            env.update({"JCP_MCP_API_URL": "https://api.jetbrains.cloud", "JCP_MCP_CLIENT_ID": "pi-lot",
                        "JCP_MCP_REDIRECT_PATH": "/space/auth",
                        "JCP_MCP_CONFIG_DIR": str(Path.home() / ".config/jcp-mcp-bridge-pilot")})
            result = self.run(["jcp-mcp-bridge", "token", "--env", "prod", "--audience", "air-automations-backend"],
                              check=True, capture_output=True, text=True, env=env)
            self._token = result.stdout.strip()
            if not self._token:
                raise RuntimeError("jcp-mcp-bridge returned an empty Air automation token")
        return self._token

    def request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token()}",
                                              "Accept": "application/json", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}

    def get_automation(self, automation_id: str) -> dict:
        return self.request("GET", f"/automations/{urllib.parse.quote(automation_id, safe='')}")

    def set_additional(self, automation_id: str, repositories: list[dict]) -> None:
        current = self.get_automation(automation_id)
        body = {key: current[key] for key in self.SPEC_FIELDS if key in current}
        if "triggers" in body:
            body["triggers"] = [{key: value for key, value in trigger.items()
                                 if key not in ("triggerId", "automationSnapshotId")}
                                for trigger in body["triggers"]]
        body.update({"automationId": current.get("automationId", automation_id),
                     "additionalRepositories": repositories})
        self.request("POST", "/automations", body)


def _items(res) -> list[dict]:
    return res if isinstance(res, list) else next(v for v in res.values() if isinstance(v, list))


def same(key: str, have, want) -> bool:
    if key == "repository" and isinstance(have, dict) and have.keys() == {"fullName"}:
        return have["fullName"] == want["fullName"]
    if key == "additionalRepositories":
        def identities(items):
            return sorted((item.get("repository", {}).get("fullName") if isinstance(item.get("repository"), dict)
                           else item.get("repository"), item.get("branch")) for item in (items or []))
        return identities(have) == identities(want)
    if key == "domains":
        return set(want or []) <= set(have or [])
    return have == want


def environment_detail(x: dict) -> dict:
    """Normalize get_environment's cloud fields to the deploy plan's environment shape."""
    x = dict(x)
    if {"serviceHost", "organization", "repositoryName"} <= x.keys():
        x["repository"] = f"https://{x.pop('serviceHost')}/{x.pop('organization')}/{x.pop('repositoryName')}"
    if "allowedDomains" in x:
        x["domains"] = x.pop("allowedDomains")
    if "instanceTypeKey" in x:
        x["vm"] = x.pop("instanceTypeKey") or "default"
    if isinstance(x.get("variables"), list):
        x["variables"] = {v["key"]: v["value"] for v in x["variables"]
                           if not v.get("isSecret") and v.get("value") not in ("", None)}
    elif isinstance(x.get("variables"), dict):
        x["variables"] = {k: v for k, v in x["variables"].items() if v not in ("", None)}
    if "personalSecrets" in x:
        x["secrets"] = [s["name"] for s in x.pop("personalSecrets")]
    return x


def diff(plan: dict, automations: list[dict], envs: list[dict]) -> list[tuple]:
    """(action, kind, name, planned item, existing item, changed fields) per create, update, delete; by name, prefix only.
    `plan["renames"]` ({old name: new name}) matches an existing item under its old name, so the update keeps its id."""
    new_to_old = {new: old for old, new in (plan.get("renames") or {}).items()}
    env_ids = {e["name"]: e.get("id") for e in envs}
    out = []
    for kind, planned, have in (("environment", plan["environments"], envs), ("automation", plan["automations"], automations)):
        have = {x["name"]: x for x in have if x["name"].startswith(plan["prefix"])}
        for raw in planned:
            p = {k: v for k, v in raw.items() if k != "spec"}
            x = have.pop(p["name"], None) or have.pop(new_to_old.get(p["name"]), None)
            want = {**p, "environmentConfigId": env_ids.get(p.get("environmentConfigId"), p.get("environmentConfigId"))}
            changed = [k for k in p if x is not None and (kind != "environment" or k in x)
                       and not same(k, x.get(k), want[k])]
            if x is None or changed:
                out.append(("create" if x is None else "update", kind, p["name"], p, x, changed))
        out += [("delete", kind, n, None, x, []) for n, x in have.items()]
    return out


def automation_detail(x: dict) -> dict:
    """A get_automation result as the plan's shape: `id` for `automationId`, no per-trigger server fields."""
    x = {**x, "id": x["automationId"]}
    x["triggers"] = [{k: v for k, v in t.items() if k not in ("triggerId", "isEnabled", "automationSnapshotId")}
                     for t in x.get("triggers") or []]
    if isinstance(x.get("repository"), str):  # "owner/repo" stands for the planned repository object
        x["repository"] = {"fullName": x["repository"]}
    return x


def read_state(automations: Mcp, cloud: Mcp, prefix: str, repository_url: str, *, backend=None,
               additional_names=()) -> tuple[list[dict], list[dict]]:
    listed = automations.call("list_automations", {"privacy": "private"})
    automation_items = listed if isinstance(listed, list) else listed["personal"]
    automation_items = [automation_detail(automations.call("get_automation", {"automationId": x["automationId"]}))
                        for x in automation_items if x["name"].startswith(prefix)]
    if backend:
        for item in automation_items:
            if item["name"] in additional_names:
                record = backend.get_automation(item["id"])
                item["additionalRepositories"] = record.get("additionalRepositories") or []
    env_items = _items(cloud.call("list_environments", {"repositoryUrl": repository_url}))
    env_items = [environment_detail(cloud.call("get_environment", {"id": x["id"]}))
                 for x in env_items if x["name"].startswith(prefix)]
    return automation_items, env_items


def apply_diff(changes: list[tuple], automations: Mcp, cloud: Mcp, repo_url: str = "", *, backend=None) -> None:
    """Environments before automations on create and update, after them on delete."""
    order = {"create": 0, "update": 0, "delete": 1}
    env_ids = None
    for action, kind, name, p, x, changed in sorted(changes, key=lambda c: (order[c[0]], (c[1] == "automation") == (c[0] != "delete"))):
        client, noun = (cloud, "environment") if kind == "environment" else (automations, "automation")
        additional = (p or {}).get("additionalRepositories") if kind == "automation" else None
        if p and kind == "automation":  # the environment id exists only after its create
            env_ids = env_ids or {e["name"]: e["id"] for e in _items(cloud.call("list_environments", {"repositoryUrl": repo_url}))}
            p = {**p, "environmentConfigId": env_ids[p["environmentConfigId"]]}
        p = {k: v for k, v in (p or {}).items() if k not in ("spec", "additionalRepositories")}
        if action == "update":
            p = {k: v for k, v in p.items() if k in changed}
        if kind == "environment" and isinstance(p.get("variables"), dict):
            live_variables = (x or {}).get("variables") or {}
            variables = [{"key": key, "value": value} for key, value in p["variables"].items()]
            variables.extend({"key": key, "value": ""} for key in sorted(LEGACY_ENVIRONMENT_VARIABLES)
                             if key in live_variables and key not in p["variables"])
            p["variables"] = variables
        tool_action = ("edit" if kind == "environment" else "update") if action == "update" else action
        id_key = "automationId" if kind == "automation" else "id"
        if action == "update":
            if p:
                client.call(f"{tool_action}_{noun}", {**p, id_key: x["id"]})
            if additional is not None and "additionalRepositories" in changed:
                if backend is None:
                    raise RuntimeError("Air automation backend is required to set additionalRepositories")
                backend.set_additional(x["id"], additional)
        elif action == "create":
            result = client.call(f"create_{noun}", p)
            if additional is not None:
                if backend is None:
                    raise RuntimeError("Air automation backend is required to set additionalRepositories")
                automation_id = result.get("automationId") or result.get("id")
                if not automation_id:
                    raise RuntimeError(f"create_automation returned no automation id for {name}")
                backend.set_additional(automation_id, additional)
        else:
            client.call(f"delete_{noun}", {id_key: x["id"]})


def canary_body(deploy_id: str) -> str:
    return (f"[factory] Canary for deploy {deploy_id}. Write the deploy timestamp as one line to `docs/canary.md`.\n\n"
            "## Acceptance checks\n\n"
            f"- [ ] `grep -q {deploy_id} docs/canary.md`\n\n"
            "Affected repositories: slop-factory. Size: about 1 line with tests.\n\n"
            f"Factory-Run: deploy:{deploy_id}")


def failure_body(deploy_id: str, stage: str, summary: str, reason: str) -> str:
    return (f"[factory] Canary for deploy {deploy_id} did not reach `done`.\n\n"
            f"Stage: {stage}\nReason: {reason}\nLast run summary: {summary or 'none'}\n\n"
            f"Factory-Run: deploy:{deploy_id}")


def canary_lost_run(gate_tracker, ticket, now=None):
    """Why the canary's current step lost its run, from the reconcile dry run on that ticket alone: an Air agent that
    exited after its waking label without an outcome leaves no sweep exception, only a step past its time-out."""
    import reconcile
    config = config_mod.load()
    results, _ = reconcile.reconcile(gate_tracker, nodes_mod.load(), list(config["tracker"]["stages"]),
                                     config["limits"], now=now, dry_run=True, tickets=[ticket],
                                     stage_kinds=config["tracker"].get("stage_kinds", {}))
    return next((r["why"] for r in results if r.get("reconcile") in ("refire", "blocked")), None)


def wait_for_canary(tracker, ticket, deploy_id, *, sleep=time.sleep, clock=time.monotonic,
                    timeout=45 * 60, interval=30, lost_run=None, lost_every=5 * 60):
    deadline = clock() + timeout
    next_lost = clock()
    while True:
        status = tracker.canary_status(ticket)
        stage = status["stage"]
        if stage == "done":
            return 0
        reason = "node error" if status["error"] or stage in ("blocked", "failed", "error") else None
        if not reason and lost_run and clock() >= next_lost:  # a dozen reads per probe: not on every poll
            next_lost = clock() + lost_every
            why = lost_run(ticket)
            reason = why and f"node error: {why}"
        if reason or clock() >= deadline:
            reason = reason or "timed out after 45 minutes"
            failure = tracker.create_issue(f"Canary failed after deploy {deploy_id}",
                                           failure_body(deploy_id, stage, status["summary"], reason),
                                           risk="low", stage="triage")
            tracker.add_to_map(failure)
            print(f"canary failed: stage={stage}; last run summary={status['summary'] or 'none'}; {reason}",
                  file=sys.stderr)
            return 1
        sleep(min(interval, deadline - clock()))


def create_canary(tracker, deploy_id, *, sleep=time.sleep, clock=time.monotonic, lost_run=None):
    for old in tracker.canary_tickets():
        tracker.close_issue(old, f"[factory] Superseded by deploy {deploy_id}.\n\nFactory-Run: deploy:{deploy_id}")
    ticket = tracker.create_issue(f"Canary after deploy {deploy_id}", canary_body(deploy_id), risk="low", stage="triage")
    tracker.add_to_map(ticket)
    tracker.add_label(ticket, vocab.issue_label("canary"))
    return wait_for_canary(tracker, ticket, deploy_id, sleep=sleep, clock=clock, lost_run=lost_run)


def deploy(mode: str, allow_delete: bool, plan: dict, automations: Mcp, cloud: Mcp, *, backend=None, tracker=None,
           deploy_id=None, sleep=time.sleep, clock=time.monotonic) -> int:
    repo_url = plan["environments"][0]["repository"] if plan["environments"] else ""
    additional_names = {a["name"] for a in plan["automations"] if "additionalRepositories" in a}
    backend = backend or (Backend() if additional_names else None)
    state = read_state(automations, cloud, plan["prefix"], repo_url, backend=backend,
                       additional_names=additional_names)
    changes = diff(plan, *state)
    for action, kind, name, _, _, changed in changes:
        print(f"{action} {kind} {name}" + (f" ({', '.join(changed)})" if changed else ""))
    if not changes:
        print("unchanged")
    if mode == "--dry-run":
        return 0
    if any(c[0] == "delete" for c in changes) and not allow_delete:
        print("refused: the diff deletes items; rerun with --allow-delete", file=sys.stderr)
        return 1
    if changes:
        apply_diff(changes, automations, cloud, repo_url, backend=backend)
        left = diff(plan, *read_state(automations, cloud, plan["prefix"], repo_url, backend=backend,
                                      additional_names=additional_names))
        for action, kind, name, *_ in left:
            print(f"verify failed: {action} {kind} {name} still differs", file=sys.stderr)
        if left:
            return 1
    if tracker:
        deploy_id = deploy_id or datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return create_canary(tracker, deploy_id, sleep=sleep, clock=clock)
    return 0


def write_files(files: dict, apply: bool) -> int:
    """Create or update the rendered workflow files under the factory repository's root (never a target repository)."""
    root = config_mod.team_root()
    for path, text in files.items():
        target = root / path
        if target.exists() and target.read_text() == text:
            continue
        print(f"{'update' if target.exists() else 'create'} file {path}")
        if apply:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
    return 0


def main(argv: list[str]) -> int:
    argv = config_mod.pop_team(argv)
    rest, renames = argv[1:], {}
    while "--rename" in rest:
        i = rest.index("--rename")
        old, sep, new = (rest[i + 1] if i + 1 < len(rest) else "").partition("=")
        if not (old and sep and new):
            print("--rename needs 'OLD NAME=NEW NAME'", file=sys.stderr)
            return 2
        renames[old] = new
        del rest[i:i + 2]
    if argv and argv[0] in ("--dry-run", "--apply") and set(rest) <= {"--allow-delete"}:
        servers = [Mcp(os.environ[v]) for v in ("FACTORY_MCP_AIR_AUTOMATIONS", "FACTORY_MCP_AIR_CLOUD")]
        try:
            plan = {**plan_air(nodes_mod.load(), config_mod.load()), "renames": renames}
            code = deploy(argv[0], "--allow-delete" in argv, plan, *servers)
            code = code or write_files(plan["files"], argv[0] == "--apply")
            if code == 0 and argv[0] == "--apply":
                from github_tracker import GitHubTracker
                tracker = GitHubTracker.from_config()
                from gate_github import GitHubGateTracker
                gate_tracker = GitHubGateTracker.from_config()
                gate_tracker.deploy_applied(plan)
                deploy_id = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
                code = create_canary(tracker, deploy_id, lost_run=lambda t: canary_lost_run(gate_tracker, t))
            return code
        finally:
            for server in servers:
                server.close()
    if len(argv) != 3 or argv[0] != "plan" or argv[1] != "--target" or argv[2] != "air":
        print(__doc__)
        return 2
    config = config_mod.load()
    out = plan_air(nodes_mod.load(), config)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

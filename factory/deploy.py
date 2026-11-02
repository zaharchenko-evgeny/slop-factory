#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Render the nodes of workflows/nodes.yaml as deployable specs (ADR-0014). Output is JSON.

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
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod
import nodes as nodes_mod

ROOT = Path(__file__).resolve().parents[1]
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
    if n.get("tiers"):
        spec["tiers"] = n["tiers"]  # the ordered tiers the gate escalates through
    if any((o or {}).get("else") for o in n["outcomes"].values()):
        spec["else_tier"] = nodes["nodes"]["fix"]["tiers"][0]  # the fix tier a red CI hands the ticket to
    if top and n["role"] == "script":  # a sweep applies the other script nodes (join, finish) too
        spec["runs"] = {k: node_spec(k, v, config, None, None, nodes, False) for k, v in nodes["nodes"].items()
                        if v["role"] == "script" and k != name and v.get("enabled") is not False}
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


def trigger(text: str, tier: str | None, pr_author: str | None = None) -> dict:
    """`issues.labeled factory:ready` → an EVENT trigger of create_automation (air-automations)."""
    event, _, arg = str(text).partition(" ")
    arg = arg.replace("<tier>", tier or "")
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
                 "itself. Run `factory sweep{args}` once and report its JSON output.")
SCRIPT_ARGS = {"join": " --main", "finish": " --main --issue <issue number from the trigger payload>",
               "reopen": " --main",
               "recover": " --stale"}


def script_prompt(name: str) -> str:
    return SCRIPT_PROMPT.format(args=SCRIPT_ARGS.get(name, ""))


def skill_lines(role: str) -> str:
    """One line per skill of the role: its name, its front-matter description and its file."""
    skills = config_mod.resolve(f"roles/{role}/skills.txt")
    lines = []
    for skill in skills.read_text().split() if skills.exists() else []:
        path = next(ROOT.glob(f"skills/**/{skill}/SKILL.md"))
        text = path.read_text()
        meta = yaml.safe_load(text.split("---")[1]) if text.startswith("---") else {}
        desc = " ".join(str(meta.get("description", "")).split())
        lines.append(f"- {skill}: {desc} (file: `{path.relative_to(ROOT)}`)")  # the path until skills install (#213)
    return "\n".join(lines)


def this_step(node: dict, focus: str | None) -> str:
    outcomes = "\n".join(f"- `{o}`: {OUTCOME_WORDS.get(o, o.replace('-', ' '))}"
                         for o, v in node["outcomes"].items() if (v or {}).get("enabled") is not False)
    return f"Woken by: {flow_triggers(node)}.\n\nOutcomes:\n{outcomes}"


def agent_prompt(name: str, n: dict, config: dict, aname: str, focus: str | None) -> str:
    role = n["role"]
    who = f"You are the {role} of {project_name(config)}" + (f" ({focus} focus)" if focus else "") + "."
    text = "\n\n".join(config_mod.resolve(f).read_text().strip() for f in (f"roles/{role}/role.md", "roles/_common.md"))
    return (f"{who}\n\n{text}\n\n## Your skills\n\n{skill_lines(role)}\n\n## This step\n\n{this_step(n, focus)}\n\n"
            "## How you start and finish\n\n"
            f"1. Run `factory begin {aname}` with the trigger payload (the <trigger_event> JSON) on stdin, as a heredoc. "
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
    envs_def = nodes.get("environments") or {}
    scheds = nodes.get("schedules") or {}
    bot = config["targets"]["air"]["bot_login"]
    prefix = f"[{project_name(config)}] "
    automations = []
    used = list(dict.fromkeys(n["env"] for n in nodes["nodes"].values() if n["role"] != "human"))
    for name, n in nodes["nodes"].items():
        if n.get("enabled") is False or n["role"] == "human":
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
                    trig.append(trigger(t, tier_arg, bot if aname.startswith(BOT_FILTERED) else None))
            automations.append({
                "name": f"{prefix}{aname}",
                "triggers": trig,
                "triggerVcs": [{"outputBehaviour": n.get("output", "read-only")} for _ in trig],
                "spec": spec,
                "agentAndModel": {"id": model, "name": model, "isDefault": False, "agentId": agent},
                "repository": repository(repos.get(envs_def[env]["repository"]) or primary),
                "environmentConfigId": f"{prefix}{env}",
                "prompt": script_prompt(name) if n["role"] == "script" else agent_prompt(name, n, config, aname, focus),
            })
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
    return {"prefix": prefix, "automations": automations, "environments": envs, "labels": labels(nodes, config)}


def labels(nodes: dict, config: dict) -> list[str]:
    """Every label the gate may write or a trigger filters on: stage labels, risk and complexity labels, and
    the command labels of the nodes with `<tier>` expanded over each node's tiers (a missing label makes
    `gh issue edit --add-label` fail, so the deploy creates them first)."""
    t = config.get("tracker") or {}
    out = set((t.get("stages") or {}).values()) | set((t.get("risk_labels") or {"low": "risk:low", "high": "risk:high"}).values())
    out |= {f"complexity:{c}" for c in ("small", "medium", "large")}
    tiers = sorted({x for n in nodes["nodes"].values() for x in n.get("tiers") or []}) or ["cheap", "mid", "strong"]
    found = []
    for n in nodes["nodes"].values():
        found += [t.split(" ", 1)[1] for t in n.get("triggers") or [] if t.endswith(">") or t.count(" ") == 1
                  and t.split(" ", 1)[1].startswith("factory:")]
        found += [v for o in (n.get("outcomes") or {}).values() for e in (o or {}).get("effects") or []
                  for k, v in e.items() if k == "label"]
    for x in found:
        out |= {x.replace("<tier>", t) for t in tiers} if "<tier>" in x else {x} if "<" not in x else set()
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


def _items(res) -> list[dict]:
    return res if isinstance(res, list) else next(v for v in res.values() if isinstance(v, list))


def same(key: str, have, want) -> bool:
    if key == "repository" and isinstance(have, dict) and have.keys() == {"fullName"}:
        return have["fullName"] == want["fullName"]
    return have == want


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
            changed = [k for k in p if x is not None and not same(k, x.get(k), want[k])]
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


def read_state(automations: Mcp, cloud: Mcp, prefix: str, repository_url: str) -> tuple[list[dict], list[dict]]:
    listed = automations.call("list_automations", {"privacy": "private"})
    automation_items = listed if isinstance(listed, list) else [x for group in listed.values() for x in group]
    automation_items = [automation_detail(automations.call("get_automation", {"automationId": x["automationId"]}))
                        for x in automation_items if x["name"].startswith(prefix)]
    env_items = _items(cloud.call("list_environments", {"repositoryUrl": repository_url}))
    env_items = [cloud.call("get_environment", {"id": x["id"]})
                 for x in env_items if x["name"].startswith(prefix)]
    return automation_items, env_items


def apply_diff(changes: list[tuple], automations: Mcp, cloud: Mcp, repo_url: str = "") -> None:
    """Environments before automations on create and update, after them on delete."""
    order = {"create": 0, "update": 0, "delete": 1}
    env_ids = None
    for action, kind, name, p, x, _ in sorted(changes, key=lambda c: (order[c[0]], (c[1] == "automation") == (c[0] != "delete"))):
        client, noun = (cloud, "environment") if kind == "environment" else (automations, "automation")
        if p and kind == "automation":  # the environment id exists only after its create
            env_ids = env_ids or {e["name"]: e["id"] for e in _items(cloud.call("list_environments", {"repositoryUrl": repo_url}))}
            p = {**p, "environmentConfigId": env_ids[p["environmentConfigId"]]}
        p = {k: v for k, v in (p or {}).items() if k != "spec"}
        tool_action = ("edit" if kind == "environment" else "update") if action == "update" else action
        id_key = "automationId" if kind == "automation" else "id"
        args = {**p, id_key: x["id"]} if action == "update" else p if p else {id_key: x["id"]}
        client.call(f"{tool_action}_{noun}", args)


def deploy(mode: str, allow_delete: bool, plan: dict, automations: Mcp, cloud: Mcp) -> int:
    repo_url = plan["environments"][0]["repository"] if plan["environments"] else ""
    changes = diff(plan, *read_state(automations, cloud, plan["prefix"], repo_url))
    for action, kind, name, _, _, changed in changes:
        print(f"{action} {kind} {name}" + (f" ({', '.join(changed)})" if changed else ""))
    if not changes:
        print("unchanged")
    if mode == "--dry-run" or not changes:
        return 0
    if any(c[0] == "delete" for c in changes) and not allow_delete:
        print("refused: the diff deletes items; rerun with --allow-delete", file=sys.stderr)
        return 1
    apply_diff(changes, automations, cloud, repo_url)
    left = diff(plan, *read_state(automations, cloud, plan["prefix"], repo_url))
    for action, kind, name, *_ in left:
        print(f"verify failed: {action} {kind} {name} still differs", file=sys.stderr)
    return 1 if left else 0


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
            return deploy(argv[0], "--allow-delete" in argv, plan, *servers)
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

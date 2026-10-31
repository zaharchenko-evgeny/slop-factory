#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Render the nodes of workflows/nodes.yaml as deployable specs (ADR-0014). Output is JSON.

Usage: deploy.py plan --target air|local
air:   {"automations": [...], "environments": [...]}: one automation per enabled node (per tier or focus
       where the node has them) and one environment per environment in use, for the factory-deploy skill.
local: the launchd job of the local loop (factory/schedule_local.py).
"""
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nodes as nodes_mod
import schedule_local

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "[factory:"
PROMPT = (
    "Save the trigger payload (the <trigger_event> JSON) to a file. "
    "Run `uv run factory/gate.py check --node {node} --event <file>{sel}`; on exit 10 stop and report why. "
    "Otherwise read `role_file` and every skill it lists, and do the role's work on the `branch` of the output "
    "(create it from the default branch when it does not exist; the platform pushes). "
    "Write the role's handoff to `handoff_path`, then run "
    "`uv run factory/gate.py apply --node {node}{sel} --event <file> --handoff <handoff_path>`."
)
BOT_FILTERED = ("review-", "join")  # start only for the bot's own PRs until `adopt` (#160) removes the filter


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
    """The `repository` input of create_automation from a factory.yaml repository."""
    url = r["url"].rstrip("/")
    return {"fullName": "/".join(url.split("/")[-2:]), "cloneUrl": url + ".git", "browseUrl": url,
            "provider": "github" if "github.com" in url else "other"}


# A script node runs no agent work: the gate's sweep decides every scripted step (join, waiting reviewers,
# merged to done) from facts, so the automation only runs it.
SCRIPT_PROMPT = ("Run `uv run factory/gate.py sweep` once and report its JSON output. Do nothing else: "
                 "change no file, label, comment or review yourself.")


def variants(name: str, n: dict, roles: dict) -> list[tuple[str, str | None, str, str | None]]:
    """(automation suffix, tier of the node, model tier, focus) per tier or focus of the node."""
    role = roles.get(n["role"], {})
    if n.get("tiers"):
        return [(f"{name}-{t}", t, t, None) for t in n["tiers"]]
    if n.get("focuses"):
        return [(f"{name}-{f}", None, role["modes"][f]["tier"], f) for f in n["focuses"]]
    return [(name, None, n.get("tier") or role.get("tier") or "cheap", None)]


def plan_air(nodes: dict, config: dict) -> dict:
    air, roles = config["targets"]["air"]["models"], config["roles"]
    repos = {r["name"]: r for r in config["repositories"]}
    primary = next(r for r in config["repositories"] if r.get("role") == "primary")
    envs_def = nodes.get("environments") or {}
    scheds = nodes.get("schedules") or {}
    bot = config["targets"]["air"]["bot_login"]
    automations, used = [], []
    for name, n in nodes["nodes"].items():
        if n.get("enabled") is False or n["role"] == "human":
            continue
        env = n.get("environment", "default")
        if env not in used:
            used.append(env)
        for aname, tier_arg, tier, focus in variants(name, n, roles):
            agent, _, model = air[tier].partition("/")
            trig = []
            for t in n["triggers"]:
                if str(t).startswith("schedule "):
                    trig.append({"triggerType": "SCHEDULED", "cronExpression": scheds[str(t)[9:]]["cron"],
                                 "timezone": "UTC"})
                elif str(t) != "manual":
                    trig.append(trigger(t, tier_arg, bot if aname.startswith(BOT_FILTERED) else None))
            automations.append({
                "name": f"{PREFIX}{aname}]",
                "triggers": trig,
                "triggerVcs": [{"outputBehaviour": n.get("output", "read-only")}],
                "agentAndModel": {"id": model, "name": model, "isDefault": False, "agentId": agent},
                "repository": repository(repos.get(envs_def[env]["repository"]) or primary),
                "environmentConfigId": f"{PREFIX}{env}] env",
                "prompt": SCRIPT_PROMPT if n["role"] == "script" else PROMPT.format(
                    node=name, sel=(f" --focus {focus}" if focus else "") + (f" --tier {tier_arg or tier}" if tier_arg or focus else "")),
            })
    envs = []
    for env in used:
        d = envs_def[env]
        repo = repos.get(d["repository"]) or primary
        envs.append({
            "name": f"{PREFIX}{env}] env",
            "repository": repo["url"],
            "startup": d["startup"],
            "variables": {"FACTORY_ENV": env},
            "secrets": d.get("secrets", []),
            "domains": d.get("domains", []),
            "vm": d.get("vm", "default"),
        })
    return {"automations": automations, "environments": envs}


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] != "plan" or argv[1] != "--target" or argv[2] not in ("air", "local"):
        print(__doc__)
        return 2
    config = yaml.safe_load((ROOT / "factory.yaml").read_text())
    if argv[2] == "local":
        out = schedule_local.render(config)
    else:
        out = plan_air(nodes_mod.load(), config)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

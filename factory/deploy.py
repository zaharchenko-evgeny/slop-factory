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
    "Run `uv run factory/gate.py check --node {node} --event <file>`; on exit 10 stop and report why. "
    "Otherwise read `role_file` and every skill it lists, and do the role's work. "
    "Then run `uv run factory/gate.py apply --node {node} --event <file> --outcome <outcome> --payload <file>` "
    "with the outcome name of the node. The payload file is a JSON object with `ticket` (from the event) "
    "plus every field the outcome's effects name (for example `tier`, `reason`)."
)


def trigger(text: str, tier: str | None) -> dict:
    """`issues.labeled factory:ready` → an EVENT trigger of create_automation (air-automations)."""
    event, _, arg = str(text).partition(" ")
    arg = arg.replace("<tier>", tier or "")
    labeled = event.startswith("issues.") or event.startswith("pull_request.labeled")
    return {"triggerType": "EVENT", "source": "github", "eventType": event,
            "filters": {"label": arg} if labeled and arg else {}}


def repository(r: dict) -> dict:
    """The `repository` input of create_automation from a factory.yaml repository."""
    url = r["url"].rstrip("/")
    return {"fullName": "/".join(url.split("/")[-2:]), "cloneUrl": url + ".git", "browseUrl": url,
            "provider": "github" if "github.com" in url else "other"}


def variants(name: str, n: dict, roles: dict) -> list[tuple[str, str | None, str]]:
    """(automation suffix, tier name for triggers, model tier) per tier or focus of the node."""
    role = roles.get(n["role"], {})
    if n.get("tiers"):
        return [(f"{name}-{t}", t, t) for t in n["tiers"]]
    if n.get("focuses"):
        return [(f"{name}-{f}", None, role["modes"][f]["tier"]) for f in n["focuses"]]
    return [(name, None, n.get("tier") or role.get("tier") or "cheap")]


def plan_air(nodes: dict, config: dict) -> dict:
    air, roles = config["targets"]["air"]["models"], config["roles"]
    repos = {r["name"]: r for r in config["repositories"]}
    primary = next(r for r in config["repositories"] if r.get("role") == "primary")
    envs_def = nodes.get("environments") or {}
    scheds = nodes.get("schedules") or {}
    automations, used = [], []
    for name, n in nodes["nodes"].items():
        if n.get("enabled") is False or n["role"] == "human":
            continue
        env = n.get("environment", "default")
        if env not in used:
            used.append(env)
        for aname, tier_arg, tier in variants(name, n, roles):
            agent, _, model = air[tier].partition("/")
            trig = []
            for t in n["triggers"]:
                if str(t).startswith("schedule "):
                    trig.append({"triggerType": "SCHEDULED", "cronExpression": scheds[str(t)[9:]]["cron"],
                                 "timezone": "UTC"})
                elif str(t) != "manual":
                    trig.append(trigger(t, tier_arg))
            automations.append({
                "name": f"{PREFIX}{aname}]",
                "triggers": trig,
                "triggerVcs": [{"outputBehaviour": n.get("output", "read-only")}],
                "agentAndModel": {"id": model, "name": model, "isDefault": False, "agentId": agent},
                "repository": repository(repos.get(envs_def[env]["repository"]) or primary),
                "environmentConfigId": f"{PREFIX}{env}] env",
                "prompt": PROMPT.format(node=name),
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

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Render the nodes of workflows/nodes.yaml as deployable specs (ADR-0014). Output is JSON.

Usage: deploy.py plan --target air
air: {"automations": [...], "environments": [...]}: one automation per enabled node (per tier or focus
     where the node has them) and one environment per environment in use, for the factory-deploy skill.
"""
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod
import nodes as nodes_mod

ROOT = Path(__file__).resolve().parents[1]
PROMPT = (
    "You are the {role} of {project}.\n\n{role_intro}\n\n"
    "Your job in this flow: triggered by {triggers}; outcomes: {outcomes}.\n\n"
    "Save the trigger payload (the <trigger_event> JSON) to a file. "
    "{gate_setup}"
    "Run `uv run {gate_root}factory/gate.py{team} check --node {node} --event <file>{sel}`; on exit 10 stop and report why. "
    "Otherwise {role_read}, and do the role's work on the `branch` of the output "
    "(create it from the default branch when it does not exist; the platform pushes). "
    "Write the role's handoff to `handoff_path`, then run "
    "`uv run {gate_root}factory/gate.py{team} apply --node {node}{sel} --event <file> --handoff <handoff_path>`."
)
PR_GATE_SETUP = (
    "For this pull-request-triggered node, first run `git fetch origin main` and "
    "`git worktree add --detach <tmp>/factory-main origin/main`. Use that worktree for the gate, role file, "
    "and listed skills; use this checkout only as the code under review or fix. "
    "Always pass `--team <tmp>/factory-main` to the gate, so the descriptor, graph and role overrides come from "
    "main, never from the pull request. "
)
PR_TRIGGERED_NODES = {"review", "join", "fix", "finish"}
BOT_FILTERED = ("review-", "join")  # start only for the bot's own PRs until `adopt` (#160) removes the filter


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


def role_intro(role: str) -> str:
    """The role's opening section, which states its responsibility and limits."""
    path = config_mod.resolve(f"roles/{role}/role.md")
    lines = path.read_text().splitlines()
    section = []
    for line in lines[1:]:
        if line.startswith("## "):
            break
        section.append(line)
    return "\n".join(section).strip()


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


def flow_outcomes(node: dict) -> str:
    return "; ".join(f"{name}: {OUTCOME_WORDS.get(name, name.replace('-', ' '))}"
                      for name in node.get("outcomes", {}))


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


# A script node runs no agent work: the gate's sweep decides every scripted step (join, waiting reviewers,
# merged to done) from facts, so the automation only runs it.
SCRIPT_PROMPT = ("Run `uv run factory/gate.py sweep` once and report its JSON output. Do nothing else: "
                 "change no file, label, comment or review yourself.")
STALE_PROMPT = SCRIPT_PROMPT.replace("gate.py sweep`", "gate.py sweep --stale`")
FINISH_SCRIPT_PROMPT = (
    f"{PR_GATE_SETUP}Read the issue number from the trigger payload. "
    "Run `uv run <tmp>/factory-main/factory/gate.py --team <tmp>/factory-main sweep --issue <issue number>` once and report its JSON output. "
    "Do nothing else: change no file, label, comment or review yourself."
)


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
    prefix = f"[{project_name(config)}] "
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
                "name": f"{prefix}{aname}",
                "triggers": trig,
                "triggerVcs": [{"outputBehaviour": n.get("output", "read-only")}],
                "agentAndModel": {"id": model, "name": model, "isDefault": False, "agentId": agent},
                "repository": repository(repos.get(envs_def[env]["repository"]) or primary),
                "environmentConfigId": f"{prefix}{env}",
                "prompt": (FINISH_SCRIPT_PROMPT if name == "finish" else
                           STALE_PROMPT if "--stale" in n.get("run", "") else SCRIPT_PROMPT)
                if n["role"] == "script" else PROMPT.format(
                    role=n["role"],
                    project=project_name(config),
                    role_intro=role_intro(n["role"]),
                    triggers=flow_triggers(n),
                    outcomes=flow_outcomes(n),
                    node=name,
                    sel=(f" --focus {focus}" if focus else "") + (f" --tier {tier_arg or tier}" if tier_arg or focus else ""),
                    gate_root="<tmp>/factory-main/" if name in PR_TRIGGERED_NODES else "",
                    team=" --team <tmp>/factory-main" if name in PR_TRIGGERED_NODES else "",
                    gate_setup=PR_GATE_SETUP if name in PR_TRIGGERED_NODES else "",
                    role_read="read `role_file` and every skill it lists from <tmp>/factory-main"
                    if name in PR_TRIGGERED_NODES else "read `role_file` and every skill it lists"),
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


def main(argv: list[str]) -> int:
    argv = config_mod.pop_team(argv)
    if len(argv) != 3 or argv[0] != "plan" or argv[1] != "--target" or argv[2] != "air":
        print(__doc__)
        return 2
    config = config_mod.load()
    out = plan_air(nodes_mod.load(), config)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

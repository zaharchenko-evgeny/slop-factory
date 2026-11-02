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
PR_TRIGGERED_NODES = {"review", "join", "fix", "update", "finish"}
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
    automations, used = [], []
    for name, n in nodes["nodes"].items():
        if n.get("enabled") is False or n["role"] == "human":
            continue
        env = n["env"]
        if env not in used:
            used.append(env)
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

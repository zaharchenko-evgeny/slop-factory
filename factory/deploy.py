#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
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
import fnmatch
import json
import time
import os
import re
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod
import definition
import github_tracker
import nodes as nodes_mod
import project as project_mod
import vocab
import youtrack_tracker

ROOT = Path(__file__).resolve().parents[1]
SLOP_FIXTURE = ROOT / "factory/tests/fixtures/slop"
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
        if (nodes["nodes"].get("fix") or {}).get("tiers"):  # old layout: the fix tier a red CI hands the ticket to
            spec["else_tier"] = nodes["nodes"]["fix"]["tiers"][0]
    if top and n["role"] == "script":  # a sweep applies the other script nodes (join, finish) too
        spec["runs"] = {k: node_spec(k, v, config, None, None, nodes, False) for k, v in nodes["nodes"].items()
                        if v["role"] == "script" and k != name and v.get("enabled") is not False and not v.get("routine")}
    if config.get("agents") and n["role"] not in project_mod.NO_AGENT_ROLES:  # without `agents:` the plan is unchanged
        agents = agents_of(nodes, config)
        persona = agents[name]["persona"]
        allowed = mention_allowlist(nodes, agents)[persona]
        spec.update(agent=agents[name]["id"], persona=persona, mentions=sorted(allowed), mentions_on=allowed)
    return spec


def agents_of(nodes: dict, config: dict) -> dict:
    """{node: agent} of the graph's agent nodes (ADR-0020); a bad `agents:` entry or `agent:` key is rejected."""
    agents, errors = project_mod.node_agents(config, nodes["nodes"])
    if errors:
        raise ValueError("; ".join(errors))
    return agents


def subject(name: str) -> str:
    """What a node acts on: `pr` for a PR-triggered node, else `ticket`."""
    return "pr" if name in PR_TRIGGERED_NODES else "ticket"


def wakes(a: dict, b: dict) -> bool:
    """True when an outcome effect of node `a` sets a stage in `b`'s `when.stage` or applies a label `b` triggers on."""
    def glob(label):
        return re.sub(r"<[^>]*>", "*", str(label))
    stages = (b.get("when") or {}).get("stage")
    stages = set(stages if isinstance(stages, list) else [stages]) - {None}
    labels = [glob(t.partition(" ")[2]) for t in map(str, b.get("triggers") or [])
              if t.partition(" ")[0] in ("issues.labeled", "pull_request.labeled", "youtrack.tagAdded")]
    for outcome in a["outcomes"].values():
        for e in (outcome or {}).get("effects") or []:
            if e.get("stage") in stages:
                return True
            if "label" in e and any(fnmatch.fnmatchcase(glob(e["label"]), t) or fnmatch.fnmatchcase(t, glob(e["label"]))
                                    for t in labels):
                return True
    return False


def mention_allowlist(nodes: dict, agents: dict) -> dict:
    """{persona: {persona: [subject]}} (ADR-0020): agent X may mention agent Y on a subject when a node of X and a node
    of Y both act on it and one wakes the other. Routines and `mention: false` nodes are outside the graph."""
    graph = {k: n for k, n in nodes["nodes"].items() if k in agents and not n.get("routine") and n.get("mention") is not False}
    allowed = {a["persona"]: {} for a in agents.values()}
    for x, nx in graph.items():
        for y, ny in graph.items():
            px, py = agents[x]["persona"], agents[y]["persona"]
            if px != py and subject(x) == subject(y) and (wakes(nx, ny) or wakes(ny, nx)):
                allowed[px].setdefault(py, set()).add(subject(x))
    return {p: {q: sorted(on) for q, on in sorted(m.items())} for p, m in allowed.items()}


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


def trigger(text: str, tier: str | None, pr_author: str | None = None, focus: str | None = None,
            repo: str | None = None) -> dict:
    """`issues.labeled factory:ready` → an EVENT trigger of create_automation (air-automations)."""
    event, _, arg = str(text).partition(" ")
    arg = arg.replace("<tier>", tier or "")
    arg = arg.replace("<focus>", focus or "")
    if event.startswith("youtrack."):  # the filter value is case-sensitive: pass it through unchanged
        name, _, value = arg.partition("=")
        filters = {"tagAdded": arg} if event == "youtrack.tagAdded" else {name: value}
        return {"triggerType": "EVENT", "source": "youtrack", "eventType": "issueUpdated", "filters": filters}
    if event == "workflow_run":  # ADR-0021 `ci: failed`: Air fires only on a failed run (AIRA-725)
        return {"triggerType": "EVENT", "source": "github", "eventType": "workflow_run.failed",
                "filters": {"workflow_name": arg, **({"repo_label": f"repo:{repo}"} if repo else {})}}
    labeled = event.startswith("issues.") or event.startswith("pull_request.labeled")
    filters = {"label": arg} if labeled and arg else {}
    if pr_author and event.startswith("pull_request"):
        filters["pr_author"] = pr_author
    if repo:  # a per-repository automation answers only tickets and PRs tagged `repo:<name>`
        filters["repo_label"] = f"repo:{repo}"
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
INSTALLED_FACTORY = (" Run the installed `factory` command (`command -v factory`); never `uv run`, `python` or a path "
                     "into the engine checkout.")
PR_NUMBER = " --main --issue <the number after the leading # of the PR title, or else the body's Closes #<n>>"
SCRIPT_ARGS = {"join": PR_NUMBER, "start": " --main --issue <the issue number>", "finish": PR_NUMBER,
               "reopen": PR_NUMBER, "merge": PR_NUMBER,
               "doctor": " doctor", "doctor-write": " doctor", "doctor-ops": " doctor"}


def team_is_project(team: dict, config: dict) -> bool:
    """True when the team repository is also a descriptor project repository (ADR-0019)."""
    return bool(config.get("team_repository")) and any(
        repository(r)["fullName"] == repository(team)["fullName"] for r in config["repositories"])


def workflow_drift(fixture: Path, team: Path) -> list[str]:
    """Describe differences between the migrated Slop fixture and the live team files."""
    project_file, workflow_file = "factory.project.yaml", "workflows/delivery.yaml"
    fixture_project = yaml.safe_load((fixture / project_file).read_text())
    team_project = yaml.safe_load((team / project_file).read_text())
    drift = [project_file] if fixture_project != team_project else []
    fixture_workflow = yaml.safe_load((fixture / workflow_file).read_text())
    team_workflow = yaml.safe_load((team / workflow_file).read_text())
    if fixture_workflow != team_workflow:
        fixture_nodes, team_nodes = fixture_workflow.get("nodes", {}), team_workflow.get("nodes", {})
        outcomes = [f"{name}.{outcome}" for name in sorted(fixture_nodes.keys() | team_nodes.keys())
                    for outcome in sorted((fixture_nodes.get(name) or {}).get("outcomes", {}).keys() |
                                          (team_nodes.get(name) or {}).get("outcomes", {}).keys())
                    if (fixture_nodes.get(name) or {}).get("outcomes", {}).get(outcome) !=
                       (team_nodes.get(name) or {}).get("outcomes", {}).get(outcome)]
        drift.extend(f"{workflow_file}: {item}" for item in outcomes)
        if not outcomes:
            drift.append(workflow_file)
    return drift


def check_fixture_drift(config: dict, team: Path | None = None) -> int:
    """Fail before contacting Air when this engine's Slop fixture differs from its deployed team."""
    if not SLOP_FIXTURE.is_dir():
        return 0
    if (SLOP_FIXTURE / "factory.yaml").is_file():  # ADR-0021: the fixture is a copy of the team's files
        team = Path(team or config_mod.team_root())
        fixture = yaml.safe_load((SLOP_FIXTURE / "factory.yaml").read_text()) or {}
        if not (team / "factory.yaml").is_file() or \
                (yaml.safe_load((team / "factory.yaml").read_text()) or {}).get("name") != fixture.get("name"):
            return 0
        drift = [p.relative_to(SLOP_FIXTURE).as_posix() for p in sorted(SLOP_FIXTURE.rglob("*"))
                 if p.is_file() and p.name != "SOURCE" and
                 (not (team / p.relative_to(SLOP_FIXTURE)).is_file() or
                  (team / p.relative_to(SLOP_FIXTURE)).read_bytes() != p.read_bytes())]
        for item in drift:
            print(f"fixture drift: {item}", file=sys.stderr)
        return 1 if drift else 0
    fixture_project = yaml.safe_load((SLOP_FIXTURE / "factory.project.yaml").read_text())
    if config.get("team_repository", "").rstrip("/") != fixture_project.get("team_repository", "").rstrip("/"):
        return 0
    drift = workflow_drift(SLOP_FIXTURE, team or config_mod.team_root())
    for item in drift:
        print(f"fixture drift: {item}", file=sys.stderr)
    return 1 if drift else 0


def other_projects(main_repository: dict, team: dict, config: dict) -> list[dict]:
    """The descriptor's repositories other than the automation's main one, in order.

    The team repository is left out, unless it is a project repository (ADR-0019): then it is one project checkout."""
    skip = {main_repository["fullName"]} | (set() if team_is_project(team, config) else {repository(team)["fullName"]})
    return [r for r in config["repositories"] if repository(r)["fullName"] not in skip]


def additional_repositories(main_repository: dict, team: dict, config: dict) -> list[dict]:
    """Air's extra checkouts (ADR-0017): the other project repositories, then the read-only team repository
    unless it is itself a project repository (ADR-0019)."""
    projects = [{"repository": repository(r), "branch": r["default_branch"]}
                for r in other_projects(main_repository, team, config)]
    return projects if team_is_project(team, config) else projects + [{"repository": repository(team), "branch": "main"}]


def checkout_instructions(main_repository: dict | None, config: dict, push_other_projects: bool = False) -> str:
    primary = next(r for r in config["repositories"] if r.get("role") == "primary")
    team = {"url": config.get("team_repository") or primary["url"]}
    if not main_repository:
        return ""
    push = ""
    if push_other_projects and config.get("write_mode") == "token" and other_projects(main_repository, team, config):
        push = ("For each other changed project repository, push this ticket branch and open or update its PR. "
                "Use `FACTORY_WRITE_TOKEN` without printing it. Push with `git -c credential.helper= push "
                "https://x-access-token:$FACTORY_WRITE_TOKEN@github.com/<owner>/<repo> HEAD:<branch>`, "
                "then use `gh pr create` or `gh pr edit` with a `[factory]` body that includes `Refs #<n>`. "
                "Use the same `ez/<n>-<slug>` branch in each repository.\n\n")
    if main_repository["fullName"] == repository(team)["fullName"]:
        # Air's gh picks its token from the working directory's origin: the tracker's checkout reaches the tracker.
        tracker = (config.get("tracker") or {}).get("repository") or repository(primary)["fullName"]
        if tracker == main_repository["fullName"]:
            return push
        return (f"## Working directory\n\nRun every command from `/workspaces/{tracker.rsplit('/', 1)[-1]}`: "
                "GitHub access there reaches the tracker. This automation's own checkout is the team repository.\n\n" + push)
    project = main_repository["fullName"].rsplit("/", 1)[-1]
    others = ", ".join(f"`/workspaces/{repository(r)['fullName'].rsplit('/', 1)[-1]}`"
                       for r in other_projects(main_repository, team, config))
    if team_is_project(team, config):
        name = repository(team)["fullName"].rsplit("/", 1)[-1]
        tail = (f"`/workspaces/{name}` is the team repository and a project repository: change it like any other. "
                "The factory reads its roles and skills from `$HOME/factory-team`, never from that checkout.\n\n")
    else:
        tail = "The factory repository is checked out beside it and is read-only.\n\n"
    return (f"## Project checkout\n\nBefore running project commands, `cd /workspaces/{project}`. "
            + (f"The other project repositories are checked out beside it: {others}. " if others else "")
            + tail + push)


def script_prompt(name: str, main_repository: dict | None = None, config: dict | None = None,
                  webhook: bool = False) -> str:
    checkout = checkout_instructions(main_repository, config) if config else ""
    if name.startswith("doctor"):
        engine = name == "doctor-ops"
        command = "`factory doctor --engine`" if engine else "`factory doctor`"
        prompt = (f"Run the installed {command} command exactly once in the foreground with "
                  + ("only the `--engine` argument. " if engine else "no arguments. ")
                  + f"Do not use `uv run`, `--help` or add {'other ' if engine else ''}arguments. Report every JSON probe line and the command's exit code. "
                  "It creates a temporary probe issue, a child issue and a PR, then deletes them when possible. "
                  "A failed probe creates or updates a `Doctor: <probe> fails` ticket; denied ticket writes appear as a "
                  "`report` probe line. You must not create, edit or close tickets yourself.")
    elif name == "reconcile":
        prompt = ("This step runs one command and judges nothing: it changes no file, label, comment or review itself. "
                  "Run `factory reconcile` in the foreground with no time limit; wait until it exits, and report its JSON output.")
    else:
        args = SCRIPT_ARGS.get(name, "")
        prompt = SCRIPT_PROMPT.format(args=args)
        if "--issue" in args:
            prompt += " Copy only that one number; never write the payload or any JSON."
        if webhook:
            prompt = prompt.replace("<the issue number>", "<the `issue` field of the trigger payload>")
    return checkout + prompt + INSTALLED_FACTORY


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
    if any(str(t).startswith("webhook ") for t in n["triggers"]):
        return "--issue <the `issue` field of the trigger payload>"
    if node in PR_TRIGGERED_NODES:
        return "--pr <the pull request number>"
    events = [str(t) for t in n["triggers"] if str(t) != "manual"]
    on_pr = [t.startswith(("pull_request", "issue_comment", "workflow_run")) for t in events]
    if events and all(on_pr):  # ADR-0021: what wakes the node says what it begins with, not its name
        return "--pr <the pull request number>"
    if any(on_pr):
        return ("--pr <the pull request number> when a pull request woke this run, else "
                "--issue <the issue number>")
    return "--issue <the issue number>"


def mcp_connectors(n: dict, config: dict, focus: str | None) -> list[str]:
    """The Air MCP connectors of an automation: its node's list, else its mode's, else its role's, else none."""
    role = (config["roles"].get(n["role"]) or {}) if isinstance(config["roles"], dict) else {}
    mode = ((role.get("modes") or {}).get(focus) or {}) if focus else {}
    for source in (n, mode, role):
        if source.get("mcp") is not None:
            return list(source["mcp"])
    return []


def agent_prompt(name: str, n: dict, config: dict, aname: str, focus: str | None,
                 main_repository: dict | None = None, tier: str | None = None) -> str:
    role = n["role"]
    servers = mcp_connectors(n, config, focus)
    who = f"You are the {role} of {project_name(config)}" + (f" ({focus} focus)" if focus else "") + "."
    checkout = checkout_instructions(main_repository, config, name in {"implement", "fix", "update"})
    import definition
    if definition.present(config_mod.team_root()):  # ADR-0021: the node's role body, rendered with its settings
        defn = definition.load(config_mod.team_root())
        agent = (defn["factory"].get("agents") or {}).get(((defn["workflow"].get("nodes") or {}).get(name) or {}).get("agent")) or {}
        if agent.get("run"):  # ADR-0021: a step with no judgement: the agent only runs the engine command
            return (f"{who}\n\n{checkout}Run `factory {agent['run']} --node {name} {begin_arg(name, n)}` once, in the "
                    "foreground, and wait until it exits. Copy only that one number. It applies "
                    "the outcome itself: do not run `factory begin` or `factory end`, and change nothing yourself. "
                    "Report its JSON output." + INSTALLED_FACTORY)
        text = definition.render(defn, name).strip()
        skills = definition.skill_lines(defn, role)
        servers = [f"{k} (Air connector {e['connector']})" if "connector" in e else f"{k} (custom, from .mcp.json)"
                   for k, e in definition.mcp(defn, name).items()]
    else:
        text = "\n\n".join(config_mod.resolve(f).read_text().strip() for f in (f"roles/{role}/role.md", "roles/_common.md"))
        skills = skill_lines(role)
    checkout = checkout_instructions(main_repository, config, name in {"implement", "fix", "update"})
    mcp = ("## Your MCP servers\n\n" + "\n".join(f"- {c}" for c in servers) + "\n\n") if servers else ""
    arg = begin_arg(name, n)
    arg = f" {arg}" if arg else ""
    return (f"{who}\n\n{checkout}{text}\n\n## Your skills\n\n{skills}\n\n{mcp}## This step\n\n{this_step(n, focus)}\n\n"
            "## How you start and finish\n\n"
            f"1. Run `factory begin {aname}{arg}`.{' Copy only that one number; never write the payload or any JSON.' if arg else ''} "
            "It prints the ticket, the facts and the branch or PR head. "
            "If it exits 10 the node must not act: report the reason and stop.\n"
            "2. Do the role's work. As the last thing you do, report one outcome above with `factory end <outcome>` "
            "and the data it needs, as the `factory-step` skill describes.")


def variants(name: str, n: dict, roles: dict, agent: dict | None = None) -> list[tuple[str, str | None, str, str | None]]:
    """(automation suffix, tier of the node, model tier, focus) per tier or focus of the node; the node's agent, when
    given, replaces its role's tiers."""
    role = agent or roles.get(n["role"], {})
    if n.get("tiers"):
        return [(f"{name}-{t}", t, t, None) for t in n["tiers"]]
    if n.get("focuses"):
        return [(f"{name}-{f}", None, role["modes"][f]["tier"], f) for f in n["focuses"]]
    return [(name, None, n.get("tier") or role.get("tier") or "cheap", None)]


def compile_automation(automation: str, nodes: dict, config: dict) -> dict:
    """The node, gate options and spec of one automation (`review-behaviour`, `fix-mid`) from the node graph."""
    roles, agents = config["roles"], agents_of(nodes, config)
    for name, n in nodes["nodes"].items():
        if n.get("enabled") is False or n["role"] in ("human", "script"):
            continue
        for aname, tier_arg, tier, focus in variants(name, n, roles, agents.get(name)):
            if aname == automation:
                return {"node": name, "focus": focus, "tier": tier_arg or (tier if focus else None),
                        "spec": node_spec(name, n, config, tier_arg or tier, focus, nodes)}
    raise KeyError(f"unknown automation: {automation}")


# Nodes planned once per project repository when `write_mode` is `per-repo` (default) and the project has several
# repositories: Air pushes only to an automation's primary repository. Only triage sees every repository.
PER_REPO = definition.PER_REPO
WRITE_TOKEN_SECRET = github_tracker.WRITE_TOKEN  # `write_mode: token`: the project-repository write token, by name only


def plan_air(nodes: dict, config: dict) -> dict:
    air, roles, agents = config["targets"]["air"]["models"], config["roles"], agents_of(nodes, config)
    for aid in sorted({a["id"] for a in agents.values() if a.get("effort")}):
        print(f"deploy: agent {aid}: effort ignored, Air automations take no reasoning setting", file=sys.stderr)
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
    unscoped = set()  # environments some automation shares across the project
    scoped = []  # (env, repository name, node) of the per-repository or per-node automations
    defn = definition.load(config_mod.team_root()) if definition.present(config_mod.team_root()) else None
    # ADR-0021: a definition's environments list their repositories, so only a code repository that is `main` in an
    # environment gets per-repository automations, in that environment
    code_envs = definition.per_repo_envs(defn) if defn else {}
    code_scopes = [r for r in config["repositories"] if definition.full_name(r["url"]) in
                   {definition.full_name(c) for c in code_envs}] if defn else config["repositories"]
    per_repo = config.get("write_mode", "per-repo") == "per-repo" and len(code_scopes) > 1
    if per_repo:
        used += [e for e in code_envs.values() if e not in used]
    for name, n in nodes["nodes"].items():
        if n.get("enabled") is False or n["role"] == "human" or n.get("runner"):
            continue
        scopes = code_scopes if per_repo and definition.per_repo_node(name) else [None]
        for scope, (aname, tier_arg, tier, focus) in [(sc, v) for sc in scopes
                                                      for v in variants(name, n, roles, agents.get(name))]:
            env = next((e for r, e in code_envs.items() if scope and definition.full_name(r) == definition.full_name(scope["url"])),
                       n["env"])  # a definition: the environment where the scope repository is `main`
            agent, _, model = air[tier].partition("/")
            trig = []
            webhook = any(str(t).startswith("webhook ") for t in n["triggers"])
            spec = node_spec(name, n, config, tier_arg or tier, focus, nodes)
            for t in n["triggers"]:
                if str(t).startswith("schedule "):
                    trig.append({"triggerType": "SCHEDULED", "cronExpression": scheds[str(t)[9:]]["cron"],
                                 "timezone": "UTC"})
                elif str(t).startswith("webhook "):
                    trig.append({"triggerType": "WEBHOOK"})
                elif str(t) != "manual":
                    trig.append(trigger(t, tier_arg, bot if aname.startswith(BOT_FILTERED) else None, focus,
                                        scope["name"] if scope else None))
            automation_repository = repository(team if focus == "knowledge" else scope or repos.get(envs_def[env]["repository"]) or primary)
            if focus == "knowledge":  # Air runs an automation only in an environment of its own repository
                env = next((e for e in used if (repos.get(envs_def[e]["repository"]) or primary)["url"] == team["url"]), env)
            scope_name = scope["name"] if scope else None
            env_scope = None if focus == "knowledge" or defn else scope_name  # a knowledge review runs in the team's own environment
            # A node with custom MCP servers runs in an environment of its own: setup writes `.mcp.json` per environment
            env_node = name if defn and focus != "knowledge" and definition.mcp_custom(definition.mcp(defn, name)) else None
            if env_scope or env_node:
                if (env, env_scope, env_node) not in scoped:
                    scoped.append((env, env_scope, env_node))
            else:
                unscoped.add(env)
            automation = {
                "name": f"{prefix}{aname}" + (f" {scope_name}" if scope_name else ""),
                "triggers": trig,
                "triggerVcs": [{"outputBehaviour": n.get("output", "read-only")} for _ in trig],
                "spec": spec,
                "agentAndModel": {"id": model, "name": model, "isDefault": False, "agentId": agent},
                "repository": automation_repository,
                "environmentConfigId": f"{prefix}{env}" + (f" {env_scope}" if env_scope else "") + (f" {env_node}" if env_node else ""),
                "mcpConnectors": mcp_connectors(n, config, focus),
                "prompt": script_prompt(name, automation_repository, config, webhook) if n["role"] == "script" else agent_prompt(
                    name, n, config, aname, focus, automation_repository, tier_arg or tier),
            }
            if automation_repository["fullName"] != repository(team)["fullName"]:
                automation["additionalRepositories"] = additional_repositories(automation_repository, team, config)
            else:
                projects = other_projects(automation_repository, team, config)
                if projects:
                    automation["additionalRepositories"] = [
                        {"repository": repository(r), "branch": r["default_branch"]} for r in projects]
            automations.append(automation)
    envs = []
    token = config.get("write_mode") == "token" and len(config["repositories"]) > 1
    issues_token = config.get("issues_token_env")  # the gate's issue, label and comment writes to project repositories
    for env, scope_name, node_name in [(e, None, None) for e in used if e in unscoped] + scoped:
        d = envs_def[env]
        repo = repos[scope_name] if scope_name else repos.get(d["repository"]) or primary
        envs.append({
            "name": f"{prefix}{env}" + (f" {scope_name}" if scope_name else "") + (f" {node_name}" if node_name else ""),
            "repository": repo["url"],
            "startup": d["startup"],
            "variables": {**(d.get("variables") or {}), "FACTORY_ENV": env, **({"FACTORY_NODE": node_name} if node_name else {})},
            "secrets": list(dict.fromkeys([*d.get("secrets", []),
                                           *([WRITE_TOKEN_SECRET] if token and github_tracker.token_env(env, config) else []),
                                           *([issues_token] if issues_token and len(config["repositories"]) > 1 else [])])),
            "domains": d.get("domains", []),
            "vm": d.get("vm", "default"),
        })
    tags = youtrack_tags(nodes, config)
    label_repos = list(dict.fromkeys(x for x in ((config.get("tracker") or {}).get("repository"),
                                                 repository(primary)["fullName"]) if x))
    fields = youtrack_fields(config)
    tag_plan = {"tags": tags, "owned_tag_prefixes": sorted({t.partition(":")[0] + ":" for t in tags if ":" in t})} if tags else {}
    field_plan = {"fields": fields, "owned_field_prefixes": sorted(
        {v.partition(":")[0] + ":" for f in fields.values() for v in f if ":" in v})} if fields else {}
    if tags or fields:  # keeps a GitHub plan (and its fixture) unchanged
        tag_plan = {**tag_plan, **field_plan, "tag_tracker": config["tracker"]}
    return {"prefix": prefix, "automations": automations, "environments": envs, "labels": labels(nodes, config),
            "label_repositories": label_repos, "label_specs": label_specs(nodes, config),
            "owned_label_prefixes": owned_label_prefixes(config),
            **tag_plan, "files": actions_files(nodes, config)}


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


COMPLEXITY_DESCRIPTION = "Size grade set by intake; chooses the developer tier"


def owned_label_prefixes(config: dict) -> list[str]:
    """The label namespaces the deploy may remove from: the stage namespace (`factory:`) and the risk and complexity groups."""
    t = config.get("tracker") or {}
    ready = (t.get("stages") or {}).get("ready") or vocab.stage("ready")
    risks = (t.get("risk_labels") or {"low": "risk:low", "high": "risk:high"}).values()
    return sorted({ready.partition(":")[0] + ":", "complexity:", *(r.partition(":")[0] + ":" for r in risks)})


def label_specs(nodes: dict, config: dict) -> dict:
    """{label: {color[, description]}} the repositories should carry. A label's color comes from its role (stage name
    or stage kind, risk, CI fact, group) in `labels.colors` of the config, then `labels.overrides`; a label with no
    mapped color has no entry and gets a random one when created. Every label of `labels()` that has no other role is a
    command."""
    t, c = config.get("tracker") or {}, config.get("labels") or {}
    colors, kinds = c.get("colors") or {}, t.get("stage_kinds") or {}
    group, out = colors.get("groups") or {}, {}

    def put(label, color):
        if label and color:
            out.setdefault(label, {"color": color})
    for key, label in (t.get("stages") or {}).items():
        put(label, (colors.get("stages") or {}).get(key) or (colors.get("stage_kinds") or {}).get(kinds.get(key)))
    for key, label in (t.get("risk_labels") or {"low": "risk:low", "high": "risk:high"}).items():
        put(label, (colors.get("risk") or {}).get(key))
    for key, label in (t.get("pr_fact_labels") or {}).items():
        put(label, (colors.get("pr_facts") or {}).get(key))
    put(t.get("needs_human_label"), group.get("needs-human"))
    put(t.get("map_label"), group.get("map"))
    for size in ("small", "medium", "large"):
        out[f"complexity:{size}"] = {**({"color": group["complexity"]} if group.get("complexity") else {}),
                                     "description": COMPLEXITY_DESCRIPTION}
    for label in [*labels(nodes, config), t.get("review_trigger"), t.get("hold_label")]:
        put(label, group.get("command"))
    for label, color in (c.get("overrides") or {}).items():
        out[label] = {**out.get(label, {}), "color": str(color).lower()}
    return out


def label_tracker(repo: str, run=None):
    return github_tracker.GitHubTracker(repo, {}, run=run or github_tracker.gh)


def reconcile_labels(plan: dict, run=None) -> list[tuple]:
    """(action, repository, label, changed fields) per required label to create or update, and per factory-owned
    label no longer required to remove, in each repository of `plan["label_repositories"]` independently."""
    out, owned, random_colors = [], tuple(plan.get("owned_label_prefixes") or []), {}
    for repo in plan.get("label_repositories") or []:
        have = label_tracker(repo, run).list_labels()
        for name in plan["labels"]:
            spec = {"description": "", **(plan.get("label_specs") or {}).get(name, {})}
            if name not in have:
                want = {"color": spec.get("color") or random_colors.setdefault(name, github_tracker.random_color()),
                        "description": spec["description"]}
                out.append(("create", repo, name, want))
            elif changed := [k for k in ("color", "description") if k in spec and have[name][k] != spec[k]]:
                want = {"color": have[name]["color"], "description": have[name]["description"], **spec}
                out.append(("update", repo, name, want, changed))
        out += [("remove", repo, name, None) for name in sorted(have) if name.startswith(owned) and name not in plan["labels"]]
    return out


def youtrack_tags(nodes: dict, config: dict) -> list[str]:
    """The tags a YouTrack definition stores its stages and commands in, `<tier>` and `<focus>` expanded; none for other adapters."""
    if (config.get("tracker") or {}).get("adapter") != "youtrack":
        return []
    tiers = sorted({x for n in nodes["nodes"].values() for x in n.get("tiers") or []}) or ["cheap", "mid", "strong"]
    focuses = sorted({x for n in nodes["nodes"].values() for x in n.get("focuses") or []})
    out = set()
    for x in [*(config.get("states") or {}).values(), *(config.get("commands") or {}).values()]:
        tag = x["store"].get("tag")
        if isinstance(tag, str):
            out |= {tag.replace("<tier>", t) for t in tiers} if "<tier>" in tag else \
                {tag.replace("<focus>", f) for f in focuses} if "<focus>" in tag else {tag}
    return sorted(out)


def youtrack_fields(config: dict) -> dict:
    """{field: {value: resolved}} the state and command stores of a YouTrack definition keep in custom fields;
    a `final` state is resolved. Empty for other adapters."""
    if (config.get("tracker") or {}).get("adapter") != "youtrack":
        return {}
    out = {}
    for x in [*(config.get("states") or {}).values(), *(config.get("commands") or {}).values()]:
        f = x["store"].get("field")
        if isinstance(f, dict):
            out.setdefault(f["name"], {})[f["value"]] = x.get("kind") == "final"
    return out


def reconcile_fields(plan: dict, run=None) -> list[tuple]:
    """(action, field, value, resolved) per required field value to create, per one whose resolved flag differs
    (`update`), and per factory-owned value no longer required (`remove`). Values outside the factory's namespaces are
    never listed for removal. A missing field raises `youtrack_tracker.MissingField`."""
    out, owned = [], tuple(plan.get("owned_field_prefixes") or [])
    for name, want in (plan.get("fields") or {}).items():
        have = tag_store(plan, run).list_field_values(name)
        for value, resolved in want.items():
            if value not in have:
                out.append(("create", name, value, resolved))
            elif have[value]["resolved"] is not None and have[value]["resolved"] != resolved:
                out.append(("update", name, value, resolved))
        out += [("remove", name, v, None) for v in sorted(have) if owned and v.startswith(owned) and v not in want]
    return out


def tag_store(plan: dict, run=None):
    return youtrack_tracker.YouTrackTicketStore.from_config(plan["tag_tracker"], run)


def reconcile_tags(plan: dict, run=None) -> list[tuple]:
    """(action, tag) per required YouTrack tag to create and per factory-owned tag no longer required to remove;
    tags outside the factory's namespaces, or still used by another project's issues, are never listed for removal."""
    if not plan.get("tags"):
        return []
    store = tag_store(plan, run)
    have = store.list_tags()
    owned = tuple(plan.get("owned_tag_prefixes") or [])
    return [("create", t) for t in plan["tags"] if t not in have] + \
        [("remove", t) for t in sorted(have) if owned and t.startswith(owned) and t not in plan["tags"]
         and not store.used_outside_project(t)]


def labels(nodes: dict, config: dict) -> list[str]:
    """Every label the gate may write or a trigger filters on: stage labels, risk and complexity labels, and
    the command labels of the nodes with `<tier>` expanded over each node's tiers (a missing label makes
    `gh issue edit --add-label` fail, so the deploy creates them first)."""
    t = config.get("tracker") or {}
    out = set((t.get("stages") or {}).values()) | set((t.get("risk_labels") or {"low": "risk:low", "high": "risk:high"}).values())
    out.add(vocab.needs_human())
    out.add(vocab.lesson())  # ADR-0021: a ticket with uncollected lessons
    out.add(vocab.comment())  # ADR-0023: scripts/comment-wake-label.sh adds it; the comment node removes it
    out |= {vocab.ci("ci-green"), vocab.ci("ci-red")}  # CI writes them (scripts/ci-state-label.sh); reconcile reads
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
        if not text:
            return res.get("structuredContent")  # the text is the tool's JSON; structuredContent may wrap it
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}  # a tool that answers in prose (delete_automation)

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

    def set_additional(self, automation_id: str, repositories: list[dict], attempts: int = 12, pause: float = 10) -> None:
        for attempt in range(attempts):  # an automation created a moment ago answers 403 until Air settles
            try:
                current = self.get_automation(automation_id)
                break
            except urllib.error.HTTPError as e:
                if e.code not in (403, 404) or attempt == attempts - 1:
                    raise
                time.sleep(pause)
        body = {key: current[key] for key in self.SPEC_FIELDS if current.get(key) is not None}  # the API rejects nulls
        if "triggers" in body:
            body["triggers"] = [{key: value for key, value in trigger.items()
                                 if key not in ("triggerId", "automationSnapshotId")}
                                for trigger in body["triggers"]]
        body.update({"id": current.get("automationId", automation_id), "additionalRepositories": repositories})
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
    if key == "mcpConnectors":
        return sorted(have or []) == sorted(want or [])
    if key == "domains":
        return set(want or []) <= set(have or [])
    if key == "secrets":  # deploy cannot set or delete one: a missing one is `set by a person`, an extra one is theirs
        return True
    if key == "variables":  # Air cannot delete a variable: an extra one is a by-hand removal, not a difference
        return all(k in (have or {}) and have[k] == v for k, v in (want or {}).items())
    return have == want


def create_environment_args(p: dict, live: dict | None = None) -> dict:
    """Air `create_environment` arguments for a planned environment; `live` is the record it replaces, whose domains and
    network settings it keeps."""
    live = live or {}
    live_domains = live.get("domains") or []
    variables = p.get("variables") or {}
    args = {"name": p["name"], "repositoryUrl": p["repository"],
            "allowedDomains": live_domains + [d for d in p.get("domains") or [] if d not in live_domains],
            "variables": [{"key": k, "value": v} for k, v in variables.items()] if isinstance(variables, dict) else variables}
    args.update({k: live[k] for k in ("networkAccess", "predefinedDomainLists") if live.get(k)})
    return args


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
        x["variables"] = {v["key"]: v["value"] for v in x["variables"] if not v.get("isSecret")}
    if "personalSecrets" in x:
        x["secrets"] = [s.get("key") or s["name"] for s in x.pop("personalSecrets")]  # Air lists them by `key`
    return x


def diff(plan: dict, automations: list[dict], envs: list[dict]) -> list[tuple]:
    """(action, kind, name, planned item, existing item, changed fields) per create, update, delete; by name, prefix only.
    `plan["renames"]` ({old name: new name}) matches an existing item under its old name, so the update keeps its id."""
    new_to_old = {new: old for old, new in (plan.get("renames") or {}).items()}
    env_ids = {e["name"]: e.get("id") for e in envs}
    # Air cannot move an environment to another repository: it is replaced, and its automations follow the new one.
    replaced = {e["name"] for e in plan["environments"] for x in envs
                if x["name"] == e["name"] and "repository" in x and x["repository"] != e["repository"]}
    env_ids.update({name: f"(replaced) {name}" for name in replaced})
    out = []
    for kind, planned, have in (("environment", plan["environments"], envs), ("automation", plan["automations"], automations)):
        have = {x["name"]: x for x in have if x["name"].startswith(plan["prefix"])}
        for raw in planned:
            p = {k: v for k, v in raw.items() if k != "spec"}
            x = have.pop(p["name"], None) or have.pop(new_to_old.get(p["name"]), None)
            want = {**p, "environmentConfigId": env_ids.get(p.get("environmentConfigId"), p.get("environmentConfigId"))}
            changed = [k for k in p if x is not None and (kind != "environment" or k in x)
                       and not same(k, x.get(k), want[k])]
            if kind == "environment" and p["name"] in replaced:
                out.append(("replace", kind, p["name"], p, x, ["repository"]))
            elif x is None or changed:
                out.append(("create" if x is None else "update", kind, p["name"], p, x, changed))
        out += [("delete", kind, n, None, x, []) for n, x in have.items()]
    return out


def automation_detail(x: dict) -> dict:
    """A get_automation result as the plan's shape: `id` for `automationId`, no per-trigger server fields."""
    x = {**x, "id": x["automationId"]}
    raw = x.get("triggers") or []
    x["triggers"] = [{k: v for k, v in t.items() if k not in ("triggerId", "isEnabled", "automationSnapshotId")
                                          and not (t.get("triggerType") == "WEBHOOK" and k in ("url", "webhookUrl"))}
                     for t in x.get("triggers") or []]
    x["webhooks"] = [{"id": t.get("triggerId"), "url": t.get("url") or t.get("webhookUrl")}
                     for t in (y for y in raw if y.get("triggerType") == "WEBHOOK")]
    if isinstance(x.get("repository"), str):  # "owner/repo" stands for the planned repository object
        x["repository"] = {"fullName": x["repository"]}
    return x


def env_url(x: dict) -> str | None:
    if {"serviceHost", "organization", "repositoryName"} <= x.keys():
        return f"https://{x['serviceHost']}/{x['organization']}/{x['repositoryName']}"
    return x.get("repository") or x.get("repositoryUrl")


def list_envs(cloud: Mcp, repository_urls) -> list[dict]:
    """The environments of every repository in `repository_urls` (one URL or a list), each once."""
    seen, out = set(), []
    for url in [repository_urls] if isinstance(repository_urls, str) else repository_urls:
        for x in _items(cloud.call("list_environments", {"repositoryUrl": url})):
            if x["id"] not in seen:
                seen.add(x["id"])
                out.append(x)
    return out


def read_state(automations: Mcp, cloud: Mcp, prefix: str, repository_url, *, backend=None,
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
    env_items = list_envs(cloud, repository_url)
    env_items = [environment_detail(cloud.call("get_environment", {"id": x["id"]}))
                 for x in env_items if x["name"].startswith(prefix)]
    return automation_items, env_items


def apply_diff(changes: list[tuple], automations: Mcp, cloud: Mcp, repo_url="", *, backend=None,
               env_repositories=None) -> None:
    """Environments before automations on create, update and replace, after them on delete. A replaced environment
    is created under its new repository; the old one is renamed `retired <name>` first (Air names are unique) and left
    for a person to delete in the Air UI, since the Air MCP has no environment delete.
    `env_repositories` ({environment name: planned repository URL}) picks the right one of two same-named environments."""
    order = {"create": 0, "update": 0, "replace": 0, "delete": 1}
    env_repositories = env_repositories or {}
    env_ids, retired = None, []
    for action, kind, name, p, x, changed in sorted(changes, key=lambda c: (order[c[0]], (c[1] == "automation") == (c[0] != "delete"))):
        client, noun = (cloud, "environment") if kind == "environment" else (automations, "automation")
        additional = (p or {}).get("additionalRepositories") if kind == "automation" else None
        if action == "replace":
            client.call("edit_environment", {"id": x["id"], "name": f"retired {x['name']}"})
            client.call("create_environment", create_environment_args(p, x))
            retired.append(x["id"])
            print(f"retired environment {x['name']} ({x['id']}) is renamed 'retired {x['name']}': delete it in the Air UI",
                  file=sys.stderr)
            continue
        if p and kind == "automation":  # the environment id exists only after its create
            env_ids = env_ids or {e["name"]: e["id"] for e in list_envs(cloud, repo_url)
                                  if e["id"] not in retired and (env_url(e) is None or e["name"] not in env_repositories
                                                                 or env_url(e) == env_repositories[e["name"]])}
            p = {**p, "environmentConfigId": env_ids[p["environmentConfigId"]]}
        p = {k: v for k, v in (p or {}).items() if k not in ("spec", "additionalRepositories")}
        if action == "update":
            # triggers and triggerVcs pair by position: update_automation takes both or neither
            pair = {"triggers", "triggerVcs"} if kind == "automation" and {"triggers", "triggerVcs"} & set(changed) else set()
            p = {k: v for k, v in p.items() if k in changed or k in pair}
        if kind == "environment" and isinstance(p.get("variables"), dict):
            p["variables"] = [{"key": key, "value": value} for key, value in p["variables"].items()]
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
            result = client.call(f"create_{noun}", create_environment_args(p) if kind == "environment" else p)
            if additional is not None:
                if backend is None:
                    raise RuntimeError("Air automation backend is required to set additionalRepositories")
                automation_id = result.get("automationId") or result.get("id")
                if not automation_id:
                    raise RuntimeError(f"create_automation returned no automation id for {name}")
                backend.set_additional(automation_id, additional)
        elif kind == "environment":  # air-cloud has no delete_environment
            print(f"remove by hand in Air: environment {name}", file=sys.stderr)
        else:  # confirmName guards against deleting the wrong automation
            client.call("delete_automation", {id_key: x["id"], "confirmName": x["name"]})


def missing_secrets(plan: dict, envs: list[dict]) -> list[str]:
    """`set by a person: <env> secret <NAME>` for each declared secret the environment in Air does not have; the
    deploy never sets a secret value and does not fail on it."""
    have = {x["name"]: set(x.get("secrets") or []) for x in envs}
    return [f"set by a person: {e['name'][len(plan['prefix']):]} secret {s}" for e in plan["environments"]
            for s in e.get("secrets") or [] if s not in have.get(e["name"], set())]


def extra_variables(plan: dict, envs: list[dict]) -> list[str]:
    """`remove by hand in Air: <env> variable <NAME>` for each variable in Air that the plan does not have."""
    have = {x["name"]: x.get("variables") or {} for x in envs}
    return [f"remove by hand in Air: {e['name']} variable {k}" for e in plan["environments"]
            for k in have.get(e["name"], {}) if k not in (e.get("variables") or {})]


def deploy(mode: str, allow_delete: bool, plan: dict, automations: Mcp, cloud: Mcp, *, backend=None, run=None, yt_run=None) -> int:
    """Write only to Air and to repository labels (through `run`, a `gh` runner): deploy never creates, edits or closes a tracker ticket."""
    # The automations' repositories too: an environment moved off a repository is found there, then replaced.
    repo_urls = list(dict.fromkeys([e["repository"] for e in plan["environments"]] +
                                   [a["repository"]["browseUrl"] for a in plan["automations"]
                                    if isinstance(a.get("repository"), dict) and a["repository"].get("browseUrl")])) or [""]
    env_repositories = {e["name"]: e["repository"] for e in plan["environments"]}
    additional_names = {a["name"] for a in plan["automations"] if "additionalRepositories" in a}
    backend = backend or (Backend() if additional_names else None)
    state = read_state(automations, cloud, plan["prefix"], repo_urls, backend=backend,
                       additional_names=additional_names)
    changes = diff(plan, *state)
    for action, kind, name, _, _, changed in changes:
        print(f"{action} {kind} {name}" + (f" ({', '.join(changed)})" if changed else ""))
    if not changes:
        print("unchanged")
    for line in missing_secrets(plan, state[1]):
        print(line)
    for line in extra_variables(plan, state[1]):
        print(line)
    label_changes = reconcile_labels(plan, run)
    tag_changes = reconcile_tags(plan, yt_run)
    try:
        field_changes = reconcile_fields(plan, yt_run)
    except youtrack_tracker.MissingField as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if mode == "--dry-run":
        for action, repo, label, *rest in label_changes:
            print(f"{action} label {label} in {repo}" + (f" ({', '.join(rest[1])})" if action == "update" else ""))
        for action, tag in tag_changes:
            print(f"{action} tag {tag} in YouTrack")
        for action, name, value, _ in field_changes:
            print(f"{action} {name} value {value} in YouTrack")
        return 0
    for action, tag in tag_changes:  # a missing tag fails every handoff that adds it
        if action == "create":
            tag_store(plan, yt_run).create_tag(tag)
            print(f"created tag {tag} in YouTrack")
    for action, name, value, resolved in field_changes:
        if action == "create":
            tag_store(plan, yt_run).create_field_value(name, value, resolved)
        elif action == "update":
            tag_store(plan, yt_run).update_field_value(name, value, resolved)
        else:
            continue
        print(f"{action}d {name} value {value} in YouTrack")
    for action, repo, label, want, *_ in label_changes:  # a missing label fails every handoff that adds it
        tracker = label_tracker(repo, run)
        if action == "create":
            tracker.create_label(label, want["color"], want["description"])
        elif action == "update":
            tracker.update_label(label, want["color"], want["description"])
        else:
            continue
        print(f"{action}d label {label} in {repo}")
    removals = [c for c in label_changes if c[0] == "remove"]
    tag_removals = [t for a, t in tag_changes if a == "remove"]
    value_removals = [c for c in field_changes if c[0] == "remove"]
    if (removals or tag_removals or value_removals or any(c[0] == "delete" for c in changes)) and not allow_delete:
        for _, repo, label, _ in removals:
            print(f"refused: would remove label {label} in {repo}", file=sys.stderr)
        for tag in tag_removals:
            print(f"refused: would remove tag {tag} in YouTrack", file=sys.stderr)
        for _, name, value, _ in value_removals:
            print(f"refused: would remove {name} value {value} in YouTrack", file=sys.stderr)
        print("refused: the diff deletes items; rerun with --allow-delete", file=sys.stderr)
        return 1
    for _, repo, label, _ in removals:
        label_tracker(repo, run).delete_label(label, plan["owned_label_prefixes"])
        print(f"removed label {label} in {repo}")
    for tag in tag_removals:
        tag_store(plan, yt_run).delete_tag(tag, plan["owned_tag_prefixes"])
        print(f"removed tag {tag} in YouTrack")
    for _, name, value, _ in value_removals:
        tag_store(plan, yt_run).delete_field_value(name, value, plan["owned_field_prefixes"])
        print(f"removed {name} value {value} in YouTrack")
    if changes:
        apply_diff(changes, automations, cloud, repo_urls, backend=backend, env_repositories=env_repositories)
        after = read_state(automations, cloud, plan["prefix"], repo_urls, backend=backend,
                           additional_names=additional_names)
        state = after
        left = [c for c in diff(plan, *after) if not (c[0] == "delete" and c[1] == "environment")]
        for action, kind, name, *_ in left:
            print(f"verify failed: {action} {kind} {name} still differs", file=sys.stderr)
        if left:
            return 1
    for x in state[0]:  # a person registers each webhook with its sender; also on an unchanged plan
        for w in x["webhooks"]:
            print(f"webhook {x['name']}: trigger {w['id']}" + (f" {w['url']}" if w["url"] else ""))
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
        config = config_mod.load()
        if check_fixture_drift(config):
            return 1
        servers = [Mcp(os.environ[v]) for v in ("FACTORY_MCP_AIR_AUTOMATIONS", "FACTORY_MCP_AIR_CLOUD")]
        try:
            plan = {**plan_air(nodes_mod.load(), config), "renames": renames}
            run = github_tracker.ProjectWriteRun(github_tracker.gh, github_tracker.project_repositories(config),
                                                    issues_token_env=config.get("issues_token_env"))
            code = deploy(argv[0], "--allow-delete" in argv, plan, *servers, run=run)
            return code or write_files(plan["files"], argv[0] == "--apply")
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

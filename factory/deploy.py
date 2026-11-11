#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""The target-neutral deploy plan of the nodes of engine/nodes.yaml (ADR-0014): each node's spec, prompts and
triggers, its tier and focus variants, and the labels the trackers must carry. A surface plugin (ADR-0026) turns it
into its own shape: `air` (factory_plugins/air/deploy.py: the Air automations and environments, their diff and
apply), `local` (factory_plugins/local/surface.py).

Usage: deploy.py [--target SURFACE] [ARGS]...
Runs the `deploy --target SURFACE` command of the surface `--target` names, else of the team's `surface:`, else of
`air` (`plan --target air`, `--dry-run`, `--apply`: factory_plugins/air/deploy.py).
"""
import fnmatch
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod
import definition
import plugins
import project as project_mod
import scopes as scopes_mod
import vocab
from ports.system import TrackerSchema

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
    if n.get("output") == "as-instructed":
        spec["output"] = n["output"]  # the run opens its PR itself: the gate prints and checks it (#1261)
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
            repo: str | None = None, repo_tags: tuple = ()) -> dict:
    """`issues.labeled factory:ready` → an EVENT trigger of create_automation (air-automations)."""
    event, _, arg = str(text).partition(" ")
    arg = arg.replace("<tier>", tier or "")
    arg = arg.replace("<focus>", focus or "")
    if event.startswith("youtrack."):  # the filter value is case-sensitive: pass it through unchanged
        name, _, value = arg.partition("=")
        if repo and arg in repo_tags:  # a per-repository stage tag: only this repository's node listens to it
            arg = f"{arg}-{repo}"
        filters = {"tagAdded": arg} if event == "youtrack.tagAdded" else {name: value}
        return {"triggerType": "EVENT", "source": "youtrack", "eventType": "issueUpdated", "filters": filters}
    if event == "workflow_run":  # ADR-0021 `ci: failed`: Air fires only on a failed run (AIRA-725)
        return {"triggerType": "EVENT", "source": "github", "eventType": "workflow_run.failed",
                "filters": {"workflow_name": arg}}
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


def checkout_instructions(main_repository: dict | None, config: dict) -> str:
    primary = next(r for r in config["repositories"] if r.get("role") == "primary")
    team = {"url": config.get("team_repository") or primary["url"]}
    if not main_repository:
        return ""
    if main_repository["fullName"] == repository(team)["fullName"]:
        # Air's gh picks its token from the working directory's origin: the tracker's checkout reaches the tracker.
        tracker = (config.get("tracker") or {}).get("repository") or repository(primary)["fullName"]
        if tracker == main_repository["fullName"]:
            return ""
        return (f"## Working directory\n\nRun every command from `/workspaces/{tracker.rsplit('/', 1)[-1]}`: "
                "GitHub access there reaches the tracker. This automation's own checkout is the team repository.\n\n")
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
            + tail)


def node_repository_prefix(main_repository: dict | None, config: dict) -> str:
    """Select this ticket repository when a copied automation starts a factory command."""
    if not main_repository:
        return ""
    tracker = (config.get("tracker") or {}).get("repository")
    tickets = (config.get("tracker") or {}).get("repositories") or ([tracker] if tracker else [])
    repo = main_repository["fullName"]
    return f"FACTORY_NODE_REPO={repo} " if repo != tracker and repo in tickets else ""


def script_prompt(name: str, main_repository: dict | None = None, config: dict | None = None,
                  webhook: bool = False) -> str:
    checkout = checkout_instructions(main_repository, config) if config else ""
    command_prefix = node_repository_prefix(main_repository, config) if config else ""
    if name.startswith("doctor"):
        engine = name == "doctor-ops"
        command = f"`{command_prefix}factory doctor --engine`" if engine else f"`{command_prefix}factory doctor`"
        prompt = (f"Run the installed {command} command exactly once in the foreground with "
                  + ("only the `--engine` argument. " if engine else "no arguments. ")
                  + f"Do not use `uv run`, `--help` or add {'other ' if engine else ''}arguments. Report every JSON probe line and the command's exit code. "
                  "It creates a temporary probe issue, a child issue and a PR, then deletes them when possible. "
                  "A failed probe creates or updates a `Doctor: <probe> fails` ticket; denied ticket writes appear as a "
                  "`report` probe line. You must not create, edit or close tickets yourself.")
    elif name == "reconcile":
        prompt = ("This step runs one command and judges nothing: it changes no file, label, comment or review itself. "
                  f"Run `{command_prefix}factory reconcile` in the foreground with no time limit; wait until it exits, and report its JSON output.")
    else:
        args = SCRIPT_ARGS.get(name, "")
        prompt = SCRIPT_PROMPT.format(args=args).replace("`factory ", f"`{command_prefix}factory ")
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
    checkout = checkout_instructions(main_repository, config)
    import definition
    if definition.present(config_mod.team_root()):  # ADR-0021: the node's role body, rendered with its settings
        defn = definition.load(config_mod.team_root())
        agent = (defn["factory"].get("agents") or {}).get(((defn["workflow"].get("nodes") or {}).get(name) or {}).get("agent")) or {}
        if agent.get("run"):  # ADR-0021: a step with no judgement: the agent only runs the engine command
            prefix = node_repository_prefix(main_repository, config)
            return (f"{who}\n\n{checkout}Run `{prefix}factory {agent['run']} --node {name} {begin_arg(name, n)}` once, in the "
                    "foreground, and wait until it exits. Copy only that one number. It applies "
                    "the outcome itself: do not run `factory begin` or `factory end`, and change nothing yourself. "
                    "Report its JSON output." + INSTALLED_FACTORY)
        text = definition.render(defn, name, (main_repository or {}).get("fullName")).strip()
        skills = definition.skill_lines(defn, role)
        servers = [f"{k} (Air connector {e['connector']})" if "connector" in e else f"{k} (custom, from .mcp.json)"
                   for k, e in definition.mcp(defn, name).items()]
    else:
        text = "\n\n".join(config_mod.resolve(f).read_text().strip() for f in (f"roles/{role}/role.md", "roles/_common.md"))
        skills = skill_lines(role)
    mcp = ("## Your MCP servers\n\n" + "\n".join(f"- {c}" for c in servers) + "\n\n") if servers else ""
    arg = begin_arg(name, n)
    arg = f" {arg}" if arg else ""
    command_prefix = node_repository_prefix(main_repository, config)
    return (f"{who}\n\n{checkout}{text}\n\n## Your skills\n\n{skills}\n\n{mcp}## This step\n\n{this_step(n, focus)}\n\n"
            "## How you start and finish\n\n"
            f"1. Run `{command_prefix}factory begin {aname}{arg}`.{' Copy only that one number; never write the payload or any JSON.' if arg else ''} "
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


def default_environment(nodes: dict, defn: dict | None) -> str | None:
    """The environment a node runs in unless it names one: the definition's `settings.environment`, else the one
    most nodes run in."""
    if defn:
        return definition.default_environment(defn)
    envs = [n["env"] for n in nodes["nodes"].values() if n.get("env")]
    return max(dict.fromkeys(envs), key=envs.count) if envs else None


def scope_filter(trig: dict, block: dict) -> dict:
    """An EVENT trigger of a project environment (#1069), narrowed to its scope where Air can filter on it: a YouTrack
    event also matches the scope field's value. A GitHub event has no filter on a label the issue already carries
    (`label` is the label the event adds), so the gate's scope check leaves another project's ticket alone."""
    scope = block.get("scope") or {}
    if trig.get("source") == "youtrack" and scope.get("field"):
        return {**trig, "filters": {**trig.get("filters", {}), scope["field"]: scope["value"]}}
    return trig


def team_definition() -> dict | None:
    """The team's definition (ADR-0021), else None (a descriptor team)."""
    return definition.load(config_mod.team_root()) if definition.present(config_mod.team_root()) else None


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


def tracker_schema(nodes: dict, config: dict, repositories=()) -> TrackerSchema:
    """The labels the trackers and code hosts must carry: the core's `labels()` and `label_specs()`, plus what the
    selected plugins' `deploy.schema` hooks add ({label: spec})."""
    names, specs = labels(nodes, config), label_specs(nodes, config)
    extra = {k: v for hook in plugins.registry(config).hook("deploy.schema") for k, v in hook(nodes, config).items()}
    if extra:
        names = sorted({*names, *extra})
        specs = {**{k: v for k, v in extra.items() if v}, **specs}
    return TrackerSchema(tuple(names), tuple(repositories), specs, tuple(owned_label_prefixes(config)))


def actions_files(nodes: dict, config: dict) -> dict:
    """{path: text} of the workflows the graph's `runner: actions` nodes render in the factory repository (hook
    `deploy.plan_files`: the github plugin renders GitHub Actions workflows)."""
    return {k: v for hook in plugins.registry(config).hook("deploy.plan_files") for k, v in hook(nodes, config).items()}


def sync_wake_files(config: dict, run, apply: bool) -> int:
    """Hook `deploy.files`: each selected code host installs (or removes) the files that wake a node in its project
    repositories (github: the comment wake workflow and its script while the comments plugin is on, ADR-0025)."""
    for hook in plugins.registry(config).hook("deploy.files"):
        if code := hook(config, run, apply):
            return code
    return 0


COMPLEXITY_DESCRIPTION = "Size grade set by intake; chooses the developer tier"


def owned_label_prefixes(config: dict) -> list[str]:
    """The label namespaces the deploy may remove from: the stage namespace (`factory:`) and the risk and complexity groups."""
    t = config.get("tracker") or {}
    ready = (t.get("stages") or {}).get("ready") or vocab.stage("ready")
    risks = (t.get("risk_labels") or {"low": "risk:low", "high": "risk:high"}).values()
    return sorted({t.get("label_prefix") or ready.partition(":")[0] + ":", "complexity:", *(r.partition(":")[0] + ":" for r in risks)})


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


def label_plan(nodes: dict, config: dict, tickets=()) -> dict[str, list[str]]:
    """{repository: [labels]} for a definition with `tracker.repositories` (ADR-0024). Every ticket repository (those,
    then `tickets`) gets the tracker schema's labels and the hold and review-trigger labels; the tracker (the first one)
    also gets the map label. A project repository that holds no tickets maps to `[]`, so `reconcile_labels` removes
    the factory-owned labels it still has."""
    t = config.get("tracker") or {}
    tickets = list(dict.fromkeys([*(definition.full_name(r) for r in t.get("repositories") or []), *tickets]))
    common = sorted({*tracker_schema(nodes, config).labels,
                     *(x for x in (t.get("hold_label"), t.get("review_trigger")) if x)})
    out = {r: [*common, *([t["map_label"]] if i == 0 and t.get("map_label") else [])] for i, r in enumerate(tickets)}
    for r in config.get("repositories") or []:
        out.setdefault(repository(r)["fullName"], [])
    return {r: sorted(set(ls)) for r, ls in out.items()}


def reconcile_labels(plan: dict, run=None, config=None) -> list[tuple]:
    """(action, repository, label, changed fields) per required label to create or update, and per factory-owned
    label no longer required to remove, from the selected plugins' `deploy.labels` hooks (github: each repository of
    the plan's `label_plan`, independently)."""
    schema = TrackerSchema.from_plan(plan)
    return [c for hook in plugins.registry(config).hook("deploy.labels") for c in hook(schema, run)]


def provision_labels(plan: dict, changes: list, apply: str, run=None, config=None) -> None:
    """Write label `changes` through the `deploy.provision` hooks: `apply` is `write` (create, update) or `remove`."""
    schema = TrackerSchema.from_plan(plan)
    for hook in plugins.registry(config).hook("deploy.provision"):
        for line in hook(schema, changes, apply, run):
            print(line)


def labels(nodes: dict, config: dict) -> list[str]:
    """Every label the gate may write or a trigger filters on: stage labels, risk and complexity labels, and
    the command labels of the nodes with `<tier>` expanded over each node's tiers (a missing label makes
    `gh issue edit --add-label` fail, so the deploy creates them first)."""
    t = config.get("tracker") or {}
    out = set((t.get("stages") or {}).values()) | set((t.get("risk_labels") or {"low": "risk:low", "high": "risk:high"}).values())
    out.add(vocab.needs_human())
    out.add(vocab.lesson())  # ADR-0021: a ticket with uncollected lessons
    # ADR-0023: scripts/comment-wake-label.sh adds it; the comment node removes it. Kept while the comments module is
    # off (ADR-0025): a wake workflow an earlier deploy installed still adds it, and fails once the label is gone.
    out.add(vocab.comment())
    out |= {vocab.ci("ci-green"), vocab.ci("ci-red")}  # ci-sync writes them from the head checks (#1207)
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


def surface_name(config: dict) -> str | None:
    """The surface the team deploys to (ADR-0026): `surface:`, else `air` when `targets.air` is set, else None."""
    return config.get("surface") or ("air" if (config.get("targets") or {}).get("air") else None)


def output_errors(nodes: dict, config: dict) -> list[str]:
    """The `deploy.errors` of the selected plugins (youtrack: `tracker.projects`), then `unsupported-output` per deployed
    node whose `output:` its surface cannot serve: the configured `surface:`, else every surface the selection provides
    once `targets.air` turns on `air`. No surface: no output is checked."""
    errs = [e for hook in plugins.registry(config).hook("deploy.errors") for e in hook(nodes, config)]
    name = surface_name(config)
    return errs + (_output_errors(nodes, config, name) if name else [])


def _output_errors(nodes: dict, config: dict, name: str) -> list[str]:
    registry = plugins.registry(config, use=(("surface", name),))
    names = [name] if config.get("surface") else registry.impls("surface")
    if not (classes := [cls for n in names if (cls := registry.impl("surface", n))]):
        return [plugins.impl_error(config, "surface", name, "surface")]
    supported = set().union(*(cls().outputs() for cls in classes))
    defn = team_definition()
    env_errs = [e for cls in classes if hasattr(cls, "environment_errors") and defn is not None
                and definition.adopted(defn) for e in cls().environment_errors(defn)]  # ADR-0027
    trigger_errs = [e for cls in classes if hasattr(cls, "trigger_output_errors") and defn is not None
                    and definition.adopted(defn) for e in cls().trigger_output_errors(defn, nodes)]
    return env_errs + trigger_errs + [f"unsupported-output: nodes.{k}: output {n['output']!r} is not supported by surface {', '.join(names)} "
            f"(supported: {', '.join(sorted(supported))})"
            for k, n in nodes["nodes"].items() if n.get("output") and n["output"] not in supported
            and n.get("enabled") is not False and n.get("role") != "human" and not n.get("runner")]


def main(argv: list[str]) -> int:
    """`factory deploy`: the `deploy --target <surface>` command of the surface `--target` names, else of the team's
    (`surface_name`; `air` when the config names none, or cannot be read). `plan` is Air's: only it renders a plan."""
    argv = config_mod.pop_team(argv)
    if argv[:1] == ["--target"] and len(argv) > 1 and argv[1] in plugins.IMPLIED["surface"]:
        surface, argv = argv[1], argv[2:]
    elif argv[:1] in (["plan"], ["--target"]):  # Air's usage names any other target
        surface = "air"
    else:
        try:
            config = config_mod.load()
        except (Exception, SystemExit):  # the surface's own deploy reports it, as before
            config = {}
        surface = surface_name(config) or "air"
    command = plugins.registry({}, use=(("surface", surface),)).command(f"deploy --target {surface}")
    if command is None:
        print(f"deploy: no plugin deploys surface {surface}", file=sys.stderr)
        print(__doc__)
        return 2
    return command(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

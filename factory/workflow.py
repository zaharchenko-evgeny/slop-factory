#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Workflow files, format v2 (ADR-0016): nodes name states and commands from factory.project.yaml,
never tracker labels or event names. `migrate` converts a nodes.yaml and its team descriptor into v2; `copy_project`
copies a v2 team's descriptor and workflow (`factory migrate project`, how factory/tests/fixtures/slop/ is made).

Usage: workflow.py migrate <nodes.yaml> <factory.descriptor.yaml> [--out DIR]
       workflow.py project <team checkout> [--out DIR]
Each validate error starts with a name: unknown-state, unknown-command, unknown-schedule, unknown-webhook, unknown-wake,
unknown-effect, unknown-condition, uncovered-state, and the agent errors of `project.node_agents`.
"""
import argparse
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import plugins  # noqa: E402  (ADR-0026: a plugin's effects; definition.errors refuses them while it is off)
import project as project_mod  # noqa: E402

FORMAT = 2
WORKFLOW_PATH = "workflows/delivery.yaml"
# The team commit a `copy_project` output was copied from.
SOURCE = "SOURCE"
EFFECTS = {"state", "command", "uncommand", "comment", "review", "create-issues", "merge", "update-branch",
           "set-field", "join", "request-reviewers", "park-behind-main-red", "assign", "unassign", "create-follow-up"}
PLACEHOLDERS = ("<tier>", "<focus>")
# GitHub adapter: wakes that are events, not a state entered or a command given.
EVENTS = {"manual": "manual", "pr pushed": "pull_request.synchronize", "pr opened": "pull_request.opened",
          "pr commented": "issue_comment.created",
          "pr reviewed": "pull_request_review.submitted", "ticket closed": "issues.closed",
          "ticket commented": "issues.comment", "ticket unlabeled": "issues.unlabeled"}
PR_STATE_NAMES = {"merged": "pr-merged", "open": "pr-open"}


def _defaults():
    return yaml.safe_load((config.engine_root() / "engine/defaults.yaml").read_text()) or {}


def load(team_root=None, descriptor=None):
    """The workflow file a project's descriptor links with `workflow:`."""
    root = Path(team_root or config.team_root())
    import definition
    if definition.present(root):  # ADR-0021
        return definition.to_project(definition.load(root))[1]
    descriptor = project_mod._read(root) if descriptor is None else descriptor
    return yaml.safe_load((root / descriptor["workflow"]).read_text()) or {}


def _template(name):
    for placeholder in PLACEHOLDERS:
        if placeholder in name:
            prefix, _, suffix = name.partition(placeholder)
            return placeholder, prefix, suffix
    return None


def resolve(commands, ref):
    """(command, store kind, store value) of a reference: a declared name, or a template's name with its
    placeholder filled (`implement-cheap`, `implement-<next tier>`, `implement-*`)."""
    match = None
    if ref in commands:
        match = (ref, None, None)
    else:
        for name in commands:
            parts = _template(name)
            if not parts:
                continue
            placeholder, prefix, suffix = parts
            if len(ref) > len(prefix) + len(suffix) and ref.startswith(prefix) and ref.endswith(suffix):
                if match is None or len(prefix) + len(suffix) > len(match[1][1]) + len(match[1][2]):
                    match = (name, parts, ref[len(prefix):len(ref) - len(suffix)])
    if match is None:
        raise KeyError(ref)
    name, parts, filler = match
    store = (commands[name] or {}).get("store")
    kind = project_mod.store_kind(store)
    value = store[kind]
    if parts and isinstance(value, str):
        value = value.replace(parts[0], filler)
    return name, kind, value


def _primary(project):
    return next((r["name"] for r in project.get("repositories") or [] if r.get("role") == "primary"), None)


def validate(project, wf):
    """Errors of a v2 workflow against its project's states and commands."""
    errs = []
    schema = json.loads((config.engine_root() / "engine/workflow.schema.json").read_text())
    config._check(schema, wf, "workflow", errs)
    if not isinstance(wf, dict) or not isinstance(wf.get("nodes"), dict):
        return errs
    errs += config.mcp_errors(project.get("roles"), ((project.get("targets") or {}).get("air") or {}).get("mcp_connectors"), wf)
    errs += project_mod.node_agents(project, wf["nodes"])[1]
    states, commands = project.get("states") or {}, project.get("commands") or {}
    schedules, webhooks, covered = wf.get("schedules") or {}, wf.get("webhooks") or {}, set()

    def state(where, s):
        if s != "none" and s not in states:
            errs.append(f"unknown-state: {where}: {s}")

    def command(where, c):
        try:
            resolve(commands, c)
        except (KeyError, ValueError):
            errs.append(f"unknown-command: {where}: {c}")

    for name, n in wf["nodes"].items():
        if not isinstance(n, dict):
            continue
        for wake in n.get("wakes_on") or []:
            verb, _, arg = str(wake).partition(" ")
            if wake in EVENTS or verb == "run":
                continue
            if verb == "state":
                state(f"{name}.wakes_on", arg)
                covered.add(arg)
            elif verb == "command":
                command(f"{name}.wakes_on", arg)
            elif verb == "schedule":
                if arg not in schedules:
                    errs.append(f"unknown-schedule: {name}: {arg}")
            elif verb == "webhook":
                if arg not in webhooks:
                    errs.append(f"unknown-webhook: {name}: {arg}")
            else:
                errs.append(f"unknown-wake: {name}: {wake}")
        when = n.get("when") if isinstance(n.get("when"), dict) else {}
        if "stage" in when:
            errs.append(f"unknown-condition: {name}: a v2 workflow names `when.state`, not `when.stage`")
        current = when.get("state")
        for s in current if isinstance(current, list) else [current] if current else []:
            state(f"{name}.when", s)
            covered.add(s)
        for oname, o in (n.get("outcomes") or {}).items():
            o = o if isinstance(o, dict) else {}
            for s in o.get("from") or []:
                state(f"{name}.{oname}.from", s)
                covered.add(s)
            for e in o.get("effects") or []:
                keys = [k for k in e if k != "on"] if isinstance(e, dict) else []
                if len(keys) != 1 or keys[0] not in EFFECTS and not plugins.owner(keys[0], "effect"):
                    errs.append(f"unknown-effect: {name}.{oname}: {e}")
                elif keys[0] == "state":
                    state(f"{name}.{oname}", e["state"])
                elif keys[0] in ("command", "uncommand"):
                    command(f"{name}.{oname}", e[keys[0]])
    errs += [f"uncovered-state: {s}: no node wakes on it, waits in it or moves from it"
             for s, entry in states.items()
             if isinstance(entry, dict) and entry.get("kind") == "active" and s not in covered]
    return errs


def migrate(nodes, descriptor):
    """(project descriptor, v2 workflow) for a nodes.yaml and its factory.descriptor.yaml."""
    tracker = config.merge(_defaults(), descriptor)["tracker"]
    stages, kinds = tracker["stages"], tracker.get("stage_kinds") or {}
    by_stage_label = {label: name for name, label in stages.items()}
    by_fact_label = {label: fact for fact, label in (tracker.get("pr_fact_labels") or {}).items()}
    by_event = {event: wake for wake, event in EVENTS.items()}
    states = {name: {"kind": kinds[name], "store": {"label": label}} for name, label in stages.items()}
    commands = {}

    def pr_state(fact):
        name = PR_STATE_NAMES.get(fact, fact)
        name = f"pr-{fact}" if name in stages else name
        states.setdefault(name, {"kind": "final" if fact == "merged" else "active", "store": {"pr": fact}})
        return name

    def command_name(label):
        return label.split(":", 1)[-1]

    def wake(trigger):
        if trigger in by_event:
            return by_event[trigger]
        event, _, arg = trigger.partition(" ")
        if event in ("schedule", "webhook"):
            return trigger
        if event == "workflow_run":
            return f"run {arg}"
        if event == "pull_request.merged":
            return f"state {pr_state('merged')}"
        if event == "issues.labeled" and arg in by_stage_label:
            return f"state {by_stage_label[arg]}"
        if event == "pull_request.labeled" and arg in by_fact_label:
            return f"state {pr_state(by_fact_label[arg])}"
        if event in ("issues.labeled", "pull_request.labeled"):
            name, store = command_name(arg), {"label" if event == "issues.labeled" else "pr_label": arg}
            if commands.get(name, {"store": store})["store"] != store:
                raise ValueError(f"cannot migrate trigger {trigger}: command {name} has another store")
            commands[name] = {"store": store}
            return f"command {name}"
        raise ValueError(f"cannot migrate trigger: {trigger}")

    def effect(e):
        key = next(k for k in e if k != "on")
        if key == "stage":
            return {"state": e["stage"]}
        if key not in ("label", "unlabel"):
            return copy.deepcopy(e)
        label, kind = e[key], "pr_label" if e.get("on") == "pr" else "label"
        name = command_name(label)
        try:
            _, found_kind, value = resolve(commands, name)
        except KeyError:
            commands[name] = {"store": {kind: label}}
            found_kind, value = kind, label
        if (found_kind, value) != (kind, label):
            raise ValueError(f"cannot migrate effect {e}: command {name} is {found_kind} {value}")
        return {"command" if key == "label" else "uncommand": name}

    triggers = {name: [wake(t) for t in n.get("triggers") or []] for name, n in nodes["nodes"].items()}
    wf_nodes = {}
    for name, n in nodes["nodes"].items():
        node = {}
        for key, value in n.items():
            if key == "triggers":
                node["wakes_on"] = triggers[name]
            elif key == "when":
                node["when"] = {("state" if k == "stage" else k): copy.deepcopy(v) for k, v in value.items()}
            elif key == "outcomes":
                node["outcomes"] = {}
                for oname, o in value.items():
                    o = copy.deepcopy(o or {})
                    if "effects" in o:
                        o["effects"] = [effect(e) for e in o["effects"]]
                    node["outcomes"][oname] = o
            else:
                node[key] = copy.deepcopy(value)
        wf_nodes[name] = node
    wf = {"format": FORMAT, **{k: copy.deepcopy(v) for k, v in nodes.items() if k not in ("nodes", "environments")},
          "nodes": wf_nodes}
    project = {k: copy.deepcopy(v) for k, v in descriptor.items() if k != "nodes"}
    project["tracker"] = {k: v for k, v in (project.get("tracker") or {}).items() if k not in ("stages", "stage_kinds")}
    primary = _primary(descriptor)
    environments = {name: {("repositories" if k == "repository" else k):
                           ([primary if v == "primary" else v] if k == "repository" else copy.deepcopy(v))
                           for k, v in env.items()} for name, env in (nodes.get("environments") or {}).items()}
    if environments:
        project["environments"] = environments
    project.update({"workflow": WORKFLOW_PATH, "states": states, "commands": commands})
    return project, wf


def dump(data):
    return yaml.safe_dump(data, sort_keys=False, width=1000, allow_unicode=True)


def copy_project(team, out):
    """Copy a v2 team checkout's factory.project.yaml and the workflow it links into `out` byte for byte, after
    validating them, and write the team's HEAD commit to SOURCE. Returns the written paths."""
    team, out = Path(team), Path(out)
    import definition
    if definition.present(team):  # ADR-0021: factory.yaml, workflow.yaml and every file under roles/
        errors = definition.errors(definition.load(team))
        if errors:
            raise ValueError("; ".join(errors))
        files = [definition.FACTORY, definition.WORKFLOW,
                 *sorted(p.relative_to(team).as_posix() for p in (team / definition.ROLES).rglob("*") if p.is_file())]
        return _copy(team, out, files)
    descriptor = project_mod.load(team)
    wf = load(team, descriptor)
    if wf.get("format") != FORMAT:
        raise ValueError(f"{descriptor['workflow']}: not format {FORMAT}; convert it with `factory migrate workflow`")
    errors = validate(descriptor, wf)
    if errors:
        raise ValueError("; ".join(errors))
    return _copy(team, out, [config.PROJECT_DESCRIPTOR, descriptor["workflow"]])


def _copy(team, out, files):
    """Copy `files` of a clean team checkout into `out`; SOURCE gets the team's HEAD commit, then its origin URL."""
    def git(*args):
        return subprocess.run(["git", "-C", str(team), *args], capture_output=True, text=True, check=True).stdout.strip()
    try:
        if git("status", "--porcelain", "--", *files):
            raise ValueError(f"{team}: uncommitted changes in {', '.join(files)}")
        commit = git("rev-parse", "HEAD")
    except (OSError, subprocess.CalledProcessError) as e:
        raise ValueError(f"{team}: not a git checkout ({e})") from e
    stale = [p for p in out.rglob("*") if p.is_file() and p.relative_to(out).as_posix() not in {*files, SOURCE}] \
        if (out / SOURCE).is_file() else []  # prune only an earlier copy: a file the team no longer has
    written = []
    for rel in files:
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        written.append(str(shutil.copyfile(team / rel, out / rel)))
    for p in stale:
        p.unlink()
    url = subprocess.run(["git", "-C", str(team), "remote", "get-url", "origin"], capture_output=True, text=True)
    url = url.stdout.strip().removesuffix(".git") if url.returncode == 0 else ""
    (out / SOURCE).write_text(f"{commit}\n" + (f"{url}\n" if url else ""))
    return [*written, str(out / SOURCE)]


def project_main(argv):
    p = argparse.ArgumentParser(prog="factory migrate project")
    p.add_argument("team")
    p.add_argument("--out", default=".factory/migrate")
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return 2
    try:
        written = copy_project(a.team, a.out)
    except (KeyError, ValueError, OSError, yaml.YAMLError) as e:
        print(f"migrate project: {e}", file=sys.stderr)
        return 1
    print(json.dumps({"written": written}))
    return 0


def main(argv):
    p = argparse.ArgumentParser(prog="workflow.py")
    p.add_argument("cmd", choices=["migrate"])
    p.add_argument("nodes")
    p.add_argument("descriptor")
    p.add_argument("--out", default=".factory/migrate")
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return 2
    nodes = yaml.safe_load(Path(a.nodes).read_text())
    descriptor = yaml.safe_load(Path(a.descriptor).read_text()) or {}
    try:
        project, wf = migrate(nodes, descriptor)
    except (KeyError, ValueError) as e:
        print(f"migrate: {e}", file=sys.stderr)
        return 1
    out = Path(a.out)
    (out / WORKFLOW_PATH).parent.mkdir(parents=True, exist_ok=True)
    (out / config.PROJECT_DESCRIPTOR).write_text(dump(project))
    (out / WORKFLOW_PATH).write_text(dump(wf))
    print(json.dumps({"project": str(out / config.PROJECT_DESCRIPTOR), "workflow": str(out / WORKFLOW_PATH)}))
    return 0


if __name__ == "__main__":
    argv = config.pop_team(sys.argv[1:])
    sys.exit(project_main(argv[1:]) if argv[:1] == ["project"] else main(argv))

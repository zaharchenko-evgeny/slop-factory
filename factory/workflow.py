#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Workflow files, format v2 (ADR-0016): nodes name states and commands from factory.project.yaml,
never tracker labels or event names. `to_nodes` renders a v2 workflow into the node shape the gate
and deploy read (ADR-0014); `migrate` converts a nodes.yaml and its team descriptor into v2.

Usage: workflow.py migrate <nodes.yaml> <factory.descriptor.yaml> [--out DIR]
Each validate error starts with a name: unknown-state, unknown-command, unknown-schedule, unknown-webhook, unknown-wake,
unknown-effect, unknown-condition, uncovered-state.
"""
import argparse
import copy
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import project as project_mod  # noqa: E402

FORMAT = 2
WORKFLOW_PATH = "workflows/delivery.yaml"
EFFECTS = {"state", "command", "uncommand", "comment", "review", "create-issues", "merge", "update-branch",
           "set-field", "join", "request-reviewers", "park-behind-main-red"}
PLACEHOLDERS = ("<tier>", "<focus>")
# GitHub adapter: wakes that are events, not a state entered or a command given.
EVENTS = {"manual": "manual", "pr pushed": "pull_request.synchronize", "pr opened": "pull_request.opened",
          "pr commented": "issue_comment.created",
          "pr reviewed": "pull_request_review.submitted", "ticket closed": "issues.closed",
          "ticket commented": "issues.comment", "ticket unlabeled": "issues.unlabeled"}
# PR facts the GitHub adapter reads from an event; the others come from `tracker.pr_fact_labels`.
PR_FACT_EVENTS = {"merged": "pull_request.merged", "open": "pull_request.opened"}
PR_STATE_NAMES = {"merged": "pr-merged", "open": "pr-open"}


def _defaults():
    return yaml.safe_load((config.engine_root() / "engine/defaults.yaml").read_text()) or {}


def load(team_root=None, descriptor=None):
    """The workflow file a project's descriptor links with `workflow:`."""
    root = Path(team_root or config.team_root())
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


def _target(kind, what, youtrack=False):
    """`issue` or `pr`: where the adapter keeps a label store (a YouTrack tag is on the ticket)."""
    if kind == "label" or (youtrack and kind == "tag"):
        return "issue"
    if kind == "pr_label":
        return "pr"
    raise ValueError(f"the GitHub adapter cannot keep {what} in a {kind} store")


def _youtrack_wake(kind, value):
    """A YouTrack ticket event as a node trigger: `youtrack.tagAdded <tag>` or `youtrack.fieldChanged <field>=<value>`."""
    if kind == "tag":
        return f"youtrack.tagAdded {value}"
    if kind == "field":
        return f"youtrack.fieldChanged {value['name']}={value['value']}"
    return None


def render_wake(wake, project):
    """A wake (`state ready`, `command implement-<tier>`, `pr pushed`, `schedule retro`, `webhook dry-run-finished`) as a node trigger."""
    if wake in EVENTS:
        return EVENTS[wake]
    verb, _, arg = wake.partition(" ")
    youtrack = (project.get("tracker") or {}).get("adapter") == "youtrack"
    if verb in ("schedule", "webhook"):
        return wake
    if verb == "run":
        return f"workflow_run {arg}"
    if verb == "state":
        store = project["states"][arg]["store"]
        kind = project_mod.store_kind(store)
        if youtrack and _youtrack_wake(kind, store[kind]):
            return _youtrack_wake(kind, store[kind])
        if kind == "label":
            return f"issues.labeled {store['label']}"
        if kind == "pr":
            fact = store["pr"]
            label = (project.get("tracker") or {}).get("pr_fact_labels", {}).get(fact)
            if fact in PR_FACT_EVENTS:
                return PR_FACT_EVENTS[fact]
            if label:
                return f"pull_request.labeled {label}"
        raise ValueError(f"the GitHub adapter cannot wake on state {arg} ({kind}: {store[kind]})")
    if verb == "command":
        _, kind, value = resolve(project["commands"], arg)
        if youtrack and kind == "tag":
            return _youtrack_wake(kind, value)
        return f"{'issues' if _target(kind, arg, youtrack) == 'issue' else 'pull_request'}.labeled {value}"
    raise ValueError(f"unknown wake: {wake}")


def render_effect(effect, project):
    key = next(k for k in effect if k != "on")
    if key == "state":
        return {"stage": effect["state"]}
    if key in ("command", "uncommand"):
        _, kind, value = resolve(project["commands"], effect[key])
        out = {"label" if key == "command" else "unlabel": value}
        if _target(kind, effect[key], (project.get("tracker") or {}).get("adapter") == "youtrack") == "pr":
            out["on"] = "pr"
        return out
    return copy.deepcopy(effect)


def _render_outcome(outcome, project):
    out = copy.deepcopy(outcome or {})
    if "effects" in out:
        out["effects"] = [render_effect(e, project) for e in out["effects"]]
    return out


def _primary(project):
    return next((r["name"] for r in project.get("repositories") or [] if r.get("role") == "primary"), None)


def render_environments(project):
    """The descriptor's environments in the nodes.yaml shape: one `repository`, `primary` for the primary one."""
    primary, out = _primary(project), {}
    for name, env in (project.get("environments") or {}).items():
        out[name] = {}
        for key, value in env.items():
            if key == "repositories":
                out[name]["repository"] = "primary" if value and value[0] == primary else (value or [None])[0]
            else:
                out[name][key] = copy.deepcopy(value)
    return out


def to_nodes(project, wf):
    """The nodes.yaml shape (triggers, `when.stage`, label effects, environments) of a v2 workflow under a project."""
    project = config.merge(_defaults(), project)
    out = {k: copy.deepcopy(v) for k, v in wf.items() if k not in ("format", "nodes")}
    if "environments" not in out and project.get("environments"):
        out = {"environments": render_environments(project), **out}
    out["nodes"] = {}
    for name, n in (wf.get("nodes") or {}).items():
        node = {}
        for key, value in n.items():
            if key == "wakes_on":
                node["triggers"] = [render_wake(w, project) for w in value]
            elif key == "when":
                node["when"] = {("stage" if k == "state" else k): copy.deepcopy(v) for k, v in value.items()}
            elif key == "outcomes":
                node["outcomes"] = {o: _render_outcome(oc, project) for o, oc in value.items()}
            else:
                node[key] = copy.deepcopy(value)
        out["nodes"][name] = node
    return out


def validate(project, wf):
    """Errors of a v2 workflow against its project's states and commands."""
    errs = []
    schema = json.loads((config.engine_root() / "engine/workflow.schema.json").read_text())
    config._check(schema, wf, "workflow", errs)
    if not isinstance(wf, dict) or not isinstance(wf.get("nodes"), dict):
        return errs
    errs += config.mcp_errors(project.get("roles"), ((project.get("targets") or {}).get("air") or {}).get("mcp_connectors"), wf)
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
                if len(keys) != 1 or keys[0] not in EFFECTS:
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
    sys.exit(main(config.pop_team(sys.argv[1:])))

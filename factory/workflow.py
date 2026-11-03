#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Workflow file v2 (ADR-0016): nodes name states and commands from the project descriptor, never labels or events.

Usage: workflow.py migrate <nodes.yaml> <descriptor.yaml> [out dir]
Each error line starts with a name: bad-schema, unknown-state, unknown-command, unknown-wake, unknown-effect, uncovered-state.
"""
import json
import os
import sys
from fnmatch import fnmatchcase
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

EFFECTS = {"state", "command", "uncommand", "comment", "review", "create-issues", "merge", "update-branch", "set-field", "join", "request-reviewers", "park-behind-main-red"}
# A wake that is neither a state entered nor a command: the event, as a fact of the PR or the ticket.
FACTS = {"pull_request.merged": "pr merged", "pull_request.synchronize": "pr pushed", "pull_request_review.submitted": "pr review-submitted",
         "issues.closed": "ticket closed", "issues.comment": "ticket comment", "manual": "manual"}
FACT_EVENTS = {v: k for k, v in FACTS.items()}
LABEL_EVENTS = {"issues.labeled": "label", "pull_request.labeled": "pr_label"}
STORE_EVENTS = {v: k for k, v in LABEL_EVENTS.items()}
RENAMED = {"stage": "state", "label": "command", "unlabel": "uncommand"}
UNRENAMED = {v: k for k, v in RENAMED.items()}


def load(team_root):
    """The workflow `workflow:` of the project descriptor names."""
    import project
    return yaml.safe_load((Path(team_root) / project.load(team_root)["workflow"]).read_text())


def command_label(name, commands):
    """The label behind a command name, which may be `<next tier>` or a `*` pattern; None when undeclared."""
    key = name.replace("<next tier>", "<tier>")
    if key in commands:
        label = next(iter(commands[key]["store"].values()))
        return label.replace("<tier>", "<next tier>") if key != name else label
    for known, command in commands.items():
        label = next(iter(command["store"].values()))
        if "*" in name and fnmatchcase(known, name) and label.endswith(known):
            return label[:-len(known)] + name
    return None


def validate(workflow, states, commands):
    """Errors for a v2 workflow against the descriptor's `states` and `commands`."""
    errors = []
    schema = json.loads((config.engine_root() / "engine/workflow.schema.json").read_text())
    config._check(schema, workflow, "workflow", errors)
    if errors:
        return [f"bad-schema: {e}" for e in errors]
    schedules = workflow.get("schedules") or {}
    covered = set()

    def state(name, where):
        covered.add(name)
        if name not in states and name != "none":
            errors.append(f"unknown-state: {where} names {name}")

    def command(name, where):
        if command_label(name, commands) is None:
            errors.append(f"unknown-command: {where} names {name}")

    for nname, node in workflow["nodes"].items():
        for wake in node["wakes_on"]:
            kind, _, arg = wake.partition(" ")
            if kind == "state":
                state(arg, f"{nname} wakes_on")
            elif kind == "command":
                command(arg, f"{nname} wakes_on")
            elif not (kind == "schedule" and arg in schedules or wake in FACT_EVENTS):
                errors.append(f"unknown-wake: {nname} wakes_on {wake}")
        wanted = (node.get("when") or {}).get("state") or []
        for name in [wanted] if isinstance(wanted, str) else wanted:
            state(name, f"{nname} when")
        for oname, outcome in node["outcomes"].items():
            where = f"{nname}.{oname}"
            for name in outcome.get("from") or []:
                state(name, where)
            for effect in outcome.get("effects") or []:
                kinds = set(effect) - {"on"}
                if not kinds or not kinds <= EFFECTS:
                    errors.append(f"unknown-effect: {where} has {sorted(kinds - EFFECTS) or 'no effect'}")
                for key, check in (("state", state), ("command", command), ("uncommand", command)):
                    if key in effect:
                        check(effect[key], where)
    errors += [f"uncovered-state: {name} is active and no node wakes on, reads or leaves it"
               for name, entry in states.items() if entry["kind"] == "active" and name not in covered]
    return errors


def _outcomes(outcomes, effect):
    return {o: {**body, **({"effects": [effect(e) for e in body["effects"]]} if "effects" in body else {})} for o, body in outcomes.items()}


def migrate(nodes, cfg):
    """(workflow, states, commands) from a legacy nodes.yaml and the legacy config's `tracker.stages`."""
    stages, kinds = cfg["tracker"]["stages"], cfg["tracker"].get("stage_kinds", {})
    prefix = os.path.commonprefix(list(stages.values()))
    by_label = {label: name for name, label in stages.items()}
    states = {name: {"kind": kinds.get(name, "waiting"), "store": {"label": label}} for name, label in stages.items()}
    commands = {}

    def command(label, store):
        name = label.removeprefix(prefix)
        if "*" in label or "<next tier>" in label:  # a pattern or a tier step: the declared command it matches
            return name
        if commands.setdefault(name, {"store": {store: label}})["store"] != {store: label}:
            raise ValueError(f"{label} is used both as a ticket label and as a PR label")
        return name

    def effect(e):
        out = {}
        for key, value in e.items():
            if key in ("label", "unlabel"):
                out[RENAMED[key]] = command(value, "pr_label" if e.get("on") == "pr" else "label")
            else:
                out[RENAMED.get(key, key)] = value
        return out

    def wake(trigger):
        event, _, label = trigger.partition(" ")
        if event in FACTS:
            return FACTS[event]
        if event not in LABEL_EVENTS:
            return trigger
        if label in by_label and event == "issues.labeled":
            return f"state {by_label[label]}"
        return f"command {command(label, LABEL_EVENTS[event])}"

    out = {}
    for nname, node in nodes["nodes"].items():
        n = {"type": node["role"] if node["role"] in ("script", "human") else "agent"}
        for key, value in node.items():
            if key == "role" and n["type"] != "agent":
                continue
            elif key == "triggers":
                n["wakes_on"] = [wake(t) for t in value]
            elif key == "when":
                n["when"] = {RENAMED.get(k, k): v for k, v in value.items()}
            elif key == "outcomes":
                n["outcomes"] = _outcomes(value, effect)
            else:
                n[key] = value
        out[nname] = n
    workflow = {"version": 2, **{k: nodes[k] for k in ("environments", "adapters", "schedules") if k in nodes}, "nodes": out}
    return workflow, states, commands


def render(workflow, states, commands):
    """The legacy nodes.yaml graph (triggers, `when`, effects) a v2 workflow stands for."""
    def effect(e):
        return {UNRENAMED.get(k, k): command_label(v, commands) if k in ("command", "uncommand") else v for k, v in e.items()}

    def trigger(wake):
        kind, _, arg = wake.partition(" ")
        if kind == "state":
            return f"issues.labeled {next(iter(states[arg]['store'].values()))}"
        if kind == "command":
            store = next(iter(commands[arg.replace("<next tier>", "<tier>")]["store"]))
            return f"{STORE_EVENTS[store]} {command_label(arg, commands)}"
        return FACT_EVENTS.get(wake, wake)

    out = {}
    for nname, node in workflow["nodes"].items():
        n = {}
        for key, value in node.items():
            if key == "type":
                n["role"] = node.get("role", value)
            elif key == "wakes_on":
                n["triggers"] = [trigger(w) for w in value]
            elif key == "when":
                n["when"] = {UNRENAMED.get(k, k): v for k, v in value.items()}
            elif key == "outcomes":
                n["outcomes"] = _outcomes(value, effect)
            elif key != "role":
                n[key] = value
        out[nname] = n
    return {**{k: v for k, v in workflow.items() if k not in ("version", "nodes")}, "nodes": out}


def main(argv):
    if len(argv) not in (3, 4) or argv[0] != "migrate":
        print(__doc__)
        return 2
    nodes = yaml.safe_load(Path(argv[1]).read_text())
    cfg = config.merge(yaml.safe_load((config.engine_root() / "engine/defaults.yaml").read_text()) or {},
                       yaml.safe_load(Path(argv[2]).read_text()) or {})
    workflow, states, commands = migrate(nodes, cfg)
    out = Path(argv[3] if len(argv) == 4 else ".")
    (out / "workflow.v2.yaml").write_text(yaml.safe_dump(workflow, sort_keys=False, width=120))
    (out / "states-commands.yaml").write_text(yaml.safe_dump({"states": states, "commands": commands}, sort_keys=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

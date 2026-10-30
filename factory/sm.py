#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Delivery state chart as a transition table: validate and simulate.

workflows/delivery.sm.yaml lists states (tracker stage labels), events (each with its source), command
labels, and transitions with guards and effects (docs/statechart.md). An event fires the first transition,
in file order, whose `from`, `event` and `if` match. A command-label event needs its label on the ticket;
the matching transition consumes it. This module only validates and replays; running steps is step.py.
"""
import argparse
import copy
import sys
from pathlib import Path

import yaml

from guards import SMError, compile_expr, evaluate, paths, truthy

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ("issue_label", "pr_label", "pr_synchronize", "pr_review", "pr_closed", "ci", "schedule")
TYPES = {"int": int, "string": str, "bool": bool}
KEYS = {"from", "event", "if", "set", "inc", "labels", "to", "human"}


def type_ok(v, typ):
    return isinstance(v, typ) and not (typ is int and isinstance(v, bool))


def as_list(x):
    return x if isinstance(x, list) else [x]


def validate(table, config=None):
    """Return the table with `commands` expanded, or raise SMError listing every problem."""
    config, errs = config or {}, []
    if not isinstance(table, dict):
        raise SMError("the table must be a YAML mapping")
    states, events, variables = (table.get(k) or {} for k in ("states", "events", "vars"))
    for key, val in (("states", states), ("events", events), ("vars", variables)):
        if not isinstance(val, dict):
            errs.append(f"{key}: must be a mapping")
    if errs:
        raise SMError("\n".join(errs))
    focuses = table.get("focuses") or []
    commands = [c.replace("<focus>", f) for c in table.get("commands") or [] for f in (focuses if "<focus>" in c else [""])]
    stages = (config.get("tracker") or {}).get("stages") or {}
    limits = config.get("limits") or {}
    for name, st in states.items():
        label = (st or {}).get("label")
        if not isinstance(label, str) or (stages and stages.get(name) != label):
            errs.append(f"state {name}: label {label!r} is not the factory.yaml tracker.stages label")
    if table.get("initial") not in states:
        errs.append(f"initial: {table.get('initial')!r} is not a state")
    for name, v in variables.items():
        typ = TYPES.get((v or {}).get("type"))
        if typ is None or not type_ok(v.get("default"), typ):
            errs.append(f"var {name}: needs a type ({', '.join(TYPES)}) and a default of that type")
    for var, cap in (("max_rounds", "fix_rounds"), ("resume_cap", "resume_cap")):
        if cap in limits and variables.get(var, {}).get("default") != limits[cap]:
            errs.append(f"var {var}: default must equal factory.yaml limits.{cap} ({limits[cap]})")
    for name, ev in events.items():
        src = (ev or {}).get("source")
        if src not in SOURCES:
            errs.append(f"event {name}: source must be one of {', '.join(SOURCES)}")
        if src in ("issue_label", "pr_label") and not (ev or {}).get("label"):
            errs.append(f"event {name}: source {src} needs a label")
        if src == "pr_label" and ev.get("label") not in commands:
            errs.append(f"event {name}: label {ev.get('label')!r} is not a declared command label")
    allowed = {(h.get("from"), h.get("to")) for h in table.get("human_transitions") or []}
    transitions, exits = [], set()
    for i, t in enumerate(table.get("transitions") or [], 1):
        unknown = set(t) - KEYS
        where = f"transition {i} ({t.get('from')} --{t.get('event')}-->)"
        if unknown:
            errs.append(f"{where}: unknown key(s) {sorted(unknown)}")
        src, names, to = as_list(t.get("from")), as_list(t.get("event")), t.get("to")
        errs += [f"{where}: from {s!r} is not a state" for s in src if s not in states]
        errs += [f"{where}: event {e!r} is not declared" for e in names if e not in events]
        if to is not None and to not in states:
            errs.append(f"{where}: to {to!r} is not a state")
        if to is not None and to != t["from"]:
            exits.update(s for s in src if s != to)
        if t.get("human"):
            errs += [f"{where}: human transition {s} -> {to} is not in human_transitions" for s in src if (s, to) not in allowed]
        for label in t.get("labels") or []:
            if label not in commands:
                errs.append(f"{where}: label {label!r} is not a declared command label")
        guard, sets = None, {}
        exprs = ([("if", t["if"])] if "if" in t else []) + [(f"set {k}", v) for k, v in (t.get("set") or {}).items()]
        for k in (t.get("set") or {}):
            if k not in variables:
                errs.append(f"{where}: set {k!r} is not a declared var")
        for k in t.get("inc") or []:
            if (variables.get(k) or {}).get("type") != "int":
                errs.append(f"{where}: inc {k!r} is not an int var")
        for what, src_expr in exprs:
            try:
                node = compile_expr(src_expr)
            except SMError as e:
                errs.append(f"{where} {what}: {e}")
                continue
            for p in paths(node):
                if p[0] not in variables and p[0] != "event":
                    errs.append(f"{where} {what}: {'.'.join(p)!r} is not a var or event field")
            guard, sets = (node, sets) if what == "if" else (guard, {**sets, what[4:]: node})
        transitions.append({**t, "from": src, "event": names, "if": guard, "set": sets, "inc": t.get("inc") or [],
                            "labels": t.get("labels") or []})
    for s, st in states.items():
        if not st.get("final") and s not in exits:
            errs.append(f"state {s}: has no exit (a transition to another state)")
    used = {e for t in transitions for e in t["event"]}
    for c in commands:  # one event per command label, and it must have transitions
        found = sorted(e for e in used if (events.get(e) or {}).get("label") == c)
        if len(found) != 1:
            errs.append(f"command label {c}: must have exactly one consuming event with transitions, found {len(found)}")
    if table.get("initial") in states:
        seen, todo = {table["initial"]}, [table["initial"]]
        while todo:
            cur = todo.pop()
            for t in transitions:
                if cur in t["from"] and t.get("to") and t["to"] not in seen:
                    seen.add(t["to"])
                    todo.append(t["to"])
        errs += [f"state {s}: unreachable from {table['initial']}" for s in states if s not in seen]
    if errs:
        raise SMError("\n".join(errs))
    return {**table, "commands": commands, "transitions": transitions}


def new_ticket(m, state=None, vars_=None):
    return {"state": state or m["initial"], "labels": [],
            "vars": {**{k: v["default"] for k, v in m["vars"].items()}, **(vars_ or {})}}


def step(m, ticket, name, payload=None):
    """Apply one event; return (new ticket, note). No match leaves the ticket as it was."""
    if name not in m["events"]:
        raise SMError(f"event {name!r} is not declared")
    t, label = copy.deepcopy(ticket), m["events"][name].get("label")
    if m["events"][name]["source"] == "pr_label" and label not in t["labels"]:
        return t, f"ignored: label {label} is not set"
    ctx = {**t["vars"], "event": payload or {}}
    for tr in m["transitions"]:
        if t["state"] in tr["from"] and name in tr["event"] and (tr["if"] is None or truthy(evaluate(tr["if"], ctx))):
            for key, node in tr["set"].items():
                value = evaluate(node, ctx)
                if not type_ok(value, TYPES[m["vars"][key]["type"]]):
                    raise SMError(f"event {name}: set {key} got {value!r}, not a {m['vars'][key]['type']}")
                t["vars"][key] = value
            for key in tr["inc"]:
                t["vars"][key] += 1
            if label in t["labels"] and m["events"][name]["source"] == "pr_label":
                t["labels"].remove(label)
            t["labels"] += [x for x in tr["labels"] if x not in t["labels"]]
            t["state"] = tr.get("to") or t["state"]
            return t, ", ".join(x for x in (f"+{' +'.join(tr['labels'])}" if tr["labels"] else "",) if x)
    return t, "ignored: no transition matches"


def parse_event(item):
    """`name` or `name=<YAML mapping>`; a mapping {name: payload} is accepted too."""
    if isinstance(item, dict):
        ((name, payload),) = item.items()
    else:
        name, _, text = str(item).partition("=")
        payload = yaml.safe_load(text) if text else {}
    if not isinstance(payload, dict):
        raise SMError(f"event {name}: payload must be a mapping")
    return name, payload


def simulate(m, events, ticket=None):
    """Replay events; return the final ticket and one (event, from, to, note) row per event."""
    ticket, rows = ticket or new_ticket(m), []
    for item in events:
        name, payload = parse_event(item)
        src = ticket["state"]
        ticket, note = step(m, ticket, name, payload)
        rows.append((name, src, ticket["state"], note))
    return ticket, rows


def load_machine(path, config_path):
    config = yaml.safe_load(Path(config_path).read_text()) if Path(config_path).exists() else {}
    return validate(yaml.safe_load(Path(path).read_text()), config)


def main(argv=None):
    p = argparse.ArgumentParser(prog="sm.py", description=__doc__.splitlines()[0])
    p.add_argument("--sm", default=ROOT / "workflows/delivery.sm.yaml", help="transition table")
    p.add_argument("--config", default=ROOT / "factory.yaml", help="factory.yaml for stages and limits")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate").add_argument("machine", nargs="?")
    sim = sub.add_parser("simulate", help="replay events from the initial state, or from --state")
    sim.add_argument("--state")
    sim.add_argument("events", nargs="*", help="name or name=<YAML mapping payload>")
    args = p.parse_args(argv)
    path = getattr(args, "machine", None) or args.sm
    try:
        m = load_machine(path, args.config)
        if args.cmd == "validate":
            print(f"ok: {path} ({len(m['states'])} states, {len(m['transitions'])} transitions)")
        else:
            t, rows = simulate(m, args.events, new_ticket(m, args.state))
            for name, src, dst, note in rows:
                print(f"{name:<26} {src} -> {dst}  {note}")
            print(f"final: {t['state']} labels={t['labels']}")
    except (SMError, OSError, yaml.YAMLError) as e:
        print(f"sm.py: error: {path}:\n{e}" if args.cmd == "validate" else f"sm.py: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Delivery state machine engine: validate, next, simulate.

A ticket sits in a state (a tracker stage). An event fires the first transition,
in file order, whose `from`, `on` and `if` match. The transition applies `loop`
and `set`, moves to `to` (default: stay) and starts `do`. Internal events are
`<action>.<result>`; their payload is the action's handoff, kept in
`out.<action>`. Actions run only in agent states; in human and external states
the engine waits for an external event. A transition that enters an agent state
without starting an action is followed, in the same step, by the built-in event
`enter` when a transition from that state matches it (a pass-through state).
See workflows/delivery.sm.yaml.
"""
import argparse
import copy
import json
import re
import sys
from collections.abc import Hashable
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ACTORS = ("agent", "human", "external")
JOINS = ("all", "any", "first")
TYPES = {"int": int, "string": str, "list": list, "bool": bool, "map": dict}
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
TRANSITION_KEYS = {"from", "on", "if", "do", "set", "to", "loop"}
MISSING = object()
ENTER = "enter"  # built-in event: fired once when a transition enters an agent state with nothing to run


class SMError(Exception):
    pass


def type_ok(value, typ):
    return isinstance(value, typ) and not (typ is int and isinstance(value, bool))


# ---- Guards: a JSONata subset, so they compile to Air gateways unchanged ----

SUBSET = "comparisons (= != < <= > >=), and, or, $not(), field paths, $count(), $append(a, b), literals"
FUNCS = {"$count": 1, "$not": 1, "$append": 2}
TOKEN = re.compile(
    r"""(?:(?P<num>-?\d+(?:\.\d+)?)|(?P<str>"[^"]*"|'[^']*')|(?P<op>!=|<=|>=|=|<|>)"""
    r"""|(?P<fn>\$[A-Za-z_]\w*)|(?P<name>[A-Za-z_]\w*)|(?P<punct>[().,]))"""
)


class Parser:
    def __init__(self, src):
        self.src, self.toks, self.i, pos = src, [], 0, 0
        while True:
            while pos < len(src) and src[pos].isspace():
                pos += 1
            if pos == len(src):
                break
            m = TOKEN.match(src, pos)
            if not m:
                self.toks, self.i = [(None, None, pos)], 0
                self.fail(f"unsupported character {src[pos]!r}")
            self.toks.append((m.lastgroup, m.group(m.lastgroup), m.start(m.lastgroup)))
            pos = m.end()

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None, len(self.src))

    def fail(self, msg):
        col = self.peek()[2] + 1
        raise SMError(f"guard {self.src!r}: {msg} at column {col}; the guard subset is {SUBSET}")

    def take(self, kind, val=None):
        tok = self.peek()
        if tok[0] != kind or (val is not None and tok[1] != val):
            self.fail(f"expected {val or kind}")
        self.i += 1
        return tok[1]

    def parse(self):
        node = self.binary("or")
        if self.peek()[0]:
            self.fail(f"unexpected {self.peek()[1]!r}")
        return node

    def binary(self, op):
        node = self.binary("and") if op == "or" else self.compare()
        while self.peek()[:2] == ("name", op):
            self.i += 1
            node = (op, node, self.binary("and") if op == "or" else self.compare())
        return node

    def compare(self):
        node = self.primary()
        if self.peek()[0] == "op":
            node = ("cmp", self.take("op"), node, self.primary())
        return node

    def primary(self):
        kind, val, _ = self.peek()
        if kind == "num":
            self.i += 1
            return ("lit", float(val) if "." in val else int(val))
        if kind == "str":
            self.i += 1
            return ("lit", val[1:-1])
        if kind == "fn":
            if val not in FUNCS:
                self.fail(f"function {val} is outside the subset")
            self.i += 1
            self.take("punct", "(")
            args = [self.binary("or")]
            for _ in range(FUNCS[val] - 1):
                self.take("punct", ",")
                args.append(self.binary("or"))
            self.take("punct", ")")
            return (val, *args)
        if (kind, val) == ("punct", "("):
            self.i += 1
            node = self.binary("or")
            self.take("punct", ")")
            return node
        if kind == "name":
            if val in ("true", "false", "null"):
                self.i += 1
                return ("lit", {"true": True, "false": False, "null": None}[val])
            if val in ("and", "or", "not", "in"):
                self.fail(f"{val!r} is not a value" + ("; use $not(x)" if val == "not" else ""))
            self.i += 1
            path = [val]
            while self.peek()[:2] == ("punct", "."):
                self.i += 1
                path.append(self.take("name"))
            return ("path", tuple(path))
        self.fail(f"unexpected {val!r}" if kind else "unexpected end")


def compile_expr(src):
    # Non-string YAML values (numbers, lists, booleans) in `set:` are literals.
    return Parser(src).parse() if isinstance(src, str) else ("lit", src)


def paths(node):
    if node[0] == "path":
        yield node[1]
    for child in node[1:]:
        if isinstance(child, tuple) and child and isinstance(child[0], str):
            yield from paths(child)


def truthy(v):
    return v is not MISSING and bool(v)


def evaluate(node, ctx):
    tag = node[0]
    if tag == "lit":
        return node[1]
    if tag == "path":
        v = ctx
        for key in node[1]:
            v = v.get(key, MISSING) if isinstance(v, dict) else MISSING
        return v
    if tag == "$count":
        v = evaluate(node[1], ctx)
        return 0 if v is MISSING else len(v) if isinstance(v, list) else 1
    if tag == "$append":  # JSONata: an undefined side yields the other, a scalar is a one-item list
        a, b = (evaluate(x, ctx) for x in node[1:])
        if a is MISSING or b is MISSING:
            return a if b is MISSING else b
        return (a if isinstance(a, list) else [a]) + (b if isinstance(b, list) else [b])
    if tag == "$not":
        return not truthy(evaluate(node[1], ctx))
    if tag == "and":
        return truthy(evaluate(node[1], ctx)) and truthy(evaluate(node[2], ctx))
    if tag == "or":
        return truthy(evaluate(node[1], ctx)) or truthy(evaluate(node[2], ctx))
    op, a, b = node[1], evaluate(node[2], ctx), evaluate(node[3], ctx)
    if a is MISSING or b is MISSING:  # JSONata: comparing undefined is false
        return False
    if op in ("=", "!="):
        return (a == b) == (op == "=")
    if not (isinstance(a, (int, float)) and isinstance(b, (int, float))) and not (
        isinstance(a, str) and isinstance(b, str)
    ):
        raise SMError(f"cannot compare {a!r} {op} {b!r}")
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


# ---- Validation ----


def lookup(config, dotted):
    v = config
    for key in dotted.split("."):
        v = v.get(key) if isinstance(v, dict) else None
    return v


def has(coll, x):
    # Malformed YAML can put lists or mappings where a name belongs; they are never members.
    return isinstance(x, Hashable) and x in coll


def mapping(v):
    return v if isinstance(v, dict) else {}


def validate(sm, config=None):
    """Return the normalised machine, or raise SMError listing every problem."""
    if not isinstance(sm, dict):
        raise SMError("the machine must be a YAML mapping")
    errs, config = [], config or {}
    for key in ("states", "vars", "actions"):
        if sm.get(key) is not None and not isinstance(sm[key], dict):
            errs.append(f"{key}: must be a mapping")
    for key in ("transitions", "events"):
        if sm.get(key) is not None and not isinstance(sm[key], list):
            errs.append(f"{key}: must be a list")
    if errs:
        raise SMError("\n".join(errs))
    states = sm.get("states") or {}
    stages = lookup(config, "tracker.stages")
    for name, st in states.items():
        if not has(ACTORS, mapping(st).get("actor")):
            errs.append(f"state {name}: actor must be one of {', '.join(ACTORS)}")
        if stages and name not in stages:
            errs.append(f"state {name}: not a tracker stage in factory.yaml")
    if not states:
        errs.append("states: at least one state is required")
    initial = sm.get("initial")
    if not has(states, initial):
        errs.append(f"initial: {initial!r} is not a state")
    variables = sm.get("vars") or {}
    for name, v in variables.items():
        v = mapping(v)
        typ = TYPES.get(v.get("type")) if has(TYPES, v.get("type")) else None
        if not IDENT.match(str(name)) or name in ("out", "event"):
            errs.append(f"var {name}: not a valid name")
        if typ is None:
            errs.append(f"var {name}: type must be one of {', '.join(TYPES)}")
        elif not type_ok(v.get("default"), typ):
            errs.append(f"var {name}: default {v.get('default')!r} is not a {v['type']}")
    actions = sm.get("actions") or {}
    for name, a in actions.items():
        retry = mapping(a).get("retry", 0)
        if not IDENT.match(str(name)):
            errs.append(f"action {name}: not a valid name")
        if not type_ok(retry, int) or retry < 0:
            errs.append(f"action {name}: retry must be a non-negative integer")
    events = set()
    for ev in sm.get("events") or []:
        if isinstance(ev, str):
            events.add(ev)
        else:
            errs.append(f"events: {ev!r} is not an event name")
    raw = sm.get("transitions") or []
    forks = {t["do"]["name"] for t in raw if isinstance(t, dict) and isinstance(t.get("do"), dict)
             and isinstance(t["do"].get("name"), str)}

    def check_expr(src, where):
        try:
            node = compile_expr(src)
        except SMError as e:
            errs.append(f"{where}: {e}")
            return None
        for path in paths(node):
            if path[0] not in variables and path[0] not in ("out", "event"):
                errs.append(f"{where}: {'.'.join(path)!r} is not a var, out.<action> or event field")
            elif path[0] == "out" and (len(path) < 2 or (path[1] not in actions and path[1] not in forks)):
                errs.append(f"{where}: {'.'.join(path)!r} does not name an action")
        return node

    transitions, edges = [], {s: set() for s in states}
    for i, t in enumerate(raw, 1):
        if not isinstance(t, dict):
            errs.append(f"transition {i}: must be a mapping with from, on and to or do, got {t!r}")
            continue
        t = dict(t)
        if True in t:  # YAML 1.1 reads a bare `on` key as boolean true
            t["on"] = t.pop(True)
        src = t.get("from")
        src = src if isinstance(src, list) else [src]
        where = f"transition {i} ({', '.join(map(str, src))} --{t.get('on')}-->)"
        for key in set(t) - TRANSITION_KEYS:
            errs.append(f"{where}: unknown key {key!r}")
        for s in src:
            if not has(states, s):
                errs.append(f"{where}: from {s!r} is not a state")
        src = [s for s in src if has(states, s)]
        to = t.get("to")
        if to is not None and not has(states, to):
            errs.append(f"{where}: to {to!r} is not a state")
            to = None
        on = t.get("on")
        action = str(on).rpartition(".")[0]
        if not isinstance(on, str) or not (action in actions or action in forks or on in events or on == ENTER):
            errs.append(f"{where}: on must be <action>.<result> or a declared external event")
        guard = check_expr(t["if"], f"{where} if") if "if" in t else None
        sets = {}
        if t.get("set") is not None and not isinstance(t["set"], dict):
            errs.append(f"{where}: set must be a mapping of var: expression")
        for key, val in mapping(t.get("set")).items():
            if key not in variables:
                errs.append(f"{where}: set {key!r} is not a declared var")
            sets[key] = check_expr(val, f"{where} set {key}")
        do = t.get("do")
        if isinstance(do, dict):
            branches = do.get("parallel")
            if not isinstance(branches, list) or not branches or any(not has(actions, b) for b in branches):
                errs.append(f"{where}: do.parallel must be a non-empty list of declared actions")
            if do.get("join") not in JOINS:
                errs.append(f"{where}: do.join must be one of {', '.join(JOINS)}")
            if not IDENT.match(str(do.get("name"))) or has(actions, do.get("name")):
                errs.append(f"{where}: do.name must name the fork and differ from every action")
        elif do is not None and not has(actions, do):
            errs.append(f"{where}: do {do!r} is not a declared action")
        targets = [to] if to else src
        if do is not None and any(mapping(states[s]).get("actor") != "agent" for s in targets):
            errs.append(f"{where}: do runs only when the target state's actor is agent")
        loop = t.get("loop")
        if loop is not None:
            loop = dict(loop) if isinstance(loop, dict) else {}
            counter = mapping(variables.get(loop["counter"])) if has(variables, loop.get("counter")) else {}
            if counter.get("type") != "int":
                errs.append(f"{where}: loop.counter must be a declared int var")
            if "max" not in loop:
                errs.append(f"{where}: loop needs max (an integer or a factory.yaml path like limits.fix_rounds)")
            else:
                limit = lookup(config, loop["max"]) if isinstance(loop["max"], str) else loop["max"]
                if not type_ok(limit, int) or limit < 1:
                    errs.append(f"{where}: loop.max {loop['max']!r} does not resolve to a positive integer")
                loop["max"] = limit
            if not has(states, loop.get("on_exhausted")):
                errs.append(f"{where}: loop.on_exhausted must be a state")
        for s in src:
            if s in edges:
                edges[s].update(x for x in targets + [(loop or {}).get("on_exhausted")] if has(states, x))
        transitions.append({"from": src, "on": on, "if": guard, "set": sets, "do": do, "to": to, "loop": loop})
    if initial in states:
        seen, todo = {initial}, [initial]
        while todo:
            for nxt in edges[todo.pop()] - seen:
                seen.add(nxt)
                todo.append(nxt)
        errs += [f"state {s}: unreachable from {initial}" for s in states if s not in seen]
    if errs:
        raise SMError("\n".join(errs))
    return {"initial": initial, "states": states, "vars": variables, "actions": actions,
            "forks": forks, "transitions": transitions}


# ---- Engine ----


RUN_KEYS = ("out", "pending", "retries", "fork")


def new_ticket(m, base=None):
    """A ticket from defaults, then the run state stored under the vars' `_run` key (see
    `persisted`), then the other keys of `base`."""
    base = copy.deepcopy(base or {})
    vars_ = base.get("vars") or {}
    t = {"state": m["initial"], "vars": {k: copy.deepcopy(v["default"]) for k, v in m["vars"].items()},
         "out": {}, "pending": [], "retries": {}, "fork": None}
    t.update({k: v for k, v in (vars_.pop("_run", None) or {}).items() if k in RUN_KEYS})
    t.update({k: v for k, v in base.items() if k != "vars"})
    t["vars"].update(vars_)
    if t["state"] not in m["states"]:
        raise SMError(f"ticket state {t['state']!r} is not a state")
    return t


def persisted(t):
    """The vars plus the running actions and their outputs under `_run`, so a stopped run resumes."""
    return {**t["vars"], "_run": {k: t[k] for k in RUN_KEYS}}


def join(fork, branch, result):
    """Record a branch result; return the fork's result once the join decides, else None."""
    fork["results"][branch] = result
    results = list(fork["results"].values())
    if fork["join"] == "first":
        return result
    if fork["join"] == "any" and result == "done":
        return "done"
    if None in results:
        return None
    return "done" if all(r == "done" for r in results) else "failed"


def report(m, t, src, event, note=None):
    actor = m["states"][t["state"]]["actor"]
    return {"event": event, "from": src, "to": t["state"], "run": list(t["pending"]),
            "wait": None if actor == "agent" else actor, "note": note}


def step(m, ticket, event, payload=None):
    """Apply one event; return (new ticket, report). Raises SMError on a workflow error."""
    t, payload, src = copy.deepcopy(ticket), payload or {}, ticket["state"]
    action, _, result = event.rpartition(".")
    internal = action in m["actions"] or action in m["forks"]
    if internal:
        if action not in t["pending"]:
            raise SMError(f"event {event}: action {action} is not running (running: {t['pending'] or 'none'})")
        retry, tries = m["actions"].get(action, {}).get("retry", 0), t["retries"].get(action, 0)
        if result == "error" and tries < retry:
            t["retries"][action] = tries + 1
            return t, report(m, t, src, event, f"retry {action} {tries + 1}/{retry}")
        t["retries"].pop(action, None)
        t["out"][action] = {"result": result, **payload}
        t["pending"].remove(action)
        if t["fork"]:
            fork = t["fork"]
            joined = join(fork, action, result)
            if joined is None:
                return t, report(m, t, src, event, f"fork {fork['name']} waits for {', '.join(t['pending'])}")
            # Unfinished branches of an `any`/`first` join are dropped; the runner cancels them.
            event = f"{fork['name']}.{joined}"
            t["out"][fork["name"]] = {"result": joined, "branches": fork["results"]}
            t["pending"], t["fork"] = [], None
    ctx = {**t["vars"], "out": t["out"], "event": payload}
    for tr in m["transitions"]:
        if src in tr["from"] and tr["on"] == event and (tr["if"] is None or truthy(evaluate(tr["if"], ctx))):
            t, rep = fire(m, t, tr, ctx, src, event)
            mid = t["state"]
            if event != ENTER and not t["pending"] and m["states"][mid]["actor"] == "agent":
                t, after = step(m, t, ENTER)
                if not (after["note"] or "").startswith("ignored"):  # a transition matched: report the pass-through
                    rep = report(m, t, src, event, f"through {mid}")
            return t, rep
    if internal:
        raise SMError(f"event {event} in state {src}: no transition matches")
    return t, report(m, t, src, event, "ignored: no transition matches")


def fire(m, t, tr, ctx, src, event):
    loop = tr["loop"]
    if loop:
        counter = loop["counter"]
        if t["vars"][counter] >= loop["max"]:
            t["state"], t["pending"], t["fork"] = loop["on_exhausted"], [], None
            return t, report(m, t, src, event, f"{counter} exhausted at {loop['max']}")
        t["vars"][counter] += 1
    for key, node in tr["set"].items():
        value, typ = evaluate(node, ctx), m["vars"][key]["type"]
        if value is MISSING or not type_ok(value, TYPES[typ]):
            raise SMError(f"event {event}: set {key} got {'nothing' if value is MISSING else repr(value)}, not a {typ}")
        t["vars"][key] = value
    t["state"], do = tr["to"] or src, tr["do"]
    t["pending"], t["fork"] = [], None
    if isinstance(do, dict):
        t["pending"] = list(do["parallel"])
        t["fork"] = {"name": do["name"], "join": do["join"], "results": {b: None for b in do["parallel"]}}
    elif do:
        t["pending"] = [do]
    return t, report(m, t, src, event)


def parse_event(item):
    """`name`, `name=<YAML mapping>` or a one-key mapping {name: payload}."""
    if isinstance(item, dict):
        ((name, payload),) = item.items()
    else:
        name, _, text = str(item).partition("=")
        payload = yaml.safe_load(text) if text else {}
    if not isinstance(payload, dict):
        raise SMError(f"event {name}: payload must be a mapping")
    return name, payload


def simulate(m, ticket, events):
    trace = []
    for item in events:
        ticket, rep = step(m, ticket, *parse_event(item))
        trace.append(rep)
    return ticket, trace


def load_machine(path, config_path):
    config = yaml.safe_load(Path(config_path).read_text()) if Path(config_path).exists() else {}
    return validate(yaml.safe_load(Path(path).read_text()), config)


def main(argv=None):
    p = argparse.ArgumentParser(prog="sm.py", description=__doc__.splitlines()[0])
    p.add_argument("--sm", default=ROOT / "workflows/delivery.sm.yaml", help="machine file")
    p.add_argument("--config", default=ROOT / "factory.yaml", help="factory.yaml for stages and limits")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate").add_argument("machine", nargs="?")
    nxt = sub.add_parser("next", help="apply one event to a ticket JSON/YAML file")
    nxt.add_argument("ticket")
    nxt.add_argument("event", help="name or name=<YAML mapping payload>")
    sim = sub.add_parser("simulate", help="run a fixture's events, then the given ones")
    sim.add_argument("fixture")
    sim.add_argument("events", nargs="*")
    args = p.parse_args(argv)
    path = getattr(args, "machine", None) or args.sm
    try:
        m = load_machine(path, args.config)
        if args.cmd == "validate":
            print(f"ok: {path} ({len(m['states'])} states, {len(m['transitions'])} transitions)")
        elif args.cmd == "next":
            t, rep = step(m, new_ticket(m, yaml.safe_load(Path(args.ticket).read_text())), *parse_event(args.event))
            print(json.dumps({"ticket": t, "step": rep}, indent=2))
        else:
            fixture = yaml.safe_load(Path(args.fixture).read_text()) or {}
            t, trace = simulate(m, new_ticket(m, fixture.get("ticket")), (fixture.get("events") or []) + args.events)
            for r in trace:
                extra = [f"run {', '.join(r['run'])}" if r["run"] else "", f"wait {r['wait']}" if r["wait"] else "",
                         r["note"] or ""]
                print(f"{r['event']:<22} {r['from']} -> {r['to']}  " + "  ".join(x for x in extra if x))
            print(f"final: {t['state']} {json.dumps(t['vars'])}")
    except (SMError, OSError, yaml.YAMLError) as e:
        print(f"sm.py: error: {path}:\n{e}" if args.cmd == "validate" else f"sm.py: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

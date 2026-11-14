"""The normalized workflow graph, from the compiler's node graph (`nodes.load()`) and the tracker's stages.

Only what the compiler establishes is a connection:
- a `transition` edge: an outcome with a stage effect, from its `from` stages, else the node's `when.stage`
  (`source` None when neither names one: the engine does not say);
- a `route` edge: an outcome's `label` effect that a node's `issues.labeled <label>` or `pull_request.labeled <label>`
  trigger names (a `<tier>`-style placeholder matches any filler);
- a `wake` edge: a trigger on a stage label, from that stage to the node.
An outcome that only comments, reviews or notifies makes no edge. Loops are the stage cycles of the transition edges;
branches are a stage with several transitions to different stages, and outcomes with an `else`.
"""
import re

PLACEHOLDER = re.compile(r"<[^<>]+>")
LABEL_EVENTS = ("issues.labeled", "pull_request.labeled")


def _list(x):
    return [] if x is None else list(x) if isinstance(x, (list, tuple)) else [x]


def _label_pattern(label):
    return re.compile("^" + "(.+)".join(re.escape(p) for p in PLACEHOLDER.split(label)) + "$")


def _trigger(raw, stage_of_label, schedules):
    event, _, arg = str(raw).partition(" ")
    kind = "schedule" if event == "schedule" else "webhook" if event == "webhook" else "manual" if event == "manual" else "event"
    out = {"raw": str(raw), "kind": kind, "event": event, "label": arg or None if event in LABEL_EVENTS else None,
           "stage": stage_of_label.get(arg) if event in LABEL_EVENTS else None}
    if kind == "schedule":
        out["cron"] = (schedules.get(arg) or {}).get("cron")
    return out


def _outcome(name, o, node_stages):
    o = o or {}
    effects = [{"kind": next((k for k in e if k not in ("on",)), None), "value": e.get(next((k for k in e if k != "on"), None)),
                "on": e.get("on")} for e in o.get("effects") or [] if isinstance(e, dict)]
    stage = next((e["value"] for e in effects if e["kind"] in ("stage", "state")), None)
    comment_only = bool(effects) and all(e["kind"] in ("comment", "review") for e in effects)
    return {"name": name, "from": _list(o.get("from")) or None, "to": stage, "else": o.get("else"),
            "condition": {k: o[k] for k in ("if", "ci") if k in o} or None, "enabled": o.get("enabled", True) is not False,
            "effects": effects, "comment_only": comment_only, "source_stages": _list(o.get("from")) or node_stages}


def _cycles(edges):
    """The strongly connected sets of stages (more than one stage, or one with a self transition), Tarjan."""
    graph = {}
    for e in edges:
        graph.setdefault(e["source"], set()).add(e["target"])
        graph.setdefault(e["target"], set())
    index, low, stack, on, found, counter = {}, {}, [], set(), [], [0]

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in sorted(graph[v]):
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            part = []
            while True:
                w = stack.pop()
                on.discard(w)
                part.append(w)
                if w == v:
                    break
            if len(part) > 1 or v in graph[v]:
                found.append(sorted(part))
    for v in sorted(graph):
        if v not in index:
            visit(v)
    return sorted(found)


def build(nodes, config):
    """The graph of compiled `nodes` ({nodes, schedules, environments}) and `config` (its `tracker.stages` and
    `tracker.stage_kinds`): {stages, nodes, edges, loops, branches}. Pure: it reads nothing else."""
    tracker = config.get("tracker") or {}
    stage_attr, kinds = tracker.get("stages") or {}, tracker.get("stage_kinds") or {}
    by_label = {v: k for k, v in stage_attr.items()}
    schedules = nodes.get("schedules") or {}
    out_nodes, edges = [], []
    for name, n in (nodes.get("nodes") or {}).items():
        when = n.get("when") or {}
        in_stages = _list(when.get("stage"))
        triggers = [_trigger(t, by_label, schedules) for t in n.get("triggers") or []]
        background = bool(triggers) and all(t["kind"] in ("schedule", "webhook") for t in triggers)
        outcomes = [_outcome(o, oc, in_stages) for o, oc in (n.get("outcomes") or {}).items()]
        out_nodes.append({"id": name, "role": n.get("role"), "tier": n.get("tier") or _list(n.get("tiers")) or None,
                          "environment": n.get("env"), "enabled": n.get("enabled", True) is not False,
                          "output": n.get("output"), "when": when or None, "stages": in_stages,
                          "triggers": triggers, "background": background or bool(n.get("routine")),
                          "outcomes": outcomes})
        for t in triggers:
            if t["stage"]:
                edges.append({"kind": "wake", "source": t["stage"], "target": name, "node": name, "outcome": None})
        for o in outcomes:
            if o["to"]:
                for src in o["source_stages"] or [None]:
                    edges.append({"kind": "transition", "source": src, "target": o["to"], "node": name,
                                  "outcome": o["name"], "condition": o["condition"]})
    patterns = [(_label_pattern(t["label"]), n["id"]) for n in out_nodes for t in n["triggers"] if t["label"]]
    for n in out_nodes:
        for o in n["outcomes"]:
            for e in o["effects"]:
                if e["kind"] == "label" and isinstance(e["value"], str):
                    edges += [{"kind": "route", "source": n["id"], "target": target, "node": n["id"],
                               "outcome": o["name"], "via": e["value"]}
                              for pattern, target in patterns if pattern.match(PLACEHOLDER.sub("x", e["value"]))
                              or pattern.match(e["value"])]
    transitions = [e for e in edges if e["kind"] == "transition" and e["source"]]
    loops = _cycles(transitions)
    in_loop = [set(c) for c in loops]
    for e in transitions:
        e["in_loop"] = any(e["source"] in c and e["target"] in c for c in in_loop)
    by_source = {}
    for e in transitions:
        by_source.setdefault(e["source"], []).append(e)
    branches = [{"kind": "stage", "stage": s, "targets": sorted({e["target"] for e in es}),
                 "via": [{"node": e["node"], "outcome": e["outcome"], "to": e["target"]} for e in es]}
                for s, es in sorted(by_source.items()) if len({e["target"] for e in es}) > 1]
    branches += [{"kind": "else", "node": n["id"], "outcome": o["name"], "else": o["else"]}
                 for n in out_nodes for o in n["outcomes"] if o["else"]]
    used = {e["source"] for e in edges if e["kind"] in ("transition", "wake") and e["source"]} | {e["target"] for e in edges if e["kind"] == "transition"}
    used |= {s for n in out_nodes for s in n["stages"]}
    names = list(dict.fromkeys([*stage_attr, *sorted(used - set(stage_attr) - {"none"})]))
    return {"stages": [{"id": s, "kind": kinds.get(s), "label": stage_attr.get(s)} for s in names],
            "nodes": out_nodes, "edges": edges, "loops": loops, "branches": branches}

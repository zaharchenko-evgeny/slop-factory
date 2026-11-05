#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The delivery state chart of a v2 factory team (ADR-0016), generated from its files and checked.

Usage:
  state_chart.py verify TEAM_DIR   check the chart in workflows/delivery.md is current, and the graph is sound
  state_chart.py write TEAM_DIR    rewrite the chart between the markers of workflows/delivery.md
  state_chart.py print TEAM_DIR    print the generated block (Mermaid chart and node table)

TEAM_DIR holds factory.project.yaml (states, with kind active|waiting|final, and a store) and the workflow
it names (workflows/delivery.yaml: nodes with when, wakes_on, outcomes and effects). Each state-changing
outcome of an enabled node becomes one arrow from each of its source states (`from`, else the node's
`when.state`, else `any`). A state that only disabled nodes lead to is drawn as off and not checked.
A state with a `pr` store is a fact of the pull request: a node wakes on it, so it may start a path.

Verify errors, each starting with a name: chart-missing, chart-stale, unknown-state, no-start-state,
unreachable-state, dead-end-state. Exit 0 when there are none, 1 otherwise, 2 on usage.
"""
import re
import sys
from pathlib import Path

import yaml

BEGIN = "<!-- state-chart:begin -->"
END = "<!-- state-chart:end -->"
ANY = "any"


def load(team):
    team = Path(team)
    project = yaml.safe_load((team / "factory.project.yaml").read_text()) or {}
    workflow_path = team / (project.get("workflow") or "workflows/delivery.yaml")
    workflow = yaml.safe_load(workflow_path.read_text()) or {}
    return project, workflow, team / "workflows" / "delivery.md"


def _list(value):
    if value in (None, "none"):
        return []
    return [value] if isinstance(value, str) else [v for v in value if v != "none"]


def _is_pr_fact(entry):
    return isinstance(entry, dict) and isinstance(entry.get("store"), dict) and "pr" in entry["store"]


def graph(project, workflow):
    """(states, edges, off, errors). An edge is a dict with src, dst, label and node. `off` lists the states
    that only disabled outcomes lead to."""
    states = project.get("states") or {}
    edges, errors, disabled_targets = [], [], set()
    for node_name, node in (workflow.get("nodes") or {}).items():
        node = node if isinstance(node, dict) else {}
        node_off = node.get("enabled") is False
        when_states = _list((node.get("when") or {}).get("state"))
        for outcome_name, outcome in (node.get("outcomes") or {}).items():
            outcome = outcome if isinstance(outcome, dict) else {}
            effects = [e for e in outcome.get("effects") or [] if isinstance(e, dict)]
            targets = [e["state"] for e in effects if "state" in e]
            if not targets:
                continue
            if node_off or outcome.get("enabled") is False:
                disabled_targets.update(targets)
                continue
            sources = _list(outcome.get("from")) or when_states or [ANY]
            extras = [f"+{e['command']}" for e in effects if "command" in e]
            extras += [f"-{e['uncommand']}" for e in effects if "uncommand" in e]
            label = f"{node_name} · {outcome_name}" + (" / " + ", ".join(extras) if extras else "")
            for src in sources:
                for dst in targets:
                    for name in (src, dst):
                        if name != ANY and name not in states:
                            errors.append(f"unknown-state: {node_name}.{outcome_name}: {name}")
                    edges.append({"src": src, "dst": dst, "label": label, "node": node_name})
    enabled_targets = {e["dst"] for e in edges}
    off = {s for s in disabled_targets if s not in enabled_targets and s in states}
    edges = [e for e in edges if e["src"] not in off]
    return states, edges, off, errors


def _id(name):
    return "s_" + re.sub(r"\W", "_", name)


def _text(s):
    return s.replace(":", " ").replace("<", "{").replace(">", "}")


def mermaid(states, edges, off=frozenset()):
    lines = ["stateDiagram-v2",
             "    classDef waiting fill:#fff8e6,stroke:#c90",
             "    classDef final fill:#eef7ee,stroke:#393",
             "    classDef off fill:#f4f4f4,stroke:#999,stroke-dasharray:5 5,color:#777"]
    targets = {e["dst"] for e in edges}
    sources = {e["src"] for e in edges}
    for name in states:
        lines.append(f'    state "{name}" as {_id(name)}')
    if ANY in sources:
        lines.append(f'    state "any state" as {_id(ANY)}')
    for name in states:
        if name not in targets and name not in off:
            lines.append(f"    [*] --> {_id(name)}")
    seen = set()
    for e in edges:
        key = (e["src"], e["dst"], e["label"])
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"    {_id(e['src'])} --> {_id(e['dst'])} : {_text(e['label'])}")
    for name, entry in states.items():
        if (entry or {}).get("kind") == "final" and name not in sources and name not in off:
            lines.append(f"    {_id(name)} --> [*]")
    for css in ("waiting", "final", "off"):
        names = [_id(n) for n, e in states.items() if (
            (n in off) if css == "off" else (n not in off and (e or {}).get("kind") == css))]
        if names:
            lines.append(f"    class {','.join(names)} {css}")
    return "\n".join(lines)


def node_table(workflow):
    rows = ["| Node | Wakes on | Role | Environment |", "|---|---|---|---|"]
    for name, node in (workflow.get("nodes") or {}).items():
        node = node if isinstance(node, dict) else {}
        wakes = "<br>".join(f"`{w}`" for w in node.get("wakes_on") or []) or "-"
        label = name + (" (off)" if node.get("enabled") is False else "")
        rows.append(f"| `{label}` | {wakes} | {node.get('role', '-')} | {node.get('env', '-')} |")
    return "\n".join(rows)


def block(project, workflow):
    """The generated text between the markers, without the markers."""
    states, edges, off, _ = graph(project, workflow)
    return "```mermaid\n" + mermaid(states, edges, off) + "\n```\n\n" + node_table(workflow) + "\n"


def soundness(project, workflow):
    """Errors of the graph: unknown states, a start state, unreachable states, dead ends."""
    states, edges, off, errors = graph(project, workflow)
    if not states:
        return errors
    targets = {e["dst"] for e in edges}
    external = {n for n, e in states.items() if _is_pr_fact(e)}
    starts = [n for n in states if (n not in targets or n in external) and n not in off]
    if not starts:
        errors.append("no-start-state: every state has an incoming arrow")
    succ = {}
    for e in edges:
        succ.setdefault(e["src"], set()).add(e["dst"])
    reached, frontier = set(), list(starts) + ([ANY] if ANY in succ else [])
    while frontier:
        cur = frontier.pop()
        if cur in reached:
            continue
        reached.add(cur)
        frontier.extend(succ.get(cur, ()))
    for name, entry in states.items():
        if name in off:
            continue
        kind = (entry or {}).get("kind")
        if name not in reached:
            errors.append(f"unreachable-state: {name} ({kind}): no arrow leads to it from a start state")
        if kind == "active" and name not in external and name not in succ and ANY not in succ:
            errors.append(f"dead-end-state: {name}: an active state with no arrow out; a ticket there never moves")
    return errors


def check(team):
    project, workflow, doc = load(team)
    errors = soundness(project, workflow)
    text = doc.read_text() if doc.is_file() else ""
    if BEGIN not in text or END not in text:
        errors.append(f"chart-missing: {doc} has no {BEGIN} ... {END} markers")
    else:
        current = text[text.index(BEGIN) + len(BEGIN):text.index(END)]
        if current.strip() != block(project, workflow).strip():
            errors.append(f"chart-stale: {doc} does not match the workflow; run state_chart.py write")
    return errors


def write(team):
    project, workflow, doc = load(team)
    text = doc.read_text()
    if BEGIN not in text or END not in text:
        raise SystemExit(f"chart-missing: add {BEGIN} and {END} to {doc}")
    head, rest = text.split(BEGIN, 1)
    _, tail = rest.split(END, 1)
    doc.write_text(head + BEGIN + "\n" + block(project, workflow) + END + tail)


def main(argv):
    if len(argv) != 2 or argv[0] not in ("verify", "write", "print"):
        print(__doc__)
        return 2
    cmd, team = argv
    if cmd == "print":
        print(block(*load(team)[:2]), end="")
        return 0
    if cmd == "write":
        write(team)
        print(f"state chart written: {load(team)[2]}")
        return 0
    errors = check(team)
    print("\n".join(errors) or "state chart: ok")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

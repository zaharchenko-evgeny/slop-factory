"""The stage statechart of a factory's nodes (ADR-0014): a Mermaid flowchart and a node table, derived from the
nodes, so they cannot drift from the factory. `block` is the generated part of `docs/statechart*.md`.

Usage: factory chart [nodes.yaml] | nodes.py chart [nodes.yaml]
"""
import config
import nodes as nodes_mod
import vocab


def planned(x):
    return isinstance(x, dict) and x.get("enabled") is False


def trigger_text(t, n, schedules):
    """A trigger in a reader's words: `issue +factory:ready`, `PR +factory:fix-{cheap|mid}`, `PR merged`."""
    tiers = "|".join(n.get("tiers") or ["tier"])
    t = t.replace("<tier>", "{" + tiers + "}")
    event, _, label = t.partition(" ")
    names = {"issues.labeled": "issue +", "pull_request.labeled": "PR +", "pull_request.merged": "PR merged", "issues.closed": "issue closed", "issues.unlabeled": "issue label removed",
             "pull_request.synchronize": "PR push", "pull_request.opened": "PR opened",
             "issue_comment.created": "comment on a PR",
             "pull_request_review.submitted": "PR review submitted", "workflow_run": "run of ", "manual": "a person",
             "pull_request.reopened": "PR reopened", "pull_request.ready_for_review": "PR ready for review",
             "pull_request.converted_to_draft": "PR converted to draft", "pull_request.closed": "PR closed unmerged",
             "pull_request.review_requested": "PR review requested", "pull_request.unlabeled": "PR label removed",
             "pull_request.edited": "PR edited", "pull_request_review_comment.created": "PR review comment"}
    if event == "webhook":
        return f"webhook {label}"
    if event == "schedule":
        return f"cron {schedules.get(label, {}).get('cron', label)}"
    words = label.split(" ") if label else []
    filters = "".join(f" ({w.replace('=', ' ')})" for w in words if "=" in w)  # `by=human` -> `(by human)`
    return names.get(event, event) + " ".join(w for w in words if "=" not in w) + filters


def writes(outcome, n):
    """Labels an outcome adds (they start other nodes), in the chart's words."""
    tiers = "|".join(n.get("tiers") or ["tier"])
    out = []
    for e in outcome.get("effects") or []:
        if "label" in e:
            out.append("+" + e["label"].replace("<next tier>", "{next}").replace("<tier>", "{" + tiers + "}"))
        if "join" in e:
            out.append("join")
    return out


def _id(x):
    return "".join(c if c.isalnum() else "_" for c in x)


def chart(nodes):
    """Mermaid flowchart of the whole factory: stages (rounded), automation nodes (boxes with what starts
    them), planned nodes and edges dashed. Derived from nodes.yaml, so it cannot drift from the factory."""
    scheds, lines = nodes.get("schedules") or {}, ["flowchart LR"]
    lines += ["    classDef stage fill:#eef,stroke:#669", "    classDef node fill:#fff,stroke:#333",
              "    classDef planned fill:#fafafa,stroke:#999,stroke-dasharray:5 5,color:#666",
              "    classDef ext fill:#efe,stroke:#393"]
    stages, edges_out = set(), []
    for name, n in nodes["nodes"].items():
        plan = planned(n)
        trig = "<br/>".join("on: " + trigger_text(t, n, scheds) for t in n.get("triggers") or [])
        who = n["role"] if n["role"] in ("script", "human") else f"{n['role']} · {n.get('tier') or '|'.join(n.get('tiers') or []) or 'review tiers'}"
        tag = f" (#{n['ticket']})" if plan and n.get("ticket") else ""
        lines.append(f'    N_{_id(name)}["<b>{name}</b>{tag}<br/>{trig}<br/>{who}"]:::{"planned" if plan else "node"}')
        src = (n.get("when") or {}).get("stage")
        srcs = src if isinstance(src, list) else [src] if src else []
        for oname, o in (n.get("outcomes") or {}).items():
            oplan = plan or planned(o)
            target = nodes_mod.effect_stage(o)
            froms = o.get("from") or srcs
            for f in froms:
                stages.add(f)
                edges_out.append((f"S_{_id(f)}", f"N_{_id(name)}", None, oplan))
            label = " ".join([oname] + writes(o, n)) + (f" (#{o['ticket']})" if planned(o) and o.get("ticket") else "")
            if target:
                stages.add(target)
                edges_out.append((f"N_{_id(name)}", f"S_{_id(target)}", label, oplan))
            elif any("join" in e for e in o.get("effects") or []) and name != "join":
                edges_out.append((f"N_{_id(name)}", "N_join", label, oplan))
    if any(vocab.ci("ci-green") in t for n in nodes["nodes"].values() for t in n.get("triggers") or []):
        green, red = vocab.ci("ci-green"), vocab.ci("ci-red")
        lines.append(f'    CI["<b>CI</b> quality-gates (labels CI result)<br/>on: PR push<br/>writes +{green} or +{red}"]:::ext')
        edges_out += [("S_in_review", "CI", "push", False), ("CI", "N_review", f"+{green}", False),
                      ("CI", "N_join", f"+{red}", False)]
    for st in sorted(stages - {"none"}):
        lines.append(f'    S_{_id(st)}(["{st}"]):::stage')
    if "none" in stages:
        lines.append('    S_none(["PR with no stage"]):::stage')
    seen = set()
    for a, b, label, plan in edges_out:
        arrow = "-.->" if plan else "-->"
        e = f"    {a} {arrow} {b}" if label is None else f'    {a} {"-." if plan else "--"} "{label}" {arrow} {b}'
        if e not in seen:
            seen.add(e)
            lines.append(e)
    lines += ["    subgraph Legend", '        L_s(["stage: label factory:‹stage›"]):::stage',
              '        L_n["<b>built node</b><br/>on: the labels or events that start its automation"]:::node',
              '        L_p["<b>planned node</b> (#ticket)"]:::planned',
              '        L_s --> L_n', '        L_n -- "outcome +label it adds" --> L_s', '        L_s -.-> L_p', "    end"]
    return "\n".join(lines) + "\n"


def table(nodes):
    """Markdown table: each node, what starts it, what it writes, and whether it is built."""
    scheds = nodes.get("schedules") or {}
    rows = ["| Node | Starts on | Writes (labels it adds, stage it sets) | Role · tier | Status |", "|---|---|---|---|---|"]
    for name, n in nodes["nodes"].items():
        trig = "<br>".join(f"`{trigger_text(t, n, scheds)}`" for t in n.get("triggers") or [])
        outs = []
        for oname, o in (n.get("outcomes") or {}).items():
            w = writes(o, n) + ([f"→ {nodes_mod.effect_stage(o)}"] if nodes_mod.effect_stage(o) else [])
            outs.append(f"{oname}: {' '.join(w) or '—'}" + (f" (planned #{o.get('ticket')})" if planned(o) else ""))
        who = n["role"] if n["role"] in ("script", "human") else f"{n['role']} · {n.get('tier') or '/'.join(n.get('tiers') or []) or 'per focus'}"
        status = f"planned #{n.get('ticket')}" if planned(n) else "built"
        rows.append(f"| `{name}` | {trig} | {'<br>'.join(outs)} | {who} | {status} |")
    return "\n".join(rows) + "\n"


def block(nodes):
    """The generated docs block: the chart and the table."""
    return "```mermaid\n" + chart(nodes) + "```\n\n" + table(nodes)


def main(argv):
    """Print the docs block of nodes.yaml (default: the team's nodes)."""
    argv = config.pop_team(argv)
    print(block(nodes_mod.load(argv[0] if argv else None)), end="")
    return 0

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Validate engine/nodes.yaml (ADR-0014) and print its stage chart.

Usage: nodes.py validate [nodes.yaml] | nodes.py chart [nodes.yaml]
Each error line starts with a name: no-trigger, bad-condition, unknown-effect, unknown-role,
unknown-tier, unknown-environment, unknown-stage, unknown-schedule, bad-output, unreachable-stage, unused-stage.
"""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import config as config_mod  # noqa: E402  (`validate` takes a `config` dict)
import vocab  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EFFECTS = {"stage", "label", "unlabel", "comment", "review", "create-issues", "merge", "update-branch", "set-field", "join", "request-reviewers", "park-behind-main-red", "assign", "unassign"}
OUTPUTS = {"read-only", "create-pr", "push-to-source", "push-to-new-branch", "as-instructed"}
WHEN_KEYS = {"stage", "blockers", "focus", "round", "fix-rounds", "ci", "actor", "always", "due", "pr-state",
             "label", "pr", "review", "children", "ticket", "mention"}  # the last six: ADR-0021 facts
NO_ROLE = {"script", "human"}


def load(path=None):
    """nodes.yaml, or the project's v2 workflow rendered into the same shape (ADR-0016)."""
    if path is None and config.is_project():
        import project
        import workflow
        descriptor = project.load(config.team_root())
        return workflow.to_nodes(descriptor, workflow.load(config.team_root(), descriptor))
    return yaml.safe_load(Path(path or (config.engine_root() / config.NODES)).read_text())


def effect_stage(outcome):
    return next((e["stage"] for e in outcome.get("effects", []) if "stage" in e), None)


def edges(nodes):
    """(source, target, label) per outcome that sets a stage; source is its `from` or the node's stage."""
    out = []
    for name, n in nodes["nodes"].items():
        for oname, o in n["outcomes"].items():
            target = effect_stage(o)
            if target is None:
                continue
            src = o.get("from") or [(n["when"] or {}).get("stage", "none")]
            src = src[0] if isinstance(src[0], list) else src
            label = f"{name}{' (P)' if n.get('pilot') else ''}: {oname}"
            out += [(s, target, label) for s in src]
    return out


def validate(nodes, config):
    errs = []
    project = config.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("name"), str) or not project["name"].strip():
        errs.append("project-name: project.name must be a non-empty string")
    stages, tiers = config["tracker"]["stages"], ((config.get("targets") or {}).get("air") or {}).get("models") or {}
    kinds = config["tracker"].get("stage_kinds", {})
    errs += [f"stage-without-kind: {s}" for s in sorted(set(stages) - set(kinds))]
    errs += [f"bad-stage-kind: {s}: {k}" for s, k in kinds.items() if k not in ("active", "waiting", "final")]
    roles = config["roles"]
    errs += config_mod.mcp_errors(roles, ((config.get("targets") or {}).get("air") or {}).get("mcp_connectors"), nodes)
    envs, scheds = nodes.get("environments") or {}, nodes.get("schedules") or {}

    def stage(where, s):
        if s != "none" and s not in stages:
            errs.append(f"unknown-stage: {where}: {s}")

    for name, n in (nodes.get("nodes") or {}).items():
        if not isinstance(n, dict) or not isinstance(n.get("outcomes"), dict) or not n["outcomes"]:
            errs.append(f"schema: {name}: needs a mapping with outcomes")
            continue
        if not n.get("triggers"):
            errs.append(f"no-trigger: {name}")
        for t in n.get("triggers") or []:
            if str(t).startswith("schedule ") and str(t)[9:] not in scheds:
                errs.append(f"unknown-schedule: {name}: {t}")
        if not isinstance(n.get("when"), dict) or not n["when"]:
            errs.append(f"bad-condition: {name}: `when` must be one non-empty mapping")
        else:
            for k in n["when"]:
                if k not in WHEN_KEYS:
                    errs.append(f"bad-condition: {name}: unknown `when` key {k}")
            if "stage" in n["when"]:
                for st in n["when"]["stage"] if isinstance(n["when"]["stage"], list) else [n["when"]["stage"]]:
                    stage(f"{name}.when", st)
        if any(str(t).startswith(("pull_request", "workflow_run")) for t in n.get("triggers") or []) \
                and "ci" not in (n.get("when") or {}) and not n.get("ci-independent"):
            errs.append(f"ci-gate: {name}: acts on a PR head, so `when` needs `ci` or the node needs `ci-independent: true`")
        role = n.get("role")
        if role not in NO_ROLE and role not in roles:
            errs.append(f"unknown-role: {name}: {role}")
        for t in n.get("tiers") or [n["tier"]] if n.get("tier") or n.get("tiers") else []:
            if t not in tiers:
                errs.append(f"unknown-tier: {name}: {t}")
        if role in roles and role != "reviewer" and not n.get("tier") and not n.get("tiers") and "tier" not in roles[role]:
            errs.append(f"unknown-tier: {name}: no tier")
        for f in n.get("focuses") or []:
            if f not in (roles.get("reviewer", {}).get("modes") or {}):
                errs.append(f"unknown-role: {name}: reviewer focus {f}")
        if role != "human" and n.get("env") not in envs:
            errs.append(f"unknown-environment: {name}: {n.get('env')}")
        if n.get("output", "read-only") not in OUTPUTS:
            errs.append(f"bad-output: {name}: {n.get('output')}")
        for oname, o in n["outcomes"].items():
            for e in (o or {}).get("effects") or []:
                keys = [k for k in e if k != "on"] if isinstance(e, dict) else []
                if len(keys) != 1 or keys[0] not in EFFECTS:
                    errs.append(f"unknown-effect: {name}.{oname}: {e}")
                elif "on" in e and (e["on"] not in ("issue", "pr", "mention") or keys[0] not in ("label", "unlabel", "comment")
                                   or (e["on"] == "mention" and keys[0] != "comment")):  # #590: where the question was
                    errs.append(f"unknown-effect: {name}.{oname}: bad `on` in {e}")
            if (o or {}).get("else") and o["else"] not in n["outcomes"]:
                errs.append(f"unknown-outcome: {name}.{oname}: else {o['else']}")
            for s in (o or {}).get("from") or []:
                stage(f"{name}.{oname}.from", s)
            if effect_stage(o or {}):
                stage(f"{name}.{oname}", effect_stage(o))

    reach, todo = {"draft"} | {s for s, _, _ in edges(nodes) if s == "none"}, ["draft", "none"]
    while todo:
        cur = todo.pop()
        for s, t, _ in edges(nodes):
            if s == cur and t not in reach:
                reach.add(t)
                todo.append(t)
    used = {s for s, t, _ in edges(nodes)} | {t for s, t, _ in edges(nodes)}
    errs += [f"unreachable-stage: {s}" for s in sorted(used - reach - {"none"})]
    read = {st for n in (nodes.get("nodes") or {}).values() if isinstance(n.get("when"), dict)
            for st in (n["when"].get("stage") if isinstance(n["when"].get("stage"), list) else [n["when"].get("stage")])}
    read |= {s for n in (nodes.get("nodes") or {}).values() for o in (n.get("outcomes") or {}).values()
             for s in (o or {}).get("from") or []}
    errs += [f"unused-stage: {s}" for s in sorted(set(stages) - used - read)]
    return errs


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
             "pull_request_review.submitted": "PR review submitted", "workflow_run": "run of ", "manual": "a person"}
    if event == "webhook":
        return f"webhook {label}"
    if event == "schedule":
        return f"cron {schedules.get(label, {}).get('cron', label)}"
    return names.get(event, event) + label


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
            target = effect_stage(o)
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
            w = writes(o, n) + ([f"→ {effect_stage(o)}"] if effect_stage(o) else [])
            outs.append(f"{oname}: {' '.join(w) or '—'}" + (f" (planned #{o.get('ticket')})" if planned(o) else ""))
        who = n["role"] if n["role"] in ("script", "human") else f"{n['role']} · {n.get('tier') or '/'.join(n.get('tiers') or []) or 'per focus'}"
        status = f"planned #{n.get('ticket')}" if planned(n) else "built"
        rows.append(f"| `{name}` | {trig} | {'<br>'.join(outs)} | {who} | {status} |")
    return "\n".join(rows) + "\n"


def block(nodes):
    """The generated docs block: the chart and the table."""
    return "```mermaid\n" + chart(nodes) + "```\n\n" + table(nodes)


def main(argv):
    argv = config.pop_team(argv)
    if not argv or argv[0] not in ("validate", "chart"):
        print(__doc__)
        return 2
    nodes = load(argv[1] if len(argv) > 1 else None)
    if argv[0] == "chart":
        print(block(nodes), end="")
        return 0
    errs = validate(nodes, config.load())
    print("\n".join(errs) or "nodes.yaml: ok")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Validate workflows/nodes.yaml (ADR-0014) and print its stage chart.

Usage: nodes.py validate [nodes.yaml] | nodes.py chart [nodes.yaml]
Each error line starts with a name: no-trigger, bad-condition, unknown-effect, unknown-role,
unknown-tier, unknown-environment, unknown-stage, unknown-schedule, bad-output, unreachable-stage.
"""
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
EFFECTS = {"stage", "label", "unlabel", "comment", "review", "create-issues", "merge", "set-field", "join", "request-reviewers"}
OUTPUTS = {"read-only", "create-pr", "push-to-source", "push-to-new-branch", "as-instructed"}
WHEN_KEYS = {"stage", "blockers", "focus", "round", "fix-rounds", "ci", "actor", "always", "due", "pr-state"}
NO_ROLE = {"script", "human"}


def load(path=None):
    return yaml.safe_load(Path(path or ROOT / "workflows/nodes.yaml").read_text())


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
    stages, tiers = config["tracker"]["stages"], config["models"]
    roles = config["roles"]
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
        if n.get("environment", "default") not in envs:
            errs.append(f"unknown-environment: {name}: {n.get('environment')}")
        if n.get("output", "read-only") not in OUTPUTS:
            errs.append(f"bad-output: {name}: {n.get('output')}")
        for oname, o in n["outcomes"].items():
            for e in (o or {}).get("effects") or []:
                keys = [k for k in e if k != "on"] if isinstance(e, dict) else []
                if len(keys) != 1 or keys[0] not in EFFECTS:
                    errs.append(f"unknown-effect: {name}.{oname}: {e}")
                elif "on" in e and (e["on"] not in ("issue", "pr") or keys[0] not in ("label", "unlabel")):
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
    return errs


def chart(nodes):
    lines = ["stateDiagram-v2", "    direction LR", "    [*] --> draft"]
    seen = set()
    for s, t, label in edges(nodes):
        e = f"    {'[*]' if s == 'none' else s.replace('-', '_')} --> {t.replace('-', '_')}: {label}"
        if e not in seen:
            seen.add(e)
            lines.append(e)
    lines.append("    done --> [*]")
    return "\n".join(lines) + "\n"


def main(argv):
    if not argv or argv[0] not in ("validate", "chart"):
        print(__doc__)
        return 2
    nodes = load(argv[1] if len(argv) > 1 else None)
    if argv[0] == "chart":
        print(chart(nodes), end="")
        return 0
    errs = validate(nodes, yaml.safe_load((ROOT / "factory.yaml").read_text()))
    print("\n".join(errs) or "nodes.yaml: ok")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

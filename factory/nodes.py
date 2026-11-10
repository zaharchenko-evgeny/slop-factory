#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Validate engine/nodes.yaml (ADR-0014); `chart` prints its stage chart through the chart plugin (ADR-0026).

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
import plugins  # noqa: E402  (ADR-0026)

ROOT = Path(__file__).resolve().parents[1]
EFFECTS = {"stage", "label", "unlabel", "comment", "review", "create-issues", "merge", "update-branch", "set-field", "join", "request-reviewers", "park-behind-main-red", "assign", "unassign"}  # and the plugins' effects
OUTPUTS = {"read-only", "create-pr", "push-to-source", "push-to-new-branch", "as-instructed"}
WHEN_KEYS = {"stage", "blockers", "focus", "round", "fix-rounds", "ci", "actor", "always", "due", "pr-state",
             "label", "pr", "review", "children", "ticket"}  # the last five: ADR-0021 facts; and the plugins' guards
NO_ROLE = {"script", "human"}


def load(path=None):
    """nodes.yaml, or the nodes of the team's definition (ADR-0021)."""
    import definition
    if path is None and definition.present(config.team_root()):
        return definition.nodes(definition.load(config.team_root()))
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
    registry = plugins.registry(config)  # ADR-0026: the keys of the plugins the team turns on
    effects, when_keys = EFFECTS | registry.effect_kinds(), WHEN_KEYS | set(registry.guards())

    def off(where, key, of):
        """The module-off error when `key` belongs to a plugin that is off, else None."""
        owner = plugins.owner(key, of) if isinstance(key, str) else None
        if owner is None or registry.is_on(owner):
            return None
        return plugins.refusal(where, f"`on: {key}`" if of == "trigger" else key, owner)

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
                if k not in when_keys:
                    errs.append(off(f"{name}.when", k, "guard") or f"bad-condition: {name}: unknown `when` key {k}")
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
            if n.get("routine") and n.get("triggers") and all(str(t).startswith("schedule ") for t in n["triggers"]) and (o or {}).get("effects"):  # ADR-0015: `apply_routine` applies no effects
                errs.append(f"routine-effects: {name}.{oname}")
            for e in (o or {}).get("effects") or []:
                keys = [k for k in e if k != "on"] if isinstance(e, dict) else []
                if len(keys) != 1 or keys[0] not in effects:
                    errs.append(len(keys) == 1 and off(f"{name}.{oname}", keys[0], "effect")
                                or f"unknown-effect: {name}.{oname}: {e}")
                elif "on" in e:
                    target = e["on"]
                    if target in ("issue", "pr"):
                        ok = keys[0] in ("label", "unlabel", "comment") \
                            or registry.effect(keys[0], {"issue": "ticket"}.get(target, target)) is not None  # notify
                    else:  # a plugin's target: only an effect it declares there (#590: `comment` `on: mention`)
                        ok = isinstance(target, str) and target in registry.triggers() \
                            and registry.effect(keys[0], target) is not None
                    if not ok:
                        errs.append(off(f"{name}.{oname}", target, "trigger")
                                    or f"unknown-effect: {name}.{oname}: bad `on` in {e}")
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


def main(argv):
    argv = config.pop_team(argv)
    if not argv or argv[0] not in ("validate", "chart"):
        print(__doc__)
        return 2
    if argv[0] == "chart":  # the chart plugin (ADR-0026): development tooling, on whenever its command is used
        return plugins.registry({}, use=(("tool", "chart"),)).command("chart")(argv[1:])
    nodes = load(argv[1] if len(argv) > 1 else None)
    errs = validate(nodes, config.load())
    print("\n".join(errs) or "nodes.yaml: ok")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

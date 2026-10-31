#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The gate: decide whether a node runs (check) and apply an outcome's effects (apply).

Usage: gate.py check --node N --event FILE [--tracker github|memory:FILE]
       gate.py apply --node N --outcome O --payload FILE [--tracker github|memory:FILE]
       gate.py sweep [--tracker github] [--dry-run]   (no agent: lists the tickets itself, one stage per ticket)
Exit 0 go / done, 10 no go (stale, duplicate or the node no longer matches), 2 bad event, payload, node
or outcome, 1 a failing effect (the stage is untouched: it is the last effect).
An event is JSON {id, ticket, head?, actor?}. A payload is JSON {ticket, event?, head?, actor?, ...}
plus one field per outcome effect that needs data (the error names the field).
`--tracker memory:FILE` runs on a JSON file {ticket: record} with the in-memory fake; `--tracker github`
uses factory/gate_github.py (bot login from `targets.air.bot_login`).
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nodes as nodes_mod  # noqa: E402
from gate_github import GitHubGateTracker  # noqa: E402
from gate_port import MemoryTracker  # noqa: E402

ROOT = nodes_mod.ROOT
GO, NO_GO, BAD, FAILED = 0, 10, 2, 1


class Bad(Exception):
    pass


def value(want, allowed):
    if want not in allowed:
        raise Bad(f"bad condition value {want!r}: allowed {', '.join(allowed)}")


def holds(when, facts, event):
    """Every key of `when` must hold (the vocabulary is nodes.WHEN_KEYS)."""
    def one(key, want):
        if key == "stage":
            return facts["stage"] == want
        if key == "blockers":
            return (facts["blockers_open"] == 0) == (want == "closed")
        if key == "focus":
            miss = facts["focus_missing"]
            return bool(miss) and event.get("focus", miss[0]) in miss
        if key == "round":
            value(want, ("complete", "complete-at-head"))
            return facts["round_complete"] and (want != "complete-at-head" or facts["round_head"] == facts["head"])
        if key == "fix-rounds":
            value(want, ("below-cap", "at-cap"))
            return (facts["fix_rounds"] < facts["fix_cap"]) == (want == "below-cap")
        if key == "ci":
            value(want, ("green", "red", "pending", "none"))
            return facts["ci"] == want
        if key == "actor":
            return event.get("actor") == want
        if key == "pr-state":
            value(want, ("open", "merged", "closed", "none"))
            return facts.get("pr_state", "none") == want
        if key == "always":
            return bool(want)
        return bool(facts["due"]) == bool(want)  # due
    return all(one(k, v) for k, v in when.items())


def load_node(name, nodes):
    node = (nodes.get("nodes") or {}).get(name)
    if node is None or node.get("enabled") is False:
        raise Bad(f"unknown node: {name}")
    return node


def read(path, what):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise Bad(f"malformed {what}: {e}")
    if not isinstance(data, dict) or "ticket" not in data:
        raise Bad(f"malformed {what}: needs an object with `ticket`")
    return data


def matches(node, facts, event, outcome=None):
    """None when the node matches, else the reason it does not."""
    if event.get("head") is not None and event["head"] != facts["head"]:
        return "stale head"
    if event.get("id") is not None and event["id"] in facts["seen"]:
        return "duplicate event"
    if outcome and outcome.get("from") and facts["stage"] not in outcome["from"]:
        return f"stage {facts['stage']} is not in the outcome's from"
    return None if holds(node["when"], facts, event) else "condition does not hold"


def check(name, event, tracker, nodes, config):
    node = load_node(name, nodes)
    if "id" not in event:
        raise Bad("malformed event: needs `id`")
    facts = tracker.facts(event["ticket"])
    why = matches(node, facts, event)
    role, roles = node["role"], config["roles"]
    tiers = node.get("tiers") or [node.get("tier") or roles.get(role, {}).get("tier")]
    skills = ROOT / "roles" / role / "skills.txt"
    return (NO_GO if why else GO), {
        "go": why is None, "reason": why, "ticket": event["ticket"], "node": name, "role": role,
        "tier": event.get("tier") if event.get("tier") in tiers else tiers[0],
        "role_file": f"roles/{role}/role.md" if role in roles else None,
        "skills": skills.read_text().split() if skills.exists() else [],
        "context": facts, "outcomes": list(node["outcomes"]),
    }


def resolve(effect, payload, ticket):
    """(kind, value) of one effect; a missing payload field raises Bad naming it."""
    (kind, arg), = [(k, v) for k, v in effect.items() if k != "on"]
    on = effect.get("on")

    def field(f):
        if f not in payload:
            raise Bad(f"payload: missing field `{f}`")
        return payload[f]
    if kind == "set-field":
        return {"kind": kind, "ticket": ticket, "value": {arg: field(arg)}}
    if kind in ("label", "unlabel"):
        out = {"kind": kind, "ticket": ticket, "value": re.sub(r"<([^>]+)>", lambda m: str(field(m[1])), arg)}
        return {**out, "on": on} if on else out
    if kind in ("comment", "create-issues"):
        return {"kind": kind, "ticket": ticket, "value": field(arg)}
    return {"kind": kind, "ticket": ticket, "value": arg}  # stage, review, merge, join


def apply(name, outcome, payload, tracker, nodes):
    node = load_node(name, nodes)
    if outcome not in node["outcomes"]:
        raise Bad(f"unknown outcome: {outcome}")
    out = node["outcomes"][outcome]
    if out.get("enabled") is False:
        raise Bad(f"disabled outcome: {outcome}")
    ticket = payload["ticket"]
    effects = [resolve(e, payload, ticket) for e in out.get("effects") or []]  # validates the payload first
    effects.sort(key=lambda e: e["kind"] == "stage")  # the stage change goes last
    for e in effects:
        if e["kind"] == "stage":
            e["event"] = payload.get("event")
        if e["kind"] == "review":
            e["focus"], e["round"] = payload.get("focus"), payload.get("round")
        if e["kind"] == "comment" and payload.get("run_id"):
            e["run_id"] = payload["run_id"]
    if not any(e["kind"] == "stage" for e in effects) and payload.get("event") is not None:
        effects.append({"kind": "seen", "ticket": ticket, "value": payload["event"]})
    why = matches(node, tracker.facts(ticket), {**payload, "id": payload.get("event")}, out)
    if why:
        return NO_GO, {"applied": False, "reason": why}
    return GO, {"applied": True, "written": [e["kind"] for e in effects if tracker.apply(e)]}


SWEEP_STAGES = ("approved", "merged", "split", "in-review", "changes-requested")


def sweep_plan(facts):
    """(node, outcome, payload) of the first outcome whose facts hold, else None."""
    stage = facts["stage"]
    if stage == "approved" and facts.get("pr_state") == "merged":
        return "sweep", "merged", {}
    if stage == "merged":
        agent = [r for r in facts["reviews"] if r["agent"]]
        text = (f"Retro: PR #{facts['pr']} {facts.get('pr_state')}; fix rounds {facts['fix_rounds']}; "
                f"reviews {len(agent)} ({sum(r['verdict'] == 'approve' for r in agent)} approve, "
                f"{sum(r['verdict'] == 'changes' for r in agent)} changes)")
        return "sweep", "retro", {"retro": text, "run_id": "sweep:retro"}
    if stage == "split" and facts.get("children_total") and facts["children_open"] == 0:
        return "sweep", "children-done", {}
    if stage == "in-review" and facts["round_complete"] and facts["round_head"] == facts["head"]:
        changes = any(r["agent"] and r["commit_id"] == facts["head"] and r["verdict"] == "changes" for r in facts["reviews"])
        return "join", "changes" if changes else "approved", {"tier": "cheap"}
    return None


def sweep(tracker, nodes, dry_run=False):
    """One pass: each listed ticket moves at most one stage. Returns (results, failed)."""
    results, failed = [], False
    for n in tracker.sweep_tickets(SWEEP_STAGES):
        try:
            facts = tracker.facts(n)
            plan = sweep_plan(facts)
            if plan is None:
                continue
            node, outcome, payload = plan
            r = {"ticket": n, "stage": facts["stage"], "node": node, "outcome": outcome}
            if not dry_run:
                code, out = apply(node, outcome, {"ticket": n, **payload}, tracker, nodes)
                r.update(applied=out["applied"], written=out.get("written", []))
            results.append(r)
        except Exception as e:  # one ticket failing does not stop the others
            failed = True
            results.append({"ticket": n, "error": f"{type(e).__name__}: {e}"})
    return results, failed


def main(argv):
    p = argparse.ArgumentParser(prog="gate.py")
    p.add_argument("cmd", choices=["check", "apply", "sweep"])
    p.add_argument("--node", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--event")
    p.add_argument("--outcome")
    p.add_argument("--payload")
    p.add_argument("--tracker", default="")
    p.add_argument("--nodes")
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return BAD
    path = ""
    try:
        kind, _, path = (a.tracker or ("github" if a.cmd == "sweep" else "")).partition(":")
        if kind == "github" and not path:
            tracker = GitHubGateTracker.from_config()
        elif kind == "memory" and path:
            try:
                tracker = MemoryTracker(dict(json.loads(Path(path).read_text())))
            except (OSError, ValueError, TypeError) as e:
                raise Bad(f"malformed --tracker file: {e}")
        else:
            raise Bad("--tracker is github or memory:FILE")
        nodes = nodes_mod.load(a.nodes)
        config = yaml.safe_load((ROOT / "factory.yaml").read_text())
        if a.cmd == "sweep":
            if not hasattr(tracker, "sweep_tickets"):
                raise Bad("sweep needs --tracker github")
            results, failed = sweep(tracker, nodes, a.dry_run)
            print(json.dumps({"dry_run": a.dry_run, "sweep": results}))
            return FAILED if failed else GO
        if not a.node:
            raise Bad("--node is required")
        if a.cmd == "check":
            code, out = check(a.node, read(a.event or "", "event"), tracker, nodes, config)
        else:
            code, out = apply(a.node, a.outcome or "", read(a.payload or "", "payload"), tracker, nodes)
            path and Path(path).write_text(json.dumps(tracker.tickets))
    except Bad as e:
        print(json.dumps({"error": str(e)}))
        return BAD
    except Exception as e:  # a failing effect: later effects and the stage did not run
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        if path:
            Path(path).write_text(json.dumps(tracker.tickets))
        return FAILED
    print(json.dumps(out))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

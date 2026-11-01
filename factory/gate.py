#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The gate: decide whether a node runs (check) and apply an outcome's effects (apply).

Usage: gate.py check --node N --event FILE [--tracker github|memory:FILE]
       gate.py apply --node N --outcome O --payload FILE [--tracker github|memory:FILE]
       gate.py apply --node N --handoff FILE [--event FILE]   (outcome and payload derived from the role's handoff)
       gate.py sweep [--tracker github] [--dry-run]   (no agent: lists the tickets itself, one stage per ticket)
Exit 0 go / done, 10 no go (stale, duplicate or the node no longer matches), 2 bad event, payload, node
or outcome, 1 a failing effect (the stage is untouched: it is the last effect).
An event is JSON {id, ticket, head?, actor?}. A payload is JSON {ticket, event?, head?, actor?, ...}
plus one field per outcome effect that needs data (the error names the field).
`check --event` also takes the raw trigger payload (an `issues`, `pull_request` or `pull_request_review`
event): the ticket is the issue number or the `<n>` of the PR branch `ez/<n>-*` (none: exit 10).
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
            return facts["stage"] in (want if isinstance(want, list) else [want])
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
            if isinstance(want, list):  # any of these
                return facts["ci"] in want
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


class NoTicket(Exception):
    pass


def from_trigger(d):
    """{id, ticket, head, actor} of a raw webhook payload (issues, pull_request, pull_request_review)."""
    pr, issue, review = d.get("pull_request"), d.get("issue"), d.get("review")
    if not isinstance(pr, dict) and not isinstance(issue, dict):
        raise Bad("malformed event: needs `ticket` or a trigger payload")
    name = "pull_request_review" if review and pr else "pull_request" if pr else "issues"
    src = pr or issue
    if pr:
        m = re.match(r"ez/(\d+)-", (pr.get("head") or {}).get("ref") or "")
        if not m:
            raise NoTicket(f"PR #{pr.get('number')} is not on an ez/<n>- branch")
        ticket = int(m[1])
    else:
        ticket = issue.get("number")
    when = d.get("delivery") or d.get("delivery_id") or (review or {}).get("submitted_at") or src.get("updated_at")
    return {"id": f"{name}:{when}", "ticket": ticket, "head": ((pr or {}).get("head") or {}).get("sha"),
            "actor": (d.get("sender") or {}).get("login")}


def read(path, what, raw=False):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise Bad(f"malformed {what}: {e}")
    if isinstance(data, dict) and raw and "ticket" not in data:
        return from_trigger(data)
    if not isinstance(data, dict) or "ticket" not in data:
        raise Bad(f"malformed {what}: needs an object with `ticket`")
    return data


COMPLEXITY_TIER = {"small": "cheap", "medium": "mid", "large": "strong"}
FIX_TIERS = ("cheap", "mid", "strong")


def slices(split):
    """The proposed slices of a router `split` (a string of lines or a list) as {title, body} children."""
    lines = split.splitlines() if isinstance(split, str) else [str(x) for x in split or []]
    out = [x.strip().lstrip("-").strip() for x in lines]
    return [{"title": x.split(" — ")[0].strip(), "body": x} for x in out if x and x != "none"]


def intake_fields(h):
    """Payload fields the outcomes need and the handoff names otherwise.
    ROUTER-INTAKE: complexity and tier from the size, `questions` from `missing`, `children` from `split`.
    A blocked or failed handoff: `reason` from `blocked_reason`."""
    out = {"reason": h["blocked_reason"]} if h.get("blocked_reason") not in (None, "", "none") else {}
    if h.get("handoff") != "ROUTER-INTAKE":
        return out
    n = int("".join(c for c in str(h.get("size_estimate", "")) if c.isdigit()) or 0)
    missing = ", ".join(h["missing"]) if isinstance(h.get("missing"), list) else str(h.get("missing", ""))
    return {**out, "complexity": "small" if n <= 150 else "medium" if n <= 400 else "large",
            "tier": "cheap" if n <= 150 else "mid" if n <= 400 else "strong",
            "questions": f"[factory] Needs answers before work starts: {missing}",
            "children": slices(h.get("split")) if h.get("split") not in (None, "none") else []}


def outcome_of(h):
    """The node outcome a handoff names: `outcome`, a reviewer `verdict`, a router intake, else `status`."""
    if h.get("outcome"):
        return h["outcome"]
    if h.get("verdict"):
        return "approve" if h["verdict"] == "approved" else "changes"
    if h.get("handoff") == "ROUTER-INTAKE":
        # shortcut: a ready bug proceeds until the bug path (#161) is enabled: `reproduce` is disabled
        return "proceed" if h.get("ready") == "yes" and h.get("split") in (None, "none") else (
            "split" if h.get("split") not in (None, "none") else "grill")
    return h.get("status")


def fix_tier(facts):
    """The fix tier of the ticket's `complexity:*` label (mid without one)."""
    for label in facts.get("attrs", []):
        if label.startswith("complexity:"):
            return COMPLEXITY_TIER.get(label[11:], "mid")
    return "mid"


def branch_of(facts, config):
    """The ticket's branch: the PR's head, else `branch_pattern` from the issue title (drive.py's slug rule)."""
    if facts.get("branch"):
        return facts["branch"]
    from drive import branch_name
    return branch_name(config["branch_pattern"], facts["ticket"], facts.get("title") or "", facts.get("body") or "")


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
    branch = branch_of({**facts, "ticket": event["ticket"]}, config)
    facts = {k: v for k, v in facts.items() if k != "body"}
    role, roles = node["role"], config["roles"]
    tiers = node.get("tiers") or [node.get("tier") or roles.get(role, {}).get("tier")]
    if node.get("focuses"):  # one review tier per focus (roles.<role>.modes.<focus>.tier)
        tiers = [roles[role]["modes"][f]["tier"] for f in node["focuses"]]
    skills = ROOT / "roles" / role / "skills.txt"
    return (NO_GO if why else GO), {
        "go": why is None, "reason": why, "ticket": event["ticket"], "node": name, "role": role,
        "tier": event.get("tier") if event.get("tier") in tiers else tiers[0],
        "focus": event.get("focus"),
        "role_file": f"roles/{role}/role.md" if role in roles else None,
        "skills": skills.read_text().split() if skills.exists() else [],
        "context": facts, "outcomes": list(node["outcomes"]),
        "branch": branch, "handoff_path": f".factory/handoff/{name}.json",
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


def apply(name, outcome, payload, tracker, nodes, focus=None, tier=None):
    node = load_node(name, nodes)
    if outcome not in node["outcomes"]:
        raise Bad(f"unknown outcome: {outcome}")
    out = node["outcomes"][outcome]
    if out.get("enabled") is False:
        raise Bad(f"disabled outcome: {outcome}")
    ticket = payload["ticket"]
    payload = {**{k: v for k, v in (("focus", focus), ("tier", tier)) if v}, **payload}  # the automation's own
    out = node["outcomes"][outcome]
    if out.get("ci") and tracker.facts(ticket)["ci"] not in out["ci"] and out.get("else") in node["outcomes"]:
        out = node["outcomes"][out["else"]]  # e.g. all reviews approve but CI is red: a fix round
        if "tier" not in payload:  # the agent saw `approved`; the fix label needs the cheapest fix tier
            payload = {**payload, "tier": nodes["nodes"]["fix"]["tiers"][0]}
    effects = [resolve(e, payload, ticket) for e in out.get("effects") or []]  # validates the payload first
    facts = tracker.facts(ticket)
    if name == "fix" and outcome == "done" and facts.get("handed_to_fix") and local_head() == facts.get("head"):
        # The platform pushes after the run; an unchanged HEAD means the fix made no commit, and no event
        # would ever start the next round. A person decides.
        effects = [{"kind": "comment", "ticket": ticket, "value": (
                       "[factory] Stopped: the fix round ended without a commit, so nothing starts the next "
                       "review. A person decides, then sets factory:ready.")},
                   {"kind": "stage", "ticket": ticket, "value": "blocked"}]
    if name == "implement" and outcome == "done" and facts.get("pr") and facts.get("ci") in ("green", "red") \
            and local_head() == facts.get("head"):
        # A resumed ticket implemented on its open PR with no new commit: no push, so no CI label would come.
        effects = refire_ci(ticket, facts["ci"]) + effects
    starts = [e for e in effects if e["kind"] == "label" and str(e["value"]).startswith(COMMANDS)]
    stop = starts and stop_reason(facts, fix=any(str(e["value"]).startswith("factory:fix-") for e in starts))
    if stop:
        # The transition that would start a developer run it may not start blocks the ticket instead; a check
        # that refused later would stop it with no comment and no event to wake it.
        effects = [{"kind": "comment", "ticket": ticket, "value": stop},
                   {"kind": "stage", "ticket": ticket, "value": "blocked"}]
    if any(e["kind"] == "stage" and e["value"] in ("blocked", "failed") for e in effects):
        effects = clear_commands(ticket) + effects
    effects.sort(key=lambda e: e["kind"] == "stage")  # the stage change goes last
    for e in effects:
        if e["kind"] == "stage":
            e["event"] = payload.get("event")
        if e["kind"] == "review":
            e["focus"], e["round"] = payload.get("focus"), payload.get("round")
        if e["kind"] == "join":
            e["tier"] = payload.get("tier")
        if e["kind"] == "comment" and payload.get("run_id"):
            e["run_id"] = payload["run_id"]
    if not any(e["kind"] == "stage" for e in effects) and payload.get("event") is not None:
        effects.append({"kind": "seen", "ticket": ticket, "value": payload["event"]})
    why = matches(node, facts, {**payload, "id": payload.get("event")}, out)
    if why:
        return NO_GO, {"applied": False, "reason": why}
    return GO, {"applied": True, "written": [e["kind"] for e in effects if tracker.apply(e)]}


SWEEP_STAGES = ("approved", "merged", "split", "in-review")
COMMANDS = ("factory:implement-", "factory:fix-", "factory:update-")


def clear_commands(ticket):
    """Effects that remove every command label. A stopped ticket keeps none, so the first command after a
    person's factory:ready is a new label and fires its node (re-adding a present label fires nothing)."""
    return [{"kind": "unlabel", "ticket": ticket, "value": "factory:implement-*"},
            {"kind": "unlabel", "ticket": ticket, "value": "factory:fix-*", "on": "pr"},
            {"kind": "unlabel", "ticket": ticket, "value": "factory:update-*", "on": "pr"}]


def refire_ci(ticket, ci):
    """Effects that write the PR's CI label again (remove, then add), so its node starts for an unchanged head."""
    return [{"kind": "unlabel", "ticket": ticket, "value": "factory:ci-green", "on": "pr"},
            {"kind": "unlabel", "ticket": ticket, "value": "factory:ci-red", "on": "pr"},
            {"kind": "label", "ticket": ticket, "value": f"factory:ci-{ci}", "on": "pr"}]


def stop_reason(facts, fix=False):
    """Why no further developer run may start for this ticket, else None: the spend guard, and for a fix
    round the fix-round cap (`limits.fix_rounds`)."""
    if facts.get("agent_runs", 0) >= facts.get("run_cap", 8):
        return (f"[factory] Stopped: {facts['agent_runs']} developer runs since the last factory:ready reached "
                f"limits.agent_runs_per_ticket. A person decides, then sets factory:ready.")
    if fix and facts.get("fix_rounds", 0) >= facts.get("fix_cap", 3):
        return (f"[factory] Stopped: {facts['fix_rounds']} fix rounds reached limits.fix_rounds and the review "
                f"still requests changes. A person decides, then sets factory:ready.")
    return None


def local_head():
    """The checkout's HEAD, else None (no git checkout)."""
    import subprocess
    r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def join_decision(facts):
    """The join's outcome and payload, or None while the round is open. Uses the adapter's `round_verdict`,
    which counts every required agent focus, human change requests and CI. A fix label takes the ticket's
    complexity tier (mid when unlabelled)."""
    if facts.get("handed_to_fix") or not facts.get("round_complete") or facts.get("round_verdict") is None:
        return None  # an open round, or a head whose round already went to a fix (its push comes next)
    return ("changes" if facts["round_verdict"] == "changes" else "approved"), {"tier": fix_tier(facts)}


def sweep_plan(facts):
    """(node, outcome, payload) of the first outcome whose facts hold, else None."""
    stage = facts["stage"]
    if stage == "approved" and facts.get("pr_state") == "merged":
        return "finish", "merged", {}
    if stage == "merged":
        agent = [r for r in facts["reviews"] if r["agent"]]
        pr = f"PR #{facts['pr']} {facts['pr_state']}" if facts["pr"] else "no PR found"
        text = (f"Retro: {pr}; fix rounds {facts['fix_rounds']}; "
                f"reviews {len(agent)} ({sum(r['verdict'] == 'approve' for r in agent)} approve, "
                f"{sum(r['verdict'] == 'changes' for r in agent)} changes)")
        return "finish", "retro", {"retro": text, "run_id": "finish:retro"}
    if stage == "split" and facts.get("children_total") and facts["children_open"] == 0:
        return "finish", "children-done", {}
    if stage == "in-review" and facts.get("waiting_reviewers") and not facts.get("handed_to_fix"):
        return "join", "waiting", {}  # ask them again; nothing moves until they decide
    if stage == "in-review" and (j := join_decision(facts)):
        return "join", *j
    return None


def sweep(tracker, nodes, dry_run=False):
    """Move every listed ticket through each step its facts allow (a merged PR goes approved, merged, done in
    one pass). A ticket that reaches done also lets its split parent finish. Returns (results, failed)."""
    results, failed = [], False
    queue, seen = list(tracker.sweep_tickets(SWEEP_STAGES)), set()
    while queue:
        n = queue.pop(0)
        for _ in range(4):  # a ticket's longest walk is approved, merged, done
            try:
                facts = tracker.facts(n)
                plan = sweep_plan(facts)
                if plan is None or (n, facts["stage"]) in seen:
                    break
                seen.add((n, facts["stage"]))
                node, outcome, payload = plan
                r = {"ticket": n, "stage": facts["stage"], "node": node, "outcome": outcome}
                results.append(r)
                if dry_run:
                    break
                code, out = apply(node, outcome, {"ticket": n, **payload}, tracker, nodes)
                r.update(applied=out["applied"], written=out.get("written", []))
                if not out["applied"]:
                    break
                if outcome == "retro" and hasattr(tracker, "parent") and (p := tracker.parent(n)):
                    queue.append(p)
            except Exception as e:  # one ticket failing does not stop the others
                failed = True
                results.append({"ticket": n, "error": f"{type(e).__name__}: {e}"})
                break
    return results, failed


def main(argv):
    p = argparse.ArgumentParser(prog="gate.py")
    p.add_argument("cmd", choices=["check", "apply", "sweep"])
    p.add_argument("--node", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--event")
    p.add_argument("--outcome")
    p.add_argument("--payload")
    p.add_argument("--handoff")
    p.add_argument("--tracker", default="")
    p.add_argument("--nodes")
    p.add_argument("--focus")  # the automation's focus and tier: the trigger payload carries neither
    p.add_argument("--tier")
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return BAD
    path = ""
    try:
        kind, _, path = (a.tracker or "github").partition(":")
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
            event = read(a.event or "", "event", raw=True)
            event.update({k: v for k, v in (("focus", a.focus), ("tier", a.tier)) if v})
            code, out = check(a.node, event, tracker, nodes, config)
        else:
            if a.handoff:
                try:
                    h = json.loads(Path(a.handoff).read_text())
                except (OSError, ValueError) as e:
                    raise Bad(f"malformed handoff: {e}")
                if not isinstance(h, dict):
                    raise Bad("malformed handoff: needs an object")
                ev = read(a.event, "event", raw=True) if a.event else {}
                if "ticket" not in {**h, **ev}:
                    raise Bad("apply --handoff needs `ticket` in the handoff or --event")
                payload = {**intake_fields(h), **h, **{k: v for k, v in ev.items() if v is not None and k != "id"},
                           **({"event": ev["id"]} if "id" in ev else {})}
                out_name = a.outcome or outcome_of(h)
            else:
                payload, out_name = read(a.payload or "", "payload"), a.outcome or ""
            code, out = apply(a.node, out_name, payload, tracker, nodes, a.focus, a.tier)
            path and Path(path).write_text(json.dumps(tracker.tickets))
    except NoTicket as e:
        print(json.dumps({"go": False, "reason": str(e)}))
        return NO_GO
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

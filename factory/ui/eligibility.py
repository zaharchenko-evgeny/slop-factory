"""Read-only eligibility of a ticket for each workflow node: trigger evidence, guards, and execution, kept apart.

- Trigger evidence comes from `local.poll` (the local surface's read of labeled issues and PRs, merges, pushes and
  closes). A trigger that poll cannot see (manual, schedule, webhook, comment, review, CI events) is `unknown`.
- Guards are the node's `when` keys, evaluated one by one with `gate.holds`, and the whole node with `gate.matches`.
  A key that needs evidence the facts lack (an actor, a focus, a due time, a missing fact) is `unknown`.
- Execution has no evidence in the engine (no shared Air/local run registry): it stays `unknown` unless the caller
  passes evidence. Eligibility never says `running`.
Never called here: `factory begin`, `local.run_event`, `gate.sweep`, `gate.check` (it runs plugin context hooks).
"""
import gate
from factory_plugins.local import surface as local
from gate_port import number_of, ticket_from_ref

POLLED = {"issues.labeled", "pull_request.labeled", "pull_request.merged", "pull_request.synchronize", "issues.closed"}
EVENT_KEYS = {"actor": "an actor", "focus": "a focus", "due": "a due time"}  # `when` keys that need event evidence
EXECUTION_STATES = {"queued", "running", "finished", "failed"}


def poll_evidence(config, nodes, source_trackers, run=None):
    """({node: {ticket number: [event ids]}}, error or None) from `local.poll` over `source_trackers`."""
    plan = local.plan_local(nodes, config)
    try:
        events = local.poll(plan, list(source_trackers), run)
    except Exception as e:  # a tracker that cannot be polled: every polled trigger is `unknown`
        return None, f"{type(e).__name__}: {e}"[:300]
    found = {}
    for event_id, entry, payload in events:
        pr, issue = payload.get("pull_request") or {}, payload.get("issue") or {}
        ticket = ticket_from_ref((pr.get("head") or {}).get("ref")) or issue.get("number") or pr.get("number")
        if ticket is not None:
            found.setdefault((entry["node"], payload.get("eventType")), {}).setdefault(number_of(ticket), []).append(event_id)
    return found, None


def triggers(node, ticket, evidence, error):
    """Per trigger of the graph node: {raw, status: match|no-match|unknown, reason, events}."""
    out = []
    for t in node["triggers"]:
        if t["event"] not in POLLED:
            out.append({"raw": t["raw"], "status": "unknown", "reason": f"no event evidence for {t['kind'] if t['kind'] != 'event' else t['event']} triggers", "events": []})
        elif evidence is None:
            out.append({"raw": t["raw"], "status": "unknown", "reason": f"poll failed: {error}", "events": []})
        else:
            ids = (evidence.get((node["id"], t["event"])) or {}).get(number_of(ticket), [])
            hit = list(ids)
            out.append({"raw": t["raw"], "status": "match" if hit else "no-match",
                        "reason": None if hit else f"poll found no {t['event']} event for the ticket", "events": hit})
    return out


def guards(node, facts, event=None):
    """{status: pass|fail|unknown, reason, keys: [{key, want, status, reason}], match} of the node's `when`
    against `facts`, by `gate.holds` per key and `gate.matches` for the known keys together."""
    event = dict(event or {})
    when = node.get("when") or {}
    keys, known = [], {}
    for key, want in when.items():
        if key in EVENT_KEYS and key not in event:
            keys.append({"key": key, "want": want, "status": "unknown", "reason": f"needs {EVENT_KEYS[key]} from an event"})
            continue
        try:
            ok = gate.holds({key: want}, facts, event)
        except (KeyError, TypeError, gate.Bad) as e:
            keys.append({"key": key, "want": want, "status": "unknown", "reason": f"missing evidence: {type(e).__name__} {e}"[:200]})
            continue
        known[key] = want
        keys.append({"key": key, "want": want, "status": "pass" if ok else "fail",
                     "reason": None if ok else f"{key} does not hold (have {_have(key, facts)!r})"})
    try:
        why = gate.matches({"when": known}, facts, event) if known else None
    except (KeyError, TypeError, gate.Bad) as e:
        why = None
        keys.append({"key": "(match)", "want": None, "status": "unknown", "reason": f"{type(e).__name__} {e}"[:200]})
    failed = [k for k in keys if k["status"] == "fail"]
    unknown = [k for k in keys if k["status"] == "unknown"]
    status = "fail" if failed or why else "unknown" if unknown else "pass"
    return {"status": status, "reason": (failed[0]["reason"] if failed else why or (unknown[0]["reason"] if unknown else None)),
            "keys": keys}


def _have(key, facts):
    return {"state": facts.get("stage"), "stage": facts.get("stage"), "blockers": facts.get("blockers_open"),
            "pr-state": facts.get("pr_state"), "ticket": facts.get("issue_state")}.get(key, facts.get(key))


def outcomes(node, facts, tracker=None, ticket=None):
    """Per outcome of the graph node: {name, status: possible|blocked|unknown, reason}: its `from` stages and its
    guard (`gate.guard_holds`) against the facts. Informational: it is not execution."""
    out = []
    for o in node["outcomes"]:
        status, reason = "possible", None
        try:
            if o["from"] and facts["stage"] not in o["from"]:
                status, reason = "blocked", f"stage {facts['stage']} is not in the outcome's from"
            elif o["condition"]:
                guard = dict(o["condition"].get("if") or {})
                if "ci" in o["condition"]:
                    guard["ci"] = o["condition"]["ci"]
                if guard and not gate.guard_holds(guard, facts, tracker, ticket):
                    status, reason = "blocked", "the outcome's guard does not hold"
        except (KeyError, TypeError, gate.Bad, AttributeError) as e:
            status, reason = "unknown", f"missing evidence: {type(e).__name__} {e}"[:200]
        out.append({"name": o["name"], "status": status, "reason": reason})
    return out


def evaluate(node, ticket, facts, evidence, error, tracker=None, execution=None):
    """One ticket against one graph node. `eligible` is `yes`, `no` or `unknown`; `execution` is `unknown`
    unless `execution` evidence names a state."""
    trig = triggers(node, ticket, evidence, error)
    guard = guards(node, facts)
    statuses = [t["status"] for t in trig]
    trigger_status = "match" if "match" in statuses else "unknown" if "unknown" in statuses else "no-match"
    if "no-match" == trigger_status or guard["status"] == "fail":
        eligible = "no"
    elif trigger_status == "match" and guard["status"] == "pass":
        eligible = "yes"
    else:
        eligible = "unknown"
    state = execution if execution in EXECUTION_STATES else None
    return {"node": node["id"], "trigger": {"status": trigger_status, "triggers": trig}, "guards": guard,
            "outcomes": outcomes(node, facts, tracker, ticket), "eligible": eligible,
            "execution": {"status": state or "unknown",
                          "reason": None if state else "no execution evidence: eligibility does not mean running"}}

"""Read-only, three-valued projection of a node's current eligibility."""

from __future__ import annotations

import json

import gate

_NODE_GUARDS = {"stage", "blockers", "focus", "round", "fix-rounds", "ci", "actor", "always", "due",
                "pr-state", "label", "pr", "review", "children", "ticket"}


def _poll_supported(trigger):
    return trigger.startswith(("issues.labeled ", "pull_request.labeled ")) \
        or trigger in ("pull_request.merged", "pull_request.synchronize", "issues.closed")


def _text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _event_parts(events, node_id):
    matched = []
    for item in events or []:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            event_id, entry, payload = item[0], item[1], item[2]
        elif isinstance(item, dict):
            event_id = item.get("id", item.get("event_id"))
            entry = item.get("entry", item)
            payload = item.get("payload", {})
        else:
            continue
        if not isinstance(entry, dict) or entry.get("node") != node_id:
            continue
        payload = payload if isinstance(payload, dict) else {}
        evidence = {**payload}
        if entry.get("focus") is not None:
            evidence.setdefault("focus", entry["focus"])
        head = ((payload.get("pull_request") or {}).get("head") or {}).get("sha")
        if head:
            evidence.setdefault("head", head)
        matched.append({**evidence, **({"id": event_id} if event_id is not None else {})})
    return matched


def _required_facts(key, value, event):
    """Return required fact names; ``None`` means that this guard needs event evidence."""
    pr_requirements = {"behind": ("behind",), "conflicting": ("conflict",)}
    requirements = {
        "stage": ("stage",),
        "blockers": ("blockers_open",),
        "round": ("round_complete", "round_head", "head") if value == "complete-at-head" else ("round_complete",),
        "fix-rounds": ("fix_rounds", "fix_cap"),
        "ci": ("ci",),
        "pr-state": ("pr_state",),
        "label": (),
        "pr": pr_requirements.get(value, ("pr_state",)) if isinstance(value, str) else ("pr_state",),
        "review": ("reviews", "head"),
        "ticket": ("issue_state",),
        "children": ("children_open",),
        "due": ("due",),
        "always": (),
        "actor": None,
        "focus": None,
    }
    return requirements.get(key, ())


def _condition_result(key, value, facts, event):
    required = _required_facts(key, value, event)
    if required is None:
        if key == "actor" and "actor" not in event:
            return "unknown", "event actor is missing"
        if key == "focus" and "focus" not in event and "focus_missing" not in facts:
            return "unknown", "focus evidence is missing"
    else:
        missing = [name for name in required if name not in facts]
        if key == "label" and not ("attrs" in facts or "pr_labels" in facts):
            missing.append("attrs or pr_labels")
        if missing:
            return "unknown", f"missing facts: {', '.join(missing)}"

    try:
        if key in _NODE_GUARDS:
            holds = gate.holds({key: value}, facts, event)
        else:
            # Plugin guards use the same registry and evaluator as the gate. If the plugin cannot
            # evaluate this partial fact set, its exception becomes an explicit unknown below.
            holds = gate.guard_holds({key: value}, facts, tracker=None, ticket=facts.get("ticket"))
    except (KeyError, TypeError, ValueError, gate.Bad, RuntimeError) as exc:
        return "unknown", f"guard could not be evaluated: {exc}"
    return ("matched", "condition holds") if holds else ("not-matched", "condition does not hold")


def _node_trigger(node):
    triggers = node.get("triggers", node.get("wakes_on", [])) or []
    if isinstance(triggers, str):
        return [triggers]
    return [str(trigger) for trigger in triggers]


def project_eligibility(node_id, node, facts: dict, events: list) -> dict:
    """Project trigger and ``when`` status; never infer that an eligible node is running."""
    node = node if isinstance(node, dict) else {}
    facts = facts if isinstance(facts, dict) else {}
    triggers = _node_trigger(node)
    observed = _event_parts(events, node_id)
    when = node.get("when") or {}
    if "state" in when and "stage" not in when:
        when = {("stage" if key == "state" else key): value for key, value in when.items()}
    conditions = []
    for key, value in when.items():
        result, reason = _condition_result(str(key), value, facts, observed[0] if observed else {})
        conditions.append({"key": str(key), "value": _text(value), "result": result, "reason": reason})

    if observed:
        trigger = "matched"
        event = observed[0]
        trigger_reason = "matching poll event was observed"
    elif triggers and facts.get("_poll_complete") is True \
            and not any(trigger == "manual" or trigger.startswith(("schedule ", "webhook "))
                        for trigger in triggers) and all(_poll_supported(trigger) for trigger in triggers):
        trigger = "not-matched"
        event = {}
        trigger_reason = "completed poll found no matching event"
    else:
        trigger = "unknown"
        event = {}
        trigger_reason = "no matching poll event was provided"

    # gate.matches is the source of truth for stale and duplicate event checks. Call it only
    # when the event supplies those checks and the corresponding facts are present.
    if observed and (event.get("head") is None or "head" in facts) \
            and (event.get("heads") is None or "heads" in facts) \
            and (event.get("id") is None or "seen" in facts):
        try:
            gate_node = {**node, "when": when}
            reason = gate.matches(gate_node, facts, event)
        except (KeyError, TypeError, ValueError, gate.Bad) as exc:
            reason = f"incomplete event facts: {exc}"
            trigger_reason = reason
        else:
            if reason in ("stale head", "duplicate event"):
                trigger = "not-matched"
                trigger_reason = reason

    results = [condition["result"] for condition in conditions]
    if trigger == "not-matched" or "not-matched" in results:
        eligibility = "not-matched"
    elif trigger == "matched" and all(result == "matched" for result in results):
        eligibility = "matched"
    else:
        eligibility = "unknown"

    reasons = [trigger_reason]
    reasons.extend(condition["reason"] for condition in conditions if condition["result"] != "matched")
    if eligibility == "matched":
        reason = "trigger and conditions match"
    else:
        reason = "; ".join(dict.fromkeys(reasons))
    return {"id": str(node_id), "trigger": trigger, "conditions": conditions,
            "eligibility": eligibility, "execution": "unknown", "reason": reason}

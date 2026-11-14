"""Stage age: how long a ticket has been in its current stage, measured from the latest stage entry.

A warning needs MORE than `WARN_AFTER` in an `active` stage. Waiting and final stages never warn. The entry time
comes from the stage label's latest `labeled` event (re-entering a stage resets it), never from `updated_at` or
comments. A missing entry time gives the state `unknown`.
"""
from datetime import datetime, timedelta, timezone

WARN_AFTER = timedelta(hours=1)


def parse_time(value):
    """A timezone-aware datetime of an ISO-8601 string (a `Z` suffix included), else None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def stage_entries(events, stages):
    """[{stage, at, actor}] in event order: each `labeled` event of a stage attr (`stages` {stage: attr}). A
    stage attr removed by `unlabeled` and added again is a new entry."""
    by_attr = {attr: name for name, attr in stages.items()}
    return [{"stage": by_attr[e["value"]], "at": e.get("at"), "actor": e.get("actor")}
            for e in events or [] if e.get("kind") == "labeled" and e.get("value") in by_attr]


def entered_at(events, stages, stage):
    """The time the ticket entered `stage` last, else None: the latest `labeled` event of its attr that no later
    `unlabeled` event removed (a missing or unreadable time is None)."""
    attr, at, held = stages.get(stage), None, False
    for e in events or []:
        if e.get("value") != attr:
            continue
        if e.get("kind") == "labeled":
            at, held = e.get("at"), True
        elif e.get("kind") == "unlabeled":
            at, held = None, False
    return at if held and parse_time(at) else None


def stage_age(stage, kind, entered, now):
    """{state, seconds, entered_at, kind, warn, reason} of a ticket in `stage` (`kind`: active|waiting|final|None).
    `state` is `ok`, `warn`, `exempt` (waiting or final stage) or `unknown` (no stage, or no entry time)."""
    base = {"stage": stage, "kind": kind, "entered_at": entered, "seconds": None, "warn": False}
    when = parse_time(entered)
    if kind in ("waiting", "final"):
        # Exempt from warnings, but the elapsed time is still reported (the completed-window filter needs it).
        seconds = max(0.0, (parse_time(now) - when).total_seconds()) if when else None
        return {**base, "state": "exempt", "seconds": seconds, "reason": f"{kind} stage: no age warning"}
    if when is None:
        return {**base, "state": "unknown", "reason": "stage entry time is missing"}
    seconds = max(0.0, (parse_time(now) - when).total_seconds())
    if kind != "active":
        return {**base, "state": "unknown", "seconds": seconds, "reason": "the stage has no kind"}
    warn = timedelta(seconds=seconds) > WARN_AFTER
    return {**base, "state": "warn" if warn else "ok", "seconds": seconds, "warn": warn,
            "reason": "more than one hour in an active stage" if warn else None}

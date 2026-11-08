"""The ticket effects a YouTrack store applies (ADR-0026 `(kind, target)` handlers, declared in `store.effects`).

A handler gets `(system, effect, ctx)`: `system` is the gate tracker that holds the store as its `tickets` and the
stage names as `stages`; `ctx["issue"]` is the ticket as read once per `effects.apply` (shared with the code host's
`close`, so `stage: done` reads the attrs from before the close). Every handler reads the ticket first and writes
only what differs.
"""
from factory_plugins.youtrack.store import MissingRepository


def _issue_once(system, n, ctx):
    if "issue" not in ctx:
        ctx["issue"] = system.tickets.issue(n)
    return ctx["issue"]


def stage(system, e, ctx):
    """The stage tag (or state field value) replaces the other stage attrs; a ticket whose repository field names
    no repository to wake is blocked, with a comment that says why, and waits for a person."""
    n, value = e["ticket"], e["value"]
    want, have = system.stages[value], [x for x in _issue_once(system, n, ctx)["attrs"] if x in system.stages.values()]
    try:
        return have != [want] and system.tickets.set_stage(n, want, have)
    except MissingRepository as err:
        system.tickets.comment(n, f"[factory] Blocked: {err}")
        return system.tickets.set_stage(n, system.stages["blocked"], have) or True


def _gone(tags, value):
    """`prefix-*` removes every tag with that prefix (a command tag of any tier)."""
    return [x for x in tags if x.startswith(value[:-1])] if value.endswith("*") else [value] if value in tags else []


def tag(system, e, ctx):
    """`label` adds the tag, `unlabel` removes it (`prefix-*`: every tag with the prefix)."""
    n, kind, value, on = e["ticket"], e["kind"], e["value"], e.get("on") or "issue"
    if on != "issue":
        raise ValueError(f"{kind}: bad target: {on}")
    tags = system.tickets.issue(n)["attrs"]
    if kind == "label":
        return value not in tags and system.tickets.edit_attrs(n, [value])
    gone = _gone(tags, value)
    return bool(gone) and system.tickets.edit_attrs(n, remove=gone)


def set_field(system, e, ctx):
    """An attribute tag `<field>:<value>`, one value per field: the gate reads the attribute back from the tags (the
    `risk` fact), so it is a tag, not a YouTrack custom field."""
    n, ((k, v),) = e["ticket"], e["value"].items()
    have = [x for x in system.tickets.issue(n)["attrs"] if x.startswith(f"{k}:")]
    want = f"{k}:{v}"
    return have != [want] and system.tickets.edit_attrs(n, [want] if want not in have else [],
                                                        [x for x in have if x != want])

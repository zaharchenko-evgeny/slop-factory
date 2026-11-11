"""The ticket effects a YouTrack store applies (ADR-0026 `(kind, target)` handlers, declared in `store.effects`).

A handler gets `(system, effect, ctx)`: `system` is the gate tracker that holds the store as its `tickets` and the
stage names as `stages`; `ctx["issue"]` is the ticket as read once per `effects.apply` (shared with the code host's
`close`, so `stage: done` reads the attrs from before the close). Every handler reads the ticket first and writes
only what differs.
"""
import re

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
        wrote = have != [want] and system.tickets.set_stage(n, want, have)
    except MissingRepository as err:
        system.tickets.comment(n, f"[factory] Blocked: {err}")
        system.tickets.set_stage(n, system.stages["blocked"], have)
        return _mirror(system, n, "blocked", e) or True
    return _mirror(system, n, value, e) or wrote


def _mirror(system, n, stage, e):
    """#1260: the shared State field follows the stage (`tracker.state_mirror`), unless a person's State chose it."""
    mirror = getattr(system.tickets, "mirror_state", None)
    return bool(mirror) and e.get("mirror", True) and mirror(n, stage)


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


def field(system, e, ctx):
    """A YouTrack custom field the outcome sets (`{name, value, type}`), written only while the field is empty: a
    value a person set stays (#1260, the router's Effort)."""
    n, v = e["ticket"], e["value"]
    if system.tickets.field_value(n, v["name"]) is not None:
        return False
    return system.tickets.set_field(n, v["name"], v["value"], v.get("type") or "SingleEnumIssueCustomField")


def resolve_field(mine, name, payload, facts, tracker, nodes, ticket):
    """Each `field` effect (`{name, from, sizes}`) with the value from the payload field `from`: the value itself,
    or with `sizes` ({value: upper limit or null}) the first value whose limit is above the number in it."""
    import gate
    out = []
    for e in mine:
        spec = e["value"]
        if spec["from"] not in payload:
            raise gate.Bad(f"payload: missing field `{spec['from']}`")
        value = payload[spec["from"]]
        if spec.get("sizes"):
            found = re.search(r"\d+", str(value))
            if not found:
                raise gate.Bad(f"payload: field `{spec['from']}` holds no number: {value!r}")
            lines = int(found[0])
            value = next((k for k, limit in sorted(spec["sizes"].items(), key=lambda x: (x[1] is None, x[1] or 0))
                          if limit is None or lines < limit), None)
            if value is None:
                raise gate.Bad(f"field {spec['name']}: no size holds {lines}")
        out.append({"kind": "field", "ticket": ticket, "value": {"name": spec["name"], "value": str(value),
                                                                 **({"type": spec["type"]} if spec.get("type") else {})}})
    return out, []

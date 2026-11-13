"""The `notify` effect (ADR-0022): `notify: message` or `notify: {message: message, people: [logins]}`, on the ticket or
(`on: pr`) its PR.

`resolve` is the `(notify, ticket)` and `(notify, pr)` effect handler: it reads the message from the payload before
anything is written. `send` (the `gate.send` hook) sends it once the outcome is sure to apply.
"""
import gate
from factory_plugins.notify import notifiers


def resolve(mine, name, payload, facts, tracker, nodes, ticket):
    """Each `notify` effect with the message the agent returned, checked; Bad names a missing or bad field."""
    def field(f):
        if f not in payload:
            raise gate.Bad(f"payload: missing field `{f}`")
        return payload[f]
    out = []
    for e in mine:
        message = {"issue": ticket, "problem": field("problem"), "links": payload.get("links", []),
                   "options": payload.get("options", []), "recommended": payload.get("recommended")}
        try:
            notifiers.validate_message(message)
        except ValueError as err:
            raise gate.Bad(f"notify: {err}")
        people = e["value"].get("people") if isinstance(e["value"], dict) else None
        out.append({**e, "value": {"message": message, "people": people}})
    return out, []


def notify_config():
    """`notify` of the team's factory.yaml; Bad when the team has none."""
    try:
        return notifiers.team_config()
    except ValueError as err:
        raise gate.Bad(str(err))


def send(effects, tracker, ticket, name):
    """Replace each `notify` effect with the one `[factory]` comment ADR-0022 asks for: the rendered text and one
    `factory:notify` marker per send. Returns the effects, the failures and the notify comments alone: the gate
    posts those comments even when a send failed (they list the sends that went out), then raises. A retry sends
    again: nothing reads the markers."""
    out, failures, posted = [], [], []
    for e in effects:
        if e["kind"] != "notify":
            out.append(e)
            continue
        message = {**e["value"]["message"], "pr": ""}
        if e.get("on") == "pr":
            message["pr"] = tracker.facts(ticket).get("pr") or ""
        try:
            sent, errors = notifiers.send_all(gate.plugin_registry(), notify_config(), message, e["value"]["people"])
        except ValueError as err:
            raise gate.Bad(str(err))
        failures += errors
        if sent:
            body = gate.effects_mod.tagged(notifiers.render(message) + "\n\nNotified: " + ", ".join(f"{p} via {v}" for v, p, _ in sent))
            body += "".join("\n" + notifiers.marker(v, p, r) for v, p, r in sent)
            if gate.definition_team():
                body += f"\n\n{gate.comment_marker(name)}"
            posted.append({"kind": "comment", "ticket": ticket, "value": body, **({"on": e["on"]} if e.get("on") else {})})
            out.append(posted[-1])
    return out, failures, posted

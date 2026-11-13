"""#591: an agent asks another agent in the thread, with a hop budget (ADR-0025 plugin)."""
import re

from effects import has_tag, tagged

ASK = re.compile(r"<!-- factory:ask id=(\S+) from=(\S+) to=(\S+) hop=(\d+) -->")
ANSWER = re.compile(r"<!-- factory:answer id=(\S+) -->")


def outcome(name, outcome, node, out, payload, tracker, ticket, effects):
    """Hook `gate.outcome`: the effects of the plugin's `ask` outcome."""
    return ask_effects(name, node, out, payload, tracker, ticket, effects) if outcome == "ask" else effects


def ask_effects(name, node, out, payload, tracker, ticket, effects):
    """The comment an `ask` outcome posts and the `factory:comment` label that wakes the comment node in the same
    apply (ADR-0023), which routes `@factory:<to>` to the target's reply step. Refused for a pair outside the
    outcome's `to` allowlist; over `limits.mention_hops` asks on the ticket it posts one comment naming the limit
    and wakes nothing."""
    import gate
    import vocab
    to = str(payload.get("to") or "")
    allowed = out.get("to") or []
    if to not in allowed:
        raise gate.Bad(f"ask: `{to}` is not an agent this node may ask; allowed: {', '.join(allowed) or 'none'}")
    from factory_plugins.comments.mentions import pr_comments
    facts = tracker.facts(ticket)
    pr = pr_comments(facts, tracker, ticket)
    asked = sum(1 for c in [*(facts.get("comments") or []), *(pr or [])]  # only the factory's own asks count
                if c.get("author_is_factory") and has_tag(c.get("body")) and ASK.search(c["body"]))
    limit = gate.config_mod.load().get("limits", {}).get("mention_hops", 3)
    if asked >= limit:
        return [{"kind": "comment", "ticket": ticket, "value": tagged(f"Ask refused: this ticket reached its limit of "
                                                          f"{limit} asks (`limits.mention_hops`). Go on without asking.")}]
    who = f"{gate.agent_of(name) if gate.definition_team() else node.get('agent', name)} ({node.get('role')})"
    marker = f"<!-- factory:ask id={ticket}-{asked + 1} from={name} to=reply-{to} hop={asked + 1} -->"
    text = " ".join(str(payload["text"]).split())
    on = {"on": effects[0]["on"]} if effects[0].get("on") else {}
    return [{**effects[0], "value": tagged(f"**{who}:** @factory:{to} {text}\n{marker}")},
            {"kind": "label", "ticket": ticket, "value": vocab.comment(), **on}]


def answered(facts, tracker, ticket):
    """The ask marker of the comment a reply step answers (the latest one naming @factory:), else None."""
    from factory_plugins.comments.mentions import latest
    return ASK.search((latest(facts, tracker, ticket) or {}).get("body") or "")


def answer_notes(notes, ask_id, text):
    """The answer's agent notes: the reply's own `notes` with the answer first in `context`, for any role, so the
    asker reads it in `agent_notes`. Over NOTE_MAX the reply's own context goes first, then the answer is cut."""
    from derive import clean_notes
    body = " ".join(str(text).split())
    own = {**(notes or {}), "for": "any"}
    while True:
        answer = f"Answer to ask {ask_id}: {body}"
        for note in ({**own, "context": "\n".join(filter(None, (answer, own.get("context"))))}, {**own, "context": answer}):
            try:
                return clean_notes(note)
            except ValueError:
                pass
        body = body[:len(body) // 2] + "..."


def wake(effects, name, payload, facts, tracker, nodes, ticket):
    """(effects, after) of a reply step's `wake: asker` effect: when its answer is to an agent's ask (#591), the
    asking node (`from=` in the ask marker) wakes again through its trigger label, removed and added so the tracker
    sends a new event. Its stage stays: when waking it needs another stage or its `when` fails, nothing wakes and a
    comment says why."""
    import gate
    from factory_plugins.comments.node import route_effects
    ask = answered(facts, tracker, ticket)
    if not ask or ask[3] != name:
        return [], []
    try:
        moved, stage = route_effects(ask[2], facts, ticket, nodes, payload.get("tier") or gate.fix_tier(facts), clean=False)
        why = None if stage == facts["stage"] else f"it runs at stage `{stage}`, the ticket is at `{facts['stage']}`"
    except gate.Bad as err:
        why = str(err)
    if why:
        text = tagged(f"The answer to ask {ask[1]} did not wake `{ask[2]}`: {why}.")
        if gate.definition_team():
            text += f"\n\n{gate.comment_marker(name)}"
        return [], [{"kind": "comment", "ticket": ticket, "value": text,
                     **({"run_id": payload["run_id"]} if payload.get("run_id") else {})}]
    return [{**e, "kind": "unlabel"} for e in moved] + moved, []

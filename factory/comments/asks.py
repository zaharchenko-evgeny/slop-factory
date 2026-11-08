"""#591: an agent asks another agent in the thread, with a hop budget (ADR-0025 module)."""
import re

ASK = re.compile(r"<!-- factory:ask id=(\S+) from=(\S+) to=(\S+) hop=(\d+) -->")
ANSWER = re.compile(r"<!-- factory:answer id=(\S+) -->")


def effects(name, node, out, payload, tracker, ticket, effects):
    """The comment an `ask` outcome posts. `@factory:<to>` wakes the target through the mention router, so it
    runs in reply mode. Refused for a pair outside the outcome's `to` allowlist; over `limits.mention_hops` asks on
    the ticket it posts one comment naming the limit and wakes nothing."""
    import gate
    to = str(payload.get("to") or "")
    allowed = out.get("to") or []
    if to not in allowed:
        raise gate.Bad(f"ask: `{to}` is not an agent this node may ask; allowed: {', '.join(allowed) or 'none'}")
    from comments.mentions import pr_comments
    facts = tracker.facts(ticket)
    pr = pr_comments(facts, tracker, ticket)
    asked = sum(1 for c in [*(facts.get("comments") or []), *(pr or [])]  # only the factory's own asks count
                if c.get("author_is_factory") and (c.get("body") or "").startswith("[factory]") and ASK.search(c["body"]))
    limit = gate.config_mod.load().get("limits", {}).get("mention_hops", 3)
    if asked >= limit:
        return [{"kind": "comment", "ticket": ticket, "value": f"[factory] Ask refused: this ticket reached its limit of "
                 f"{limit} asks (`limits.mention_hops`). Go on without asking."}]
    who = f"{gate.agent_of(name) if gate.definition_team() else node.get('agent', name)} ({node.get('role')})"
    marker = f"<!-- factory:ask id={ticket}-{asked + 1} from={name} to=reply-{to} hop={asked + 1} -->"
    text = " ".join(str(payload["text"]).split())
    return [{**effects[0], "value": f"[factory] **{who}:** @factory:{to} {text}\n{marker}"}]

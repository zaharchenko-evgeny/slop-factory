"""#591: an agent asks another agent in the thread, with a hop budget (ADR-0025 plugin)."""
import re

ASK = re.compile(r"<!-- factory:ask id=(\S+) from=(\S+) to=(\S+) hop=(\d+) -->")
ANSWER = re.compile(r"<!-- factory:answer id=(\S+) -->")


def outcome(name, outcome, node, out, payload, tracker, ticket, effects):
    """Hook `gate.outcome`: the effects of the plugin's `ask` outcome."""
    if outcome == "ask":
        return ask_effects(name, node, out, payload, tracker, ticket, effects)
    return answer_effects(name, payload, tracker, ticket, effects) if outcome == "replied" else effects


def ask_effects(name, node, out, payload, tracker, ticket, effects):
    """Post an ask comment and wake the comment node. Refuse an unlisted pair; over the hop limit, post a refusal."""
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
                if c.get("author_is_factory") and (c.get("body") or "").startswith("[factory]") and ASK.search(c["body"]))
    limit = gate.config_mod.load().get("limits", {}).get("mention_hops", 3)
    if asked >= limit:
        return [{"kind": "comment", "ticket": ticket, "value": f"[factory] Ask refused: this ticket reached its limit of "
                 f"{limit} asks (`limits.mention_hops`). Go on without asking."}]
    who = f"{gate.agent_of(name) if gate.definition_team() else node.get('agent', name)} ({node.get('role')})"
    marker = f"<!-- factory:ask id={ticket}-{asked + 1} from={name} to=reply-{to} hop={asked + 1} -->"
    text = " ".join(str(payload["text"]).split())
    return [{**effects[0], "value": f"[factory] **{who}:** @factory:{to} {text}\n{marker}"},
            {"kind": "label", "ticket": ticket, "value": vocab.comment()}]


def answer_effects(name, payload, tracker, ticket, effects):
    """Wake the node named by an ask marker and attach the answer as a note."""
    if not name.startswith("reply-"):
        return effects
    from factory_plugins.comments.mentions import pr_comments
    facts = tracker.facts(ticket)
    comments = [*(facts.get("comments") or []), *(pr_comments(facts, tracker, ticket) or [])]
    comments += facts.get("reviews") or []
    mentioned = [c for c in comments if "@factory:" in (c.get("body") or "")]
    latest = max(enumerate(mentioned), key=lambda ic: (ic[1].get("at") or ic[1].get("submitted_at") or "", ic[0]))[1] \
        if mentioned else None
    body = (latest or {}).get("body") or ""
    ask = ASK.search(body)
    if not ask or ask[3] != name or not latest.get("author_is_factory") or not body.startswith("[factory]"):
        return effects
    import config
    import definition
    graph = definition.load(config.team_root())
    source = ask[2]
    source_node = (graph["workflow"].get("nodes") or {}).get(source) or {}
    trigger = next((t for t in source_node.get("triggers") or [] if isinstance(t, dict) and t.get("label")), None)
    if not trigger:
        import gate
        raise gate.Bad(f"ask: node `{source}` has no label trigger to wake")
    label = trigger["label"]
    agents = graph["factory"].get("agents") or {}
    agent = agents.get(source_node.get("agent")) or {}
    role = agent.get("role") or source_node.get("role")
    notes = {"for": role if role in ("developer", "reviewer") else "any", "hint": str(payload.get("reply") or "")}
    for effect in effects:
        if effect.get("kind") == "comment":
            effect["notes"] = notes
    tier = next((suffix for suffix in ("small", "medium", "large", "low", "high")
                 if source.endswith(f"-{suffix}")), None)
    if "<tier>" in label and tier:
        label = label.replace("<tier>", tier)
    elif "<tier>" in label:
        import gate
        raise gate.Bad(f"ask: node `{source}` has no concrete label trigger to wake")
    on_pr = {e.get("label") or e.get("unlabel") for node in (graph["workflow"].get("nodes") or {}).values()
             for outcome in (node.get("outcomes") or {}).values()
             for e in (outcome or {}).get("effects") or [] if e.get("on") == "pr"}
    return [*effects, {"kind": "label", "ticket": ticket, "value": label, **({"on": "pr"} if label in on_pr else {})}]

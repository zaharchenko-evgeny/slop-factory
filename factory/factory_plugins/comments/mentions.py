"""#590: `@factory:<agent>` in a comment wakes that agent in reply mode (ADR-0021 layout, ADR-0025 plugin).

`add_nodes(defn)` (hook `definition.load`) adds the generated reply nodes when the plugin is on; the comment node
(ADR-0023) routes a comment to one with its `reply` effect. `errors(defn)` (hook `definition.errors`) checks what
only the plugin allows; `holds(agent, ...)` is the `mention` guard fact; `latest` and `reply_target` (the `comment`
effect `on: mention`) tell a reply step who asked it and where.
"""
import re


def neighbours(defn):
    """{frozenset({a, b})}: agents a and b are neighbours when an outcome of a node of one adds a label that a node
    of the other triggers on (they may mention each other). Generated mention nodes do not count."""
    nodes = [n for n in (defn["workflow"].get("nodes") or {}).values() if not n.get("generated")]
    woken = {}
    for n in nodes:
        for t in n.get("triggers") or []:
            if isinstance(t, dict) and "label" in t:
                woken.setdefault(t["label"], set()).add(n.get("agent"))
    pairs = set()
    for n in nodes:
        for oc in (n.get("outcomes") or {}).values():
            for e in (oc or {}).get("effects") or []:
                for b in woken.get(e.get("label"), ()):
                    if b and b != n.get("agent"):
                        pairs.add(frozenset((n.get("agent"), b)))
    return pairs


def add_nodes(defn):
    """An agent with `mentionable: true` can be asked with @factory:<agent>. Add, in memory, a `reply-<agent>` node
    per mentionable agent (its role in reply mode, read-only, any stage, marked `generated`): its answer to an
    agent's ask wakes the asker (`wake: asker`)."""
    f, w = defn["factory"], defn["workflow"]
    agents, nodes = f.setdefault("agents", {}), w.setdefault("nodes", {})
    prefix = (w.get("stages") or {}).get("label_prefix", "factory:")
    askable = [a for a, spec in agents.items() if (spec or {}).get("mentionable")]
    if not askable:
        return
    pairs = neighbours(defn)
    for n in nodes.values():  # an `ask` outcome (#591): the agents its agent may ask, the deploy-time allowlist
        if n.get("agent") in askable and n.get("mention") is not False and not n.get("generated") \
                and not all(isinstance(t, dict) and "schedule" in t for t in n.get("triggers") or [None]):
            to = sorted(b for b in askable if frozenset((n["agent"], b)) in pairs)
            n.setdefault("outcomes", {}).setdefault("ask", {"to": to, "effects": [{"comment": "text"}]})
    for a in askable:
        nodes.setdefault(f"reply-{a}", {
            "agent": a, "generated": True, "triggers": [{"label": f"{prefix}reply-{a}"}], "output": "read-only",
            "settings": {"mode": "reply"},
            "outcomes": {"replied": {"effects": [{"unlabel": f"{prefix}reply-{a}"}, {"comment": "reply", "on": "mention"},
                                                 {"wake": "asker"}]}}})


def errors(defn):
    """The plugin's checks of a definition: a mentionable agent needs a role; its node names are reserved."""
    from definition import NO_ROLE as no_role
    errs = []
    for a, spec in (defn["factory"].get("agents") or {}).items():
        spec = spec or {}
        if spec.get("mentionable") and (spec.get("run") or spec.get("harness") in no_role):
            errs.append(f"mentionable-needs-role: agents.{a}: only an agent with a role can reply")
    for name, n in (defn["workflow"].get("nodes") or {}).items():
        if name.startswith("reply-") and not n.get("generated"):
            errs.append(f"reserved-node: nodes.{name}: `reply-<agent>` is generated from `mentionable`")
    return errs


def pr_comments(facts, tracker, ticket):
    """The PR comments in `facts`, else read from the tracker (None when it has none)."""
    pr = facts.get("pr_comments")
    if pr is None and tracker is not None and hasattr(tracker, "pr_comments"):
        pr = tracker.pr_comments(ticket)
    return pr


def latest(facts, tracker=None, ticket=None):
    """{by, body, at, on, url} of the latest comment or review on the ticket or its PR that names @factory:, else None."""
    found = [dict(c, on="ticket") for c in facts.get("comments") or []]
    found += [dict(c, on="pr") for c in pr_comments(facts, tracker, ticket) or []]
    found += [{"author": r.get("author"), "body": r.get("body"), "at": r.get("submitted_at"), "on": "pr"}
              for r in facts.get("reviews") or [] if r.get("body")]
    found = [c for c in found if "@factory:" in (c.get("body") or "")]
    if not found:
        return None
    c = max(enumerate(found), key=lambda ic: (ic[1].get("at") or "", ic[0]))[1]
    return {"by": c.get("author"), "body": c.get("body"), "at": c.get("at"), "on": c["on"], "url": c.get("url")}


def holds(agent, facts, tracker=None, ticket=None):
    """The latest comment on the ticket or its PR names `@factory:<agent>`, and may ask it. A person may ask any
    agent; an agent (its comment's `factory:from` marker) only a neighbour in workflow.yaml; a reply never asks
    anyone, so two agents cannot answer each other forever."""
    import config as config_mod
    import definition
    from gate import FROM
    from factory_plugins.comments.asks import ANSWER, ASK
    comments = list(facts.get("comments") or [])
    comments += pr_comments(facts, tracker, ticket) or []
    comments += [{"body": r.get("body") or "", "at": r.get("submitted_at") or r.get("at")}
                 for r in facts.get("reviews") or [] if r.get("body")]  # a reviewer asks in its review
    if not comments:
        return False
    newest = max(enumerate(comments), key=lambda ic: (ic[1].get("at") or "", ic[0]))[1]
    body = newest.get("body") or ""
    if not re.search(rf"@factory:{re.escape(agent)}(?![\w-])", body):
        return False
    marker = FROM.search(body)
    if not marker:
        return True  # a person
    if marker[2]:  # a reply asks no one, except an answer to an ask, which goes back to the asker (#591)
        answer = ANSWER.search(body)
        ask = next((c for c in comments if answer and (m := ASK.search(c.get("body") or "")) and m[1] == answer[1]), None)
        asker = FROM.search(ask["body"]) if ask else None
        return bool(asker and asker[1] == agent)
    if marker[1] == agent:
        return False
    return frozenset((marker[1], agent)) in neighbours(definition.load(config_mod.team_root()))


def reply_target(e, facts, tracker, ticket):
    """A reply's `comment` effect with `on: mention`: post it where the question was, to whoever asked; an answer
    to an agent's ask carries the answer in its agent notes for the asker."""
    from gate import FROM
    from factory_plugins.comments.asks import ASK, answer_notes
    asked = latest(facts, tracker, ticket)
    e["on"] = "pr" if asked and asked["on"] == "pr" else None
    if not e["on"]:
        del e["on"]
    if (ask := ASK.search((asked or {}).get("body") or "")) and (asker := FROM.search(asked["body"])):
        e["notes"] = answer_notes(e.get("notes"), ask[1], e["value"])
        e["value"] = f"@factory:{asker[1]} {e['value']}\n<!-- factory:answer id={ask[1]} -->"  # #591: back to the asker
    elif asked and asked.get("by"):
        e["value"] = f"@{asked['by']} {e['value']}"

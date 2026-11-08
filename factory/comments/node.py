"""ADR-0023: one comment node routes person comments and agent asks (ADR-0025 module).

`step_input` lists, for the step, the comments after the cursor; `moves` turns the node's `route`, `reply` and `skip`
effects into the writes that move the item, remove the wake label and post the reply with the new cursor.
"""
import json
import re
from pathlib import Path

import vocab
from comments import EFFECTS
from comments.asks import ASK

SEEN = re.compile(r"<!-- factory:comment-seen (\d+) -->")


def is_comment_node(node):
    return any(next((k for k in e if k != "on"), None) in EFFECTS
               for o in node["outcomes"].values() for e in (o or {}).get("effects") or [])


def cursor(comments):
    """The highest id in a `factory:comment-seen` marker of the factory's own comments (0: none). Only
    `author_is_factory` counts; another integration cannot move the cursor."""
    return max((int(m[1]) for c in comments if c.get("author_is_factory")
                and (c.get("body") or "").startswith("[factory]") and (m := SEEN.search(c["body"]))), default=0)


def new_comments(facts, tracker=None, ticket=None):
    """The comments on the ticket and its PR after the cursor, oldest first, `on` saying where each is. The factory's
    own comments (a `factory:from` marker without an ask marker) are not input."""
    from gate import FROM
    from comments.mentions import pr_comments
    pr = pr_comments(facts, tracker, ticket)
    found = [dict(c, on="ticket") for c in facts.get("comments") or []] + [dict(c, on="pr") for c in pr or []]
    seen = cursor(found)
    own = lambda c: FROM.search(c.get("body") or "") and not ASK.search(c.get("body") or "")
    return sorted((c for c in found if int(c.get("id") or 0) > seen and not own(c)), key=lambda c: int(c["id"]))


def input_path(ticket):
    return Path(".factory") / "check" / f"{ticket}-comments.json"


def save_input(ticket, comments):
    """The newest comment id the step's context lists: `apply` acknowledges nothing after it."""
    path = input_path(ticket)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ticket": ticket, "last": max((int(c["id"]) for c in comments), default=0)}))


def input_last(ticket):
    """The saved id, or None when no `check` ran (every comment is then input)."""
    try:
        state = json.loads(input_path(ticket).read_text())
        return int(state["last"]) if str(state.get("ticket")) == str(ticket) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def step_input(node, facts, tracker, ticket):
    """What a comment node's step reads: the comments after the cursor, saved so `apply` acknowledges only those."""
    if not is_comment_node(node):
        return
    listed = new_comments(facts, tracker, ticket)
    save_input(ticket, listed)
    facts["new_comments"] = [{k: c.get(k) for k in ("id", "on", "author", "author_is_bot", "author_is_factory", "at", "body")}
                             for c in listed]


def route_effects(to, facts, ticket, nodes, tier, clean):
    """The effects that move the item to node `to`: its trigger label and the stage its `when` needs, both from
    workflow.yaml. `clean` also drops the person's wait and the stale command labels. Refuses (Bad, nothing
    written) an unknown node, one without a label trigger, or one whose `when` fails with the new stage and label."""
    import gate
    node = (nodes.get("nodes") or {}).get(to)
    if node is None or node.get("enabled") is False:
        raise gate.Bad(f"route: unknown node `{to}`")
    trigger = next((m for t in node.get("triggers") or [] if isinstance(t, str)
                    and (m := re.fullmatch(r"(issues|pull_request)\.labeled (\S+)", t))), None)
    if not trigger:
        raise gate.Bad(f"route: node `{to}` has no label trigger")
    label, on = trigger[2].replace("<tier>", tier), "pr" if trigger[1] == "pull_request" else None
    if "<" in label:
        raise gate.Bad(f"route: the trigger label of `{to}` needs a value: {label}")
    when = node.get("when") or {}
    stages = [when["stage"]] if isinstance(when.get("stage"), str) else list(when.get("stage") or [])
    stage = facts["stage"] if facts["stage"] in stages or not stages else stages[0]
    held = {**facts, "stage": stage, **{"pr_labels" if on else "attrs": [*facts.get("pr_labels" if on else "attrs", []), label]}}
    if not gate.holds({k: v for k, v in when.items() if k != "stage"}, held, {}):
        raise gate.Bad(f"route: node `{to}` does not match this item now (its `when` fails)")
    out = gate.clear_commands(ticket) + [{"kind": "unlabel", "ticket": ticket, "value": vocab.needs_human(), **extra}
                                         for extra in ({}, {"on": "pr"})] if clean else []
    out.append({"kind": "label", "ticket": ticket, "value": label, **({"on": on} if on else {})})
    if stage != facts["stage"]:
        out.append({"kind": "stage", "ticket": ticket, "value": stage})
    return out, stage


def moves(effects, name, payload, facts, tracker, nodes, ticket):
    """(effects, after) for the comment node's route, reply and skip effects. `effects` move the item; `after`
    always runs: remove the wake label, react on a skipped comment, post a short reply with the cursor."""
    import gate
    found = new_comments(facts, tracker, ticket)
    last = input_last(ticket)
    fresh = [c for c in found if last is None or int(c["id"]) <= last]  # what the step read
    later = {c["on"] for c in found if c not in fresh}  # arrived during the run: still pending
    tier = payload.get("tier") or gate.fix_tier(facts)
    out, summary = [], "Nothing to do."
    for e in effects:
        if e["kind"] == "skip":
            if fresh:
                out.append({"kind": "react", "ticket": ticket, "value": "eyes", "comment": fresh[-1]["id"],
                            **({"on": "pr"} if fresh[-1]["on"] == "pr" else {})})
            continue
        route = e["kind"] == "route"
        to = e["value"] if route else f"reply-{e['value']}"
        more, stage = route_effects(to, facts, ticket, nodes, tier, clean=route)
        out += more
        summary = f"Moved to `{to}`, stage `{stage}`." if route else f"Asked `{e['value']}` to answer."
    text = " ".join(str(payload.get("reply") or summary).split())
    if fresh:
        text += f"\n<!-- factory:comment-seen {fresh[-1]['id']} -->"
    if gate.definition_team():
        text += f"\n\n{gate.comment_marker(name)}"
    after = [{"kind": "unlabel", "ticket": ticket, "value": vocab.comment(), **extra} for extra in ({}, {"on": "pr"})]
    after += [{"kind": "label", "ticket": ticket, "value": vocab.comment(), **({"on": "pr"} if on == "pr" else {})}
              for on in sorted(later)]  # the label added again wakes the node for the later comments
    after.append({"kind": "comment", "ticket": ticket, "value": text, **({"on": "pr"} if fresh and fresh[-1]["on"] == "pr" else {}),
                  **({"run_id": payload["run_id"]} if payload.get("run_id") else {})})
    return out, after

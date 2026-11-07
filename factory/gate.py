#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The gate: decide whether a node runs (check) and apply an outcome's effects (apply).

Usage: gate.py check --node N --event FILE [--tracker github|memory:FILE]
       gate.py apply --node N --outcome O --payload FILE [--tracker github|memory:FILE]
       gate.py apply --node N --handoff FILE [--event FILE]   (outcome and payload derived from the handoff)
       --spec FILE (check, apply, sweep): the node spec of the automation (deploy plan); no engine/nodes.yaml is read
       gate.py sweep [--tracker github] [--dry-run] [--stale] [--event FILE | --issue N] (no agent: lists the tickets itself,
                                                                 one stage per ticket;
                                                                 --stale also re-fires nodes whose run died silently)
       gate.py ci-sync [--dry-run]   (factory:ci-green / factory:ci-red on the open factory PRs of every `role: project`
                                      repository, from their head check runs and commit statuses; `factory reconcile`
                                      runs it on its schedule)
Exit 0 go / done, 10 no go (stale, duplicate or the node no longer matches), 2 bad event, payload, node
or outcome, 1 a failing effect (the stage is untouched: it is the last effect).
An event is JSON {id, ticket, head?, actor?}. A payload is JSON {ticket, event?, head?, actor?, ...}
plus one field per outcome effect that needs data (the error names the field).
`check --event` also takes the raw trigger payload (an `issues`, `pull_request` or `pull_request_review`
event): the ticket is the issue number or the `<n>` of the PR branch `ez/<n>-*` (none: exit 10).
`--tracker memory:FILE` runs on a JSON file {ticket: record} with the in-memory fake; `--tracker github`
uses factory/gate_github.py (bot login from `targets.air.bot_login`).
"""
import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod  # noqa: E402
import nodes as nodes_mod  # noqa: E402
import notify as notifiers  # noqa: E402
from gate_github import BAD_CI, GOOD_CI, GitHubCodeHost, GitHubGateTracker, clean_notes  # noqa: E402
from gate_space import SpaceCodeHost, from_config as space_from_config, space_event  # noqa: E402
from gate_port import MemoryTracker, ticket_from_ref  # noqa: E402
from github_tracker import project_repositories  # noqa: E402
import vocab  # noqa: E402

ROOT = nodes_mod.ROOT
GO, NO_GO, BAD, FAILED = 0, 10, 2, 1
AGENT_KEYS = ("agent", "persona", "mentions", "mentions_on")  # node spec fields of a project with `agents:`


class Bad(Exception):
    pass


def value(want, allowed):
    if want not in allowed:
        raise Bad(f"bad condition value {want!r}: allowed {', '.join(allowed)}")


def holds(when, facts, event):
    """Every key of `when` must hold (the vocabulary is nodes.WHEN_KEYS)."""
    def one(key, want):
        if key == "stage":
            return facts["stage"] in (want if isinstance(want, list) else [want])
        if key == "blockers":
            return (facts["blockers_open"] == 0) == (want == "closed")
        if key == "focus":
            miss = facts["focus_missing"]
            focus = event.get("focus", miss[0] if miss else None)
            return focus in miss or event.get("rereview") == focus
        if key == "round":
            value(want, ("complete", "complete-at-head"))
            return facts["round_complete"] and (want != "complete-at-head" or facts["round_head"] == facts["head"])
        if key == "fix-rounds":
            value(want, ("below-cap", "at-cap"))
            return (facts["fix_rounds"] < facts["fix_cap"]) == (want == "below-cap")
        if key == "ci":
            if isinstance(want, list):  # any of these
                return facts["ci"] in want
            value(want, ("green", "red", "pending", "none"))
            return facts["ci"] == want
        if key == "actor":
            return event.get("actor") == want
        if key == "pr-state":
            value(want, ("open", "merged", "closed", "none"))
            return facts.get("pr_state", "none") == want
        if key == "always":
            return bool(want)
        if key == "label":  # ADR-0021: every label listed is on the ticket or its PR (an `on: pr` label)
            have = set(facts.get("attrs") or []) | set(facts.get("pr_labels") or [])
            return all(x in have for x in (want if isinstance(want, list) else [want]))
        if key == "pr":
            value(want, ("open", "merged", "behind", "conflicting", "none"))
            return {"open": facts.get("pr_state") == "open", "merged": facts.get("pr_state") == "merged",
                    "behind": bool(facts.get("behind")), "conflicting": bool(facts.get("conflict")),
                    "none": facts.get("pr_state", "none") == "none"}[want]
        if key == "review":
            value(want, ("approved-at-head", "changes-at-head", "none-at-head"))
            return review_at_head(facts) == want.removesuffix("-at-head").replace("approved", "approve")
        if key == "ticket":
            value(want, ("open", "closed"))
            return facts.get("issue_state", "open") == want
        if key == "children":
            value(want, ("open", "closed"))
            return ("children_open" in facts) and (facts["children_open"] == 0) == (want == "closed")
        return bool(facts["due"]) == bool(want)  # due
    return all(one(k, v) for k, v in when.items())


def spec_nodes(path):
    """A `nodes` mapping from a node spec file: the spec's node and the script nodes it `runs`."""
    try:
        spec = json.loads(Path(path).read_text())
        out = {}
        for s in [spec, *(spec.get("runs") or {}).values()]:
            node = {k: s[k] for k in ("role", "when", "outcomes", "else_tier", "tiers", "routine", *AGENT_KEYS) if k in s}
            node.update({"tier": s["tier"]} if s.get("tier") else {}, **({"focuses": [s["focus"]]} if s.get("focus") else {}))
            out[s["node"]] = node
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as e:
        raise Bad(f"malformed spec: {e!r}")
    return {"nodes": out}


def load_node(name, nodes):
    node = (nodes.get("nodes") or {}).get(name)
    if node is None or node.get("enabled") is False:
        raise Bad(f"unknown node: {name}")
    return node


class NoTicket(Exception):
    pass


class MissingNumber(Exception):
    pass


def pr_ticket(code_host, number, repository=None):
    """The ticket of PR `number`, looked up in the payload's repository when it names one (ADR-0017)."""
    name = repository.get("full_name") if isinstance(repository, dict) else None
    return code_host.ticket_for_pr(number, name) if name else code_host.ticket_for_pr(number)


def from_trigger(d, code_host=None):
    """Extract the event kind and ticket number; tracker facts supply mutable metadata."""
    event_name = d.get("eventType") or d.get("event_name")
    if str(event_name or "").startswith("space.") or isinstance(d.get("merge_request"), dict) \
            or isinstance((d.get("data") or {}).get("merge_request"), dict):
        ev = space_event(d)
        ticket = ev["ticket"] or (code_host.ticket_for_pr(ev["number"], ev["project"]) if code_host and ev["project"] else None)
        if ticket is None:
            raise NoTicket(f"merge request {ev['number']} is not on an ez/<n>- branch")
        return {"id": f"merge_request:{ticket}:{ev['action']}", "ticket": ticket, "event_name": "pull_request", "action": ev["action"]}
    if not any(isinstance(d.get(key), dict) for key in ("pull_request", "issue", "review")):
        if isinstance(d.get("data"), dict):
            wrapper = d
            d = d["data"]
            event_name = event_name or wrapper.get("eventType") or wrapper.get("event_name")
    pr, issue, review = d.get("pull_request"), d.get("issue"), d.get("review")
    pr_comment = isinstance(issue, dict) and isinstance(issue.get("pull_request"), dict) and not isinstance(pr, dict)
    is_pr = isinstance(pr, dict) or pr_comment or str(event_name or "").startswith("pull_request")
    src = pr or issue or d
    number = src.get("number") if isinstance(src, dict) else None
    if not isinstance(number, int) or number < 1:
        raise MissingNumber("payload has no issue or PR number")
    name = "pull_request_review" if review and is_pr else "issue_comment" if pr_comment \
        else "pull_request" if is_pr else "issues"  # a comment on a PR arrives as an issue event of its number
    if is_pr:
        ticket = pr_ticket(code_host, number, d.get("repository")) if code_host else None
        if ticket is None and not isinstance(code_host, GitHubCodeHost):  # a live lookup is the only source for GitHub
            ticket = ticket_from_ref(((pr or {}).get("head") or {}).get("ref")) or ticket_from_ref((pr or {}).get("title"))
        if ticket is None:
            raise NoTicket(f"PR #{number} is not on an ez/<n>- branch")
    else:
        ticket = number
    action = d.get("action") or "event"
    return {"id": f"{name}:{ticket}:{action}", "ticket": ticket, "event_name": name, "action": action}


def read(path, what, raw=False, code_host=None):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise Bad(f"malformed {what}: {e}")
    if isinstance(data, dict) and raw and "ticket" not in data:
        return from_trigger(data, code_host)
    if not isinstance(data, dict) or "ticket" not in data:
        raise Bad(f"malformed {what}: needs an object with `ticket`")
    return data


NOTE_AUDIENCE = {"developer": ("developer",), "reviewer": ("reviewer",)}  # the `for` values a role reads, besides `any`
COMPLEXITY_TIER = {"small": "cheap", "medium": "mid", "large": "strong"}
FIX_TIERS = ("cheap", "mid", "strong")


def slices(split):
    """The proposed slices of a router `split` as {title, body} children: a list of {title, body} objects (a
    per-repository child also names its `repo`, `subsystem` and the repositories it is `after`), else
    lines (a string, or a list of strings) whose text is both title and body."""
    if isinstance(split, list) and all(isinstance(x, dict) for x in split):
        return [{"title": str(x["title"]).strip(), "body": str(x.get("body") or x["title"]).strip(),
                 **{k: x[k] for k in ("repo", "subsystem", "after") if x.get(k)}} for x in split]
    lines = split.splitlines() if isinstance(split, str) else [str(x) for x in split or []]
    out = [x.strip().lstrip("-").strip() for x in lines]
    return [{"title": x.split(" — ")[0].strip(), "body": x} for x in out if x and x != "none"]


def intake_fields(h):
    """Payload fields the outcomes need and the handoff names otherwise.
    With a `size_estimate` (the router's intake): complexity and tier from the size, `questions` from `reason`,
    `children` from `slices`."""
    if "size_estimate" not in h:
        return {}
    size = re.search(r"\d+", str(h["size_estimate"]))
    n = int(size[0]) if size else 0
    proposed = h.get("proposed_checks")
    proposed = "\n".join(f"- {x}" for x in proposed) if isinstance(proposed, list) else str(proposed or "")
    proposal = (f"\n\nProposed acceptance checks (accept by editing the ticket, or by `{vocab.stage('triage')}` with this pasted):\n"
                f"{proposed}" if proposed.strip() not in ("", "none") else "")
    return {"complexity": "small" if n <= 150 else "medium" if n <= 400 else "large",
            "tier": "cheap" if n <= 150 else "mid" if n <= 400 else "strong",
            "questions": f"[factory] Needs answers before work starts: {h.get('reason', '')}{proposal}",
            "children": slices(h.get("slices"))}


def outcome_of(h):
    """The node outcome a handoff names: `outcome`, else a reviewer `verdict`."""
    if h.get("outcome"):
        return h["outcome"]
    if "verdict" in h:
        verdict = h["verdict"]
        verdicts = {"approve": "approve", "approved": "approve", "changes": "changes",
                    "changes-requested": "changes"}
        if not isinstance(verdict, str) or verdict not in verdicts:
            raise Bad(f"unknown reviewer verdict: {verdict!r}")
        return verdicts[verdict]
    return h.get("status")


LESSON_MARKER = "<!-- factory:lesson -->"


def lesson_effects(ticket, node, payload):
    """ADR-0021: a result's `lessons` as one marked comment on the ticket and the lesson label, which retro collects.
    Each lesson is `{text, scope: project|factory|engine, about, evidence}`; only `text` is required."""
    lessons = payload.get("lessons") or []
    if not isinstance(lessons, list) or not all(isinstance(x, dict) and str(x.get("text") or "").strip() for x in lessons):
        raise Bad("lessons must be a list of objects with `text`")
    if not lessons:
        return []
    lines = [f"- [{x.get('scope', 'project')}] {x.get('about') or 'general'}: {' '.join(str(x['text']).split())}"
             + (f" (evidence: {x['evidence']})" if x.get("evidence") else "") for x in lessons]
    body = f"[factory] Lessons from `{node}`\n{LESSON_MARKER}\n" + "\n".join(lines)
    if payload.get("run_id"):
        body += f"\n\nFactory-Run: {payload['run_id']}"
    return [{"kind": "comment", "ticket": ticket, "value": body}, {"kind": "label", "ticket": ticket, "value": vocab.lesson()}]


def agent_of(node):
    """The agent of workflow node `node` in a definition team's factory (ADR-0021), else the node's own name."""
    import definition
    defn = definition.load(config_mod.team_root())
    return ((defn["workflow"].get("nodes") or {}).get(node) or {}).get("agent", node)


def comment_marker(node):
    """`<!-- factory:from <agent>[ reply] -->`: which agent wrote a comment, so a mention in it can be checked."""
    import definition
    reply = definition.settings(definition.load(config_mod.team_root()), node).get("mode") == "reply"
    return f"<!-- factory:from {agent_of(node)}{' reply' if reply else ''} -->"


def node_mode(node):
    """A definition team's node `settings.mode` (fix, update, reply, ...), or None (ADR-0021)."""
    import definition
    return definition.settings(definition.load(config_mod.team_root()), node).get("mode")


def definition_team():
    """True when the team defines its factory in factory.yaml + workflow.yaml (ADR-0021)."""
    import definition
    return definition.present(config_mod.team_root())


def ci_state(tracker, ticket, wait_seconds=0, poll=30, sleep=None):
    """{ci, head, failing}: CI at the ticket's PR head, after waiting up to `wait_seconds` for it to conclude
    (ADR-0021: a reviewer runs `factory ci --wait` before its verdict, so no CI event has to wake anyone)."""
    import time
    sleep = sleep or time.sleep
    waited = 0
    while True:
        facts = tracker.facts(ticket)
        if facts.get("ci") in ("green", "red") or waited >= wait_seconds:
            break
        sleep(poll)
        waited += poll
    failing = tracker.ci_failure(facts["head"]) if facts.get("ci") == "red" and facts.get("head") \
        and hasattr(tracker, "ci_failure") else None
    return {"ci": facts.get("ci"), "head": facts.get("head"), "failing": failing}


def decide(name, ticket, tracker, nodes, dry_run=False):
    """ADR-0021 `factory decide`: apply the first outcome of node `name` whose guards hold (`from`, `ci`, `if`), for a
    node whose agent only runs this command. (status, {outcome, ...}); no outcome holding is NO_GO."""
    node = load_node(name, nodes)
    facts = tracker.facts(ticket)
    for outcome, out in node["outcomes"].items():
        if out.get("enabled") is False or (out.get("from") and facts["stage"] not in out["from"]) \
                or (out.get("ci") and facts["ci"] not in out["ci"]) \
                or (out.get("if") and not guard_holds(out["if"], facts, tracker, ticket)):
            continue
        if dry_run:  # the outcome whose guards hold, nothing written
            return GO, {"outcome": outcome, "applied": False, "dry_run": True}
        payload = {"ticket": ticket, "reason": f"[factory] {name}: {outcome}"}
        status, res = apply(name, outcome, payload, tracker, nodes)
        return status, {"outcome": outcome, **res}
    return NO_GO, {"outcome": None, "applied": False, "reason": "no outcome's guards hold"}


def merge_refused(effects, facts):
    """ADR-0021: a `merge` effect keeps its own invariants, whatever chose it: the current head is approved by the
    agent reviews at it, none asks for changes, CI is green there and the branch is not behind. None when it may
    merge, else the reason. The old layout's merge node checks its gates before it gets here."""
    if not any(e["kind"] == "merge" for e in effects) or not definition_team():
        return None
    if review_at_head(facts) != "approve":
        return "merge refused: no approving review at the current head"
    if facts.get("ci") != "green":
        return f"merge refused: CI is {facts.get('ci')} at the current head"
    if facts.get("behind"):
        return "merge refused: the branch is behind its base (update it first)"
    return None


def unblocked(effects, tracker, facts):
    """ADR-0021, automatic: when this outcome moves the ticket to a final stage, each ticket it blocks that is
    `ready` with no other open blocker gets `ready` removed and added again, which fires its trigger (re-adding a
    present label fires nothing). `reconcile` does the same for a ticket a person closed."""
    if not definition_team() or not hasattr(tracker, "blocking"):
        return []
    final = {s for s, k in (config_mod.load()["tracker"].get("stage_kinds") or {}).items() if k == "final"}
    to = next((e["value"] for e in effects if e["kind"] == "stage"), None)
    if to not in final:
        return []
    ticket, label, out = next(e["ticket"] for e in effects if e["kind"] == "stage"), vocab.stage("ready"), []
    for d in tracker.blocking(ticket):
        n = d["number"] if isinstance(d, dict) else d
        f = tracker.facts(n)
        others = f.get("blockers_open", 0) - (1 if facts.get("issue_state", "open") == "open" else 0)
        if f["stage"] == "ready" and others <= 0:
            out += [{"kind": "unlabel", "ticket": n, "value": label}, {"kind": "label", "ticket": n, "value": label}]
    return out


def group_unlabels(effects, facts):
    """ADR-0021: a label `<group>:<value>` outside the stage namespace holds one value per group; adding one removes
    the group's other values (`risk:high` removes `risk:low`) on the same ticket or PR."""
    labels = [e for e in effects if e["kind"] == "label" and ":" in str(e.get("value"))]
    if not labels:
        return []
    stages = config_mod.load()["tracker"].get("stages") or {}
    namespaces = {str(v).partition(":")[0] for v in stages.values() if ":" in str(v)}
    out = []
    for e in labels:
        group = str(e["value"]).partition(":")[0]
        if group in namespaces:
            continue
        have = facts.get("pr_labels" if e.get("on") == "pr" else "attrs") or []
        out += [{"kind": "unlabel", "ticket": e["ticket"], "value": x, **({"on": e["on"]} if e.get("on") else {})}
                for x in have if x.startswith(f"{group}:") and x != e["value"]]
    return out


def review_at_head(facts):
    """`approve` when agent reviews at the current head all approve, `changes` when one asks for changes, else `none`."""
    verdicts = [r.get("verdict") for r in facts.get("reviews") or [] if r.get("agent") and r.get("commit_id") == facts.get("head")]
    if not verdicts:
        return "none"
    return "changes" if any(v != "approve" for v in verdicts) else "approve"


def guard_holds(guard, facts, tracker=None, ticket=None):
    """True when the ticket holds an outcome's `if` guard (ADR-0021): the same facts as `when`."""
    if "children" in guard and "children_open" not in facts and tracker is not None and hasattr(tracker, "children"):
        kids = tracker.children(ticket) or []
        facts = {**facts, "children_open": sum(1 for c in kids if c.get("state") == "open")}
    unknown = set(guard) - GUARD_KEYS
    if unknown:
        raise Bad(f"unknown guard: {', '.join(sorted(unknown))}")
    if "mention" in guard:
        if not mention_holds(guard["mention"], facts, tracker, ticket):
            return False
        guard = {k: v for k, v in guard.items() if k != "mention"}
    return holds(guard, facts, {})


def latest_mention(facts, tracker=None, ticket=None):
    """{by, body, at, on, url} of the latest comment or review on the ticket or its PR that names @factory:, else None."""
    found = [dict(c, on="ticket") for c in facts.get("comments") or []]
    pr = facts.get("pr_comments")
    if pr is None and tracker is not None and hasattr(tracker, "pr_comments"):
        pr = tracker.pr_comments(ticket)
    found += [dict(c, on="pr") for c in pr or []]
    found += [{"author": r.get("author"), "body": r.get("body"), "at": r.get("submitted_at"), "on": "pr"}
              for r in facts.get("reviews") or [] if r.get("body")]
    found = [c for c in found if "@factory:" in (c.get("body") or "")]
    if not found:
        return None
    c = max(enumerate(found), key=lambda ic: (ic[1].get("at") or "", ic[0]))[1]
    return {"by": c.get("author"), "body": c.get("body"), "at": c.get("at"), "on": c["on"], "url": c.get("url")}


def mention_holds(agent, facts, tracker=None, ticket=None):
    """ADR-0021 #590: the latest comment on the ticket or its PR names `@factory:<agent>`, and may ask it. A person
    may ask any agent; an agent (its comment's `factory:from` marker) only a neighbour in workflow.yaml; a reply
    never asks anyone, so two agents cannot answer each other forever."""
    comments = list(facts.get("comments") or [])
    pr_comments = facts.get("pr_comments")
    if pr_comments is None and tracker is not None and hasattr(tracker, "pr_comments"):
        pr_comments = tracker.pr_comments(ticket)
    comments += pr_comments or []
    comments += [{"body": r.get("body") or "", "at": r.get("submitted_at") or r.get("at")}
                 for r in facts.get("reviews") or [] if r.get("body")]  # a reviewer asks in its review
    if not comments:
        return False
    latest = max(enumerate(comments), key=lambda ic: (ic[1].get("at") or "", ic[0]))[1]
    body = latest.get("body") or ""
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
    import definition
    return frozenset((marker[1], agent)) in definition.neighbours(definition.load(config_mod.team_root()))


GUARD_KEYS = {"stage", "label", "pr", "ci", "review", "blockers", "children", "ticket", "mention"}
FROM = re.compile(r"<!-- factory:from (\S+?)( reply)? -->")


def fix_tier(facts, default="mid"):
    """The tier of the ticket's `complexity:*` label (`default` without one)."""
    for label in facts.get("attrs", []):
        if label.startswith("complexity:"):
            return COMPLEXITY_TIER.get(label[11:], default)
    return default


def review_wake_labels(facts, completed_head=None):
    """The per-focus reviewer commands needed for this green PR head."""
    ci = "green" if completed_head and facts.get("head") == completed_head else facts.get("ci")
    if facts.get("stage") != "in-review" or ci != "green":
        return []
    return [vocab.cmd("review-<focus>", focus) for focus in facts.get("focus_missing", [])]


def branch_of(facts, config):
    """The ticket's branch: the PR's head, else `branch_pattern` from the issue title."""
    if facts.get("branch"):
        return facts["branch"]
    body = facts.get("body") or ""
    explicit = re.search(r"^slug:[ \t]*([a-z0-9][a-z0-9-]*)[ \t]*$", body, re.M)
    title = facts.get("title") or ""
    words = [word for word in re.findall(r"[a-z0-9]+", title.lower()) if word not in {"a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "with", "from", "by"}]
    slug = explicit.group(1) if explicit else "-".join(words[:4]) or "ticket"
    return config["branch_pattern"].format(ticket=facts["ticket"], slug=slug)


def matches(node, facts, event, outcome=None):
    """None when the node matches, else the reason it does not."""
    if event.get("head") is not None and event["head"] != facts["head"]:
        return "stale head"
    if event.get("heads") is not None and event["heads"] != facts.get("heads"):
        return "stale head"  # a ticket with PRs in several repositories: any of them moved
    if event.get("id") is not None and event["id"] in facts["seen"]:
        return "duplicate event"
    if outcome and outcome.get("from") and facts["stage"] not in outcome["from"]:
        return f"stage {facts['stage']} is not in the outcome's from"
    return None if holds(node["when"], facts, event) else "condition does not hold"


def check(name, event, tracker, nodes, config, code_host=None):
    node = load_node(name, nodes)
    if "id" not in event:
        raise Bad("malformed event: needs `id`")
    facts = tracker.facts(event["ticket"])
    if isinstance(code_host, SpaceCodeHost):  # Space owns the merge request: its CI and review facts replace the tracker's
        space = code_host.facts(event["ticket"])
        if space["pr"] is not None:
            facts = {**facts, **{k: space[k] for k in ("pr", "head", "branch", "pr_state", "ci", "dry_run", "pr_labels", "reviews", "round_complete", "round_verdict", "round_head")}}
    if "event_name" in event:
        event["id"] = f"{event['event_name']}:{event['ticket']}:{event['action']}:{facts.get('updated_at', '')}"
        event["head"] = facts.get("head")
        if facts.get("heads"):
            event["heads"] = facts["heads"]
        event["actor"] = facts.get("event_actor")
        pending = facts.get("pending_rereviews", [])
        labels = facts.get("pr_labels", [])
        event["rereview"] = next(iter(pending), None) or next(
            (x.removeprefix(vocab.prefix("rereview-<focus>")) for x in labels
             if x.startswith(vocab.prefix("rereview-<focus>"))), None)
    why = matches(node, facts, event)
    branch = branch_of({**facts, "ticket": event["ticket"]}, config)
    notes = facts.get("agent_notes", [])
    if comment_node(node):  # ADR-0023: what the comment node reads, after the cursor
        listed = new_comments(facts, tracker, event["ticket"])
        save_comment_input(event["ticket"], listed)
        facts["new_comments"] = [{k: c.get(k) for k in ("id", "on", "author", "author_is_bot", "at", "body")}
                                 for c in listed]
    facts = {k: v for k, v in facts.items() if k not in ("body", "agent_notes")}
    role, roles = node["role"], config["roles"]
    tiers = node.get("tiers") or [node.get("tier") or roles.get(role, {}).get("tier")]
    if node.get("focuses"):  # one review tier per focus (roles.<role>.modes.<focus>.tier)
        tiers = [roles[role]["modes"][f]["tier"] for f in node["focuses"]]
    skills = config_mod.resolve(f"roles/{role}/skills.txt")
    fixing = definition_team() and node_mode(name) in ("fix", "update")
    return (NO_GO if why else GO), {
        "go": why is None, "reason": why, "ticket": event["ticket"], "node": name, "role": role,
        "tier": event.get("tier") if event.get("tier") in tiers else tiers[0],
        "focus": event.get("focus"),
        "role_file": config_mod.shown(f"roles/{role}/role.md") if role in roles else None,
        "skills": skills.read_text().split() if skills.exists() else [],
        "context": facts, "outcomes": list(node["outcomes"]),
        "agent_notes": [n for n in notes if n["for"] in ("any", *NOTE_AUDIENCE.get(role, ()), *(("fixer",) if fixing else ()))],
        **({"mention": latest_mention(facts, tracker, event["ticket"])} if name.startswith("reply-") and definition_team() else {}),
        "merge_main": bool(node["role"] == "developer" and facts.get("conflict") and fixing),
        "branch": branch, "handoff_path": f".factory/handoff/{name}.json",
        # one entry per repository of a ticket that changes several (ADR-0017), all on the ticket's one branch
        "checkouts": [{"repo": p["repo"], "branch": p["branch"] or branch} for p in facts.get("prs") or []],
        **{k: node[k] for k in AGENT_KEYS if k in node},  # who the role is and whom it may address (ADR-0020)
    }


def check_routine(name, nodes, config):
    """A routine (a scheduled node outside any ticket, ADR-0015): no tracker facts, so it always matches."""
    node = load_node(name, nodes)
    role, roles = node["role"], config["roles"]
    skills = config_mod.resolve(f"roles/{role}/skills.txt")
    return GO, {
        "go": True, "reason": None, "ticket": None, "node": name, "role": role,
        "tier": node.get("tier") or roles.get(role, {}).get("tier"), "focus": None,
        "role_file": config_mod.shown(f"roles/{role}/role.md") if role in roles else None,
        "skills": skills.read_text().split() if skills.exists() else [],
        "context": {}, "outcomes": list(node["outcomes"]), "merge_main": False, "branch": None,
        "handoff_path": f".factory/handoff/{name}.json", **{k: node[k] for k in AGENT_KEYS if k in node},
    }


def apply_routine(name, outcome, nodes):
    """A routine writes through its role (a branch, a PR): the gate only checks the outcome and changes nothing."""
    outcomes = load_node(name, nodes)["outcomes"]
    if outcome == "done" and "done" not in outcomes:  # the role handoff's `status: done` means the routine's one outcome
        outcome = ""
    if not outcome and len(outcomes) == 1:
        outcome = next(iter(outcomes))
    if outcome not in outcomes:
        raise Bad(f"unknown outcome: {outcome}")
    return GO, {"applied": True, "written": []}


def resolve(effect, payload, ticket):
    """(kind, value) of one effect; a missing payload field raises Bad naming it."""
    (kind, arg), = [(k, v) for k, v in effect.items() if k != "on"]
    on = effect.get("on")

    def field(f):
        if f not in payload:
            raise Bad(f"payload: missing field `{f}`")
        return payload[f]
    if kind == "set-field":
        return {"kind": kind, "ticket": ticket, "value": {arg: field(arg)}}
    if kind in ("label", "unlabel"):
        out = {"kind": kind, "ticket": ticket, "value": re.sub(r"<([^>]+)>", lambda m: str(field(m[1])), arg)}
        return {**out, "on": on} if on else out
    if kind in ("assign", "unassign"):
        if kind == "assign" and arg == "owner":
            arg = config_mod.owner()
        out = {"kind": kind, "ticket": ticket, "value": arg}
        return {**out, "on": on} if on else out
    if kind in ("comment", "create-issues"):
        out = {"kind": kind, "ticket": ticket, "value": field(arg)}
        return {**out, "on": on} if on else out
    if kind in ("park-behind-main-red", "route", "reply"):
        return {"kind": kind, "ticket": ticket, "value": field(arg)}
    if kind == "notify":  # `notify: message` or `notify: {message: message, people: [logins]}` (ADR-0022)
        message = {"issue": ticket, "problem": field("problem"), "links": payload.get("links", []), "options": payload.get("options", []), "recommended": payload.get("recommended")}
        try:
            notifiers.validate_message(message)
        except ValueError as e:
            raise Bad(f"notify: {e}")
        return {"kind": kind, "ticket": ticket, "value": {"message": message, "people": arg.get("people") if isinstance(arg, dict) else None},
                **({"on": on} if on else {})}
    return {"kind": kind, "ticket": ticket, "value": arg}  # stage, review, merge, join


ASK = re.compile(r"<!-- factory:ask id=(\S+) from=(\S+) to=(\S+) hop=(\d+) -->")
ANSWER = re.compile(r"<!-- factory:answer id=(\S+) -->")


def notify_config():
    """`notify` of the team's factory.yaml; Bad when the team has none."""
    import definition
    config = (definition.load(config_mod.team_root())["factory"] or {}).get("notify")
    if not config:
        raise Bad("notify: the team's factory.yaml has no `notify` config")
    return config


def latest_run(comments):
    """The `Factory-Run:` value of the latest `[factory]` stage comment (not a notify comment), else None."""
    for c in reversed(comments or []):
        body = c.get("body") or ""
        if body.startswith("[factory]") and "<!-- factory:notify " not in body and (m := re.search(r"(?m)^Factory-Run:\s*(\S+)\s*$", body)):
            return m[1]
    return None


def notify_effects(effects, tracker, ticket, name, run_id=None):
    """Replace each `notify` effect with the one `[factory]` comment ADR-0022 asks for: the rendered text and one
    `factory:notify` marker per send. The comment is posted even when a send failed (it lists the sends that went
    out); the failures then raise. Returns the effects, the failures and the notify comments alone. A retry sends
    again: nothing reads the markers."""
    out, failures, posted = [], [], []
    for e in effects:
        if e["kind"] != "notify":
            out.append(e)
            continue
        facts = tracker.facts(ticket)
        message = {**e["value"]["message"], "pr": ""}
        if e.get("on") == "pr":
            message["pr"] = facts.get("pr") or ""
        repo = getattr(tracker, "pr_repo" if message["pr"] else "repo", None)
        message["url"] = f"https://github.com/{repo}/{'pull' if message['pr'] else 'issues'}/{message['pr'] or ticket}" if repo else ""
        message["run_url"] = latest_run(facts.get("comments")) or run_id or ""
        try:
            sent, errors = notifiers.send_all(notify_config(), message, e["value"]["people"])
        except ValueError as err:
            raise Bad(str(err))
        failures += errors
        if sent:
            body = "[factory] " + notifiers.render(message) + "\n\nNotified: " + ", ".join(f"{p} via {v}" for v, p, _ in sent)
            body += "".join("\n" + notifiers.marker(v, p, r) for v, p, r in sent)
            if definition_team():
                body += f"\n\n{comment_marker(name)}"
            posted.append({"kind": "comment", "ticket": ticket, "value": body, **({"on": e["on"]} if e.get("on") else {})})
            out.append(posted[-1])
    return out, failures, posted


def ask_effects(name, node, out, payload, tracker, ticket, effects):
    """#591: the comment an `ask` outcome posts. `@factory:<to>` wakes the target through the mention router, so it
    runs in reply mode. Refused for a pair outside the outcome's `to` allowlist; over `limits.mention_hops` asks on
    the ticket it posts one comment naming the limit and wakes nothing."""
    to = str(payload.get("to") or "")
    allowed = out.get("to") or []
    if to not in allowed:
        raise Bad(f"ask: `{to}` is not an agent this node may ask; allowed: {', '.join(allowed) or 'none'}")
    facts = tracker.facts(ticket)
    pr = facts.get("pr_comments")
    if pr is None and hasattr(tracker, "pr_comments"):
        pr = tracker.pr_comments(ticket)
    asked = sum(1 for c in [*(facts.get("comments") or []), *(pr or [])]  # only the factory's own asks count
                if c.get("author_is_bot") and (c.get("body") or "").startswith("[factory]") and ASK.search(c["body"]))
    limit = config_mod.load().get("limits", {}).get("mention_hops", 3)
    if asked >= limit:
        return [{"kind": "comment", "ticket": ticket, "value": f"[factory] Ask refused: this ticket reached its limit of "
                 f"{limit} asks (`limits.mention_hops`). Go on without asking."}]
    who = f"{agent_of(name) if definition_team() else node.get('agent', name)} ({node.get('role')})"
    marker = f"<!-- factory:ask id={ticket}-{asked + 1} from={name} to=reply-{to} hop={asked + 1} -->"
    text = " ".join(str(payload["text"]).split())
    return [{**effects[0], "value": f"[factory] **{who}:** @factory:{to} {text}\n{marker}"}]


COMMENT_EFFECTS = ("route", "reply", "skip")  # the effects of the comment node's outcomes (ADR-0023)
SEEN = re.compile(r"<!-- factory:comment-seen (\d+) -->")


def comment_cursor(comments, bot=None):
    """The highest id in a `factory:comment-seen` marker of the factory's own comments (0: none). With the tracker's
    `bot` login known, only that author counts; another integration cannot move the cursor."""
    return max((int(m[1]) for c in comments if (c.get("author") == bot if bot else c.get("author_is_bot"))
                and (c.get("body") or "").startswith("[factory]") and (m := SEEN.search(c["body"]))), default=0)


def new_comments(facts, tracker=None, ticket=None):
    """The comments on the ticket and its PR after the cursor, oldest first, `on` saying where each is. The factory's
    own comments (a `factory:from` marker without an ask marker) are not input."""
    pr = facts.get("pr_comments")
    if pr is None and tracker is not None and hasattr(tracker, "pr_comments"):
        pr = tracker.pr_comments(ticket)
    found = [dict(c, on="ticket") for c in facts.get("comments") or []] + [dict(c, on="pr") for c in pr or []]
    cursor = comment_cursor(found, getattr(tracker, "bot", None))
    own = lambda c: FROM.search(c.get("body") or "") and not ASK.search(c.get("body") or "")
    return sorted((c for c in found if int(c.get("id") or 0) > cursor and not own(c)), key=lambda c: int(c["id"]))


def comment_input_path(ticket):
    return Path(".factory") / "check" / f"{ticket}-comments.json"


def save_comment_input(ticket, comments):
    """The newest comment id the step's context lists: `apply` acknowledges nothing after it."""
    path = comment_input_path(ticket)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ticket": ticket, "last": max((int(c["id"]) for c in comments), default=0)}))


def comment_input_last(ticket):
    """The saved id, or None when no `check` ran (every comment is then input)."""
    try:
        state = json.loads(comment_input_path(ticket).read_text())
        return int(state["last"]) if str(state.get("ticket")) == str(ticket) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def comment_node(node):
    return any(next((k for k in e if k != "on"), None) in COMMENT_EFFECTS
               for o in node["outcomes"].values() for e in (o or {}).get("effects") or [])


def route_effects(to, facts, ticket, nodes, tier, clean):
    """The effects that move the item to node `to`: its trigger label and the stage its `when` needs, both from
    workflow.yaml. `clean` also drops the person's wait and the stale command labels. Refuses (Bad, nothing
    written) an unknown node, one without a label trigger, or one whose `when` fails with the new stage and label."""
    node = (nodes.get("nodes") or {}).get(to)
    if node is None or node.get("enabled") is False:
        raise Bad(f"route: unknown node `{to}`")
    trigger = next((m for t in node.get("triggers") or [] if isinstance(t, str)
                    and (m := re.fullmatch(r"(issues|pull_request)\.labeled (\S+)", t))), None)
    if not trigger:
        raise Bad(f"route: node `{to}` has no label trigger")
    label, on = trigger[2].replace("<tier>", tier), "pr" if trigger[1] == "pull_request" else None
    if "<" in label:
        raise Bad(f"route: the trigger label of `{to}` needs a value: {label}")
    when = node.get("when") or {}
    stages = [when["stage"]] if isinstance(when.get("stage"), str) else list(when.get("stage") or [])
    stage = facts["stage"] if facts["stage"] in stages or not stages else stages[0]
    held = {**facts, "stage": stage, **{"pr_labels" if on else "attrs": [*facts.get("pr_labels" if on else "attrs", []), label]}}
    if not holds({k: v for k, v in when.items() if k != "stage"}, held, {}):
        raise Bad(f"route: node `{to}` does not match this item now (its `when` fails)")
    out = clear_commands(ticket) + [{"kind": "unlabel", "ticket": ticket, "value": vocab.needs_human(), **extra}
                                    for extra in ({}, {"on": "pr"})] if clean else []
    out.append({"kind": "label", "ticket": ticket, "value": label, **({"on": on} if on else {})})
    if stage != facts["stage"]:
        out.append({"kind": "stage", "ticket": ticket, "value": stage})
    return out, stage


def comment_moves(moves, name, payload, facts, tracker, nodes, ticket):
    """(effects, after) for the comment node's route, reply and skip effects (ADR-0023). `effects` move the item;
    `after` always runs: remove the wake label, react on a skipped comment, post a short reply with the cursor."""
    found = new_comments(facts, tracker, ticket)
    last = comment_input_last(ticket)
    fresh = [c for c in found if last is None or int(c["id"]) <= last]  # what the step read
    later = {c["on"] for c in found if c not in fresh}  # arrived during the run: still pending
    tier = payload.get("tier") or fix_tier(facts)
    effects, summary = [], "Nothing to do."
    for e in moves:
        if e["kind"] == "skip":
            if fresh:
                effects.append({"kind": "react", "ticket": ticket, "value": "eyes", "comment": fresh[-1]["id"],
                                **({"on": "pr"} if fresh[-1]["on"] == "pr" else {})})
            continue
        route = e["kind"] == "route"
        to = e["value"] if route else f"reply-{e['value']}"
        more, stage = route_effects(to, facts, ticket, nodes, tier, clean=route)
        effects += more
        summary = f"Moved to `{to}`, stage `{stage}`." if route else f"Asked `{e['value']}` to answer."
    text = " ".join(str(payload.get("reply") or summary).split())
    if fresh:
        text += f"\n<!-- factory:comment-seen {fresh[-1]['id']} -->"
    if definition_team():
        text += f"\n\n{comment_marker(name)}"
    after = [{"kind": "unlabel", "ticket": ticket, "value": vocab.comment(), **extra} for extra in ({}, {"on": "pr"})]
    after += [{"kind": "label", "ticket": ticket, "value": vocab.comment(), **({"on": "pr"} if on == "pr" else {})}
              for on in sorted(later)]  # the label added again wakes the node for the later comments
    after.append({"kind": "comment", "ticket": ticket, "value": text, **({"on": "pr"} if fresh and fresh[-1]["on"] == "pr" else {}),
                  **({"run_id": payload["run_id"]} if payload.get("run_id") else {})})
    return effects, after


def apply(name, outcome, payload, tracker, nodes, focus=None, tier=None):
    node = load_node(name, nodes)
    if outcome not in node["outcomes"]:
        raise Bad(f"unknown outcome: {outcome}" + (" (a reply cannot ask)" if outcome == "ask" and name.startswith("reply-") else ""))
    if outcome == "done" and any(c.get("result") == "fail" for c in payload.get("checks", [])):
        raise Bad("done with a failed check is refused")
    out = node["outcomes"][outcome]
    if out.get("enabled") is False:
        raise Bad(f"disabled outcome: {outcome}")
    ticket = payload["ticket"]
    payload = {**{k: v for k, v in (("focus", focus), ("tier", tier)) if v}, **payload}  # the automation's own
    tiers = node.get("tiers") or []
    if tiers and payload.get("tier") in tiers and "next tier" not in payload:  # the successor comes from the trusted tier list
        i = tiers.index(payload["tier"]) + 1
        if i < len(tiers):
            payload = {**payload, "next tier": tiers[i]}
        elif outcome == "escalate" and "unresolvable" in node["outcomes"]:  # no higher tier: a person decides
            outcome = "unresolvable"
            payload = {**payload, "reason": payload.get("reason") or f"[factory] Blocked: the {payload['tier']} tier could not resolve it and no higher tier exists. A person decides."}
    out = node["outcomes"][outcome]
    if out.get("if") and not guard_holds(out["if"], tracker.facts(ticket), tracker, ticket):
        if out.get("else") not in node["outcomes"]:
            raise Bad(f"outcome {outcome}: its guard does not hold")
        out = node["outcomes"][out["else"]]  # ADR-0021: e.g. automatic merge refused for a high-risk ticket
    if out.get("ci") and tracker.facts(ticket)["ci"] not in out["ci"] and out.get("else") in node["outcomes"]:
        out = node["outcomes"][out["else"]]  # e.g. all reviews approve but CI is red: a fix round
        if "tier" not in payload:  # the agent saw `approved`; the fix label needs the cheapest fix tier
            payload = {**payload, "tier": node.get("else_tier") or nodes["nodes"]["fix"]["tiers"][0]}
    if any("set-field" in e and e["set-field"] == "risk" for e in out.get("effects") or []):
        payload = {"risk": tracker.facts(ticket)["risk"], **payload}  # the gate's fact: the ticket's risk label
    effects = [resolve(e, payload, ticket) for e in out.get("effects") or []]  # validates the payload first
    if outcome == "ask":
        effects = ask_effects(name, node, out, payload, tracker, ticket, effects)
    if payload.get("agent_notes"):  # hidden notes for later agents ride on the review, the comments or the PR text
        try:
            notes = clean_notes(payload["agent_notes"])
        except ValueError as err:
            raise Bad(str(err))
        for e in effects:
            if e["kind"] in ("review", "comment"):
                e["notes"] = notes
    facts = tracker.facts(ticket)
    comment_after = []
    if any(e["kind"] in COMMENT_EFFECTS for e in effects):
        moved, comment_after = comment_moves([e for e in effects if e["kind"] in COMMENT_EFFECTS], name, payload, facts,
                                             tracker, nodes, ticket)
        effects = [e for e in effects if e["kind"] not in COMMENT_EFFECTS] + moved
    after = []  # effects that must follow the stage change
    starts = [e for e in effects if e["kind"] == "label" and str(e["value"]).startswith(commands())]
    stop = starts and stop_reason(facts, fix=any(str(e["value"]).startswith(vocab.prefix("fix-<tier>")) for e in starts))
    if stop:
        # The transition that would start a developer run it may not start blocks the ticket instead; a check
        # that refused later would stop it with no comment and no event to wake it.
        effects = [{"kind": "comment", "ticket": ticket, "value": stop},
                   {"kind": "stage", "ticket": ticket, "value": "blocked"}]
        # The route did not happen: no success reply and no cursor move, so a person's comment stays unread.
        comment_after = [e for e in comment_after if e["kind"] == "unlabel"]
    if any(e["kind"] == "stage" and e["value"] == "blocked" for e in effects):
        effects = clear_commands(ticket) + effects
    stage_effect = next((e for e in effects if e["kind"] == "stage"), None)
    if stage_effect and not after:  # the join reads the new stage (nothing-to-fix: in-review)
        after = [{**e, "tier": payload.get("tier") or fix_tier(facts)} for e in effects if e["kind"] == "join"]
        effects = [e for e in effects if e["kind"] != "join"]
    if stage_effect:
        target = stage_effect["value"]
        human_effects = []
        human_state = target in ("blocked", "needs-answers")
        if not human_state and vocab.needs_human() in facts.get("attrs", []):
            human_effects += [{"kind": "unlabel", "ticket": ticket, "value": vocab.needs_human()},
                              {"kind": "unassign", "ticket": ticket, "value": None}]
        if human_state:
            person = (facts.get("author") if target == "needs-answers" else
                      config_mod.owner())
            human_effects += [{"kind": "label", "ticket": ticket, "value": vocab.needs_human()},
                              {"kind": "assign", "ticket": ticket, "value": person},
                              {"kind": "label", "ticket": ticket, "value": vocab.needs_human(), "on": "pr"},
                              {"kind": "assign", "ticket": ticket, "value": person, "on": "pr"}]
        elif vocab.needs_human() in facts.get("attrs", []) or target in ("ready", "triage", "approved", "done"):
            human_effects += [{"kind": "unlabel", "ticket": ticket, "value": vocab.needs_human(), "on": "pr"},
                              {"kind": "unassign", "ticket": ticket, "value": None, "on": "pr"}]
        effects[:0] = human_effects
    after = [*after, *comment_after, *unblocked(effects, tracker, facts)]
    effects[:0] = group_unlabels(effects, facts)
    effects.sort(key=lambda e: e["kind"] == "stage")  # the stage change goes last
    for e in effects:
        if e["kind"] == "stage":
            e["event"] = payload.get("event")
        if e["kind"] == "review":
            # ADR-0021: a definition team has no focuses; its review is named after the node that wrote it
            e["focus"] = payload.get("focus") or name
            e["round"] = payload.get("round")
            e["findings"], e["summary"], e["run_id"] = payload.get("findings"), payload.get("summary"), payload.get("run_id")
        if e["kind"] == "join":
            e["tier"] = payload.get("tier")
        if e["kind"] in ("merge", "update-branch"):
            e["head"] = facts.get("head")  # the head the gates judged
            if facts.get("heads"):
                e["heads"] = facts["heads"]  # one per repository of a multi-repository ticket
        if e["kind"] == "comment" and payload.get("run_id"):
            e["run_id"] = payload["run_id"]
        if e["kind"] == "comment" and e.get("on") == "mention":  # #590: answer where, and whom, the question came from
            asked = latest_mention(facts, tracker, ticket)
            e["on"] = "pr" if asked and asked["on"] == "pr" else None
            if not e["on"]:
                del e["on"]
            if (ask := ASK.search((asked or {}).get("body") or "")) and (asker := FROM.search(asked["body"])):
                e["value"] = f"@factory:{asker[1]} {e['value']}\n<!-- factory:answer id={ask[1]} -->"  # #591: back to the asker
            elif asked and asked.get("by"):
                e["value"] = f"@{asked['by']} {e['value']}"
        if e["kind"] == "comment" and definition_team():
            e["value"] = f"{e['value']}\n\n{comment_marker(name)}"
        if e["kind"] == "review" and definition_team():
            e["summary"] = f"{e.get('summary') or ''}\n\n{comment_marker(name)}"
    if not any(e["kind"] == "stage" for e in effects) and payload.get("event") is not None:
        effects.append({"kind": "seen", "ticket": ticket, "value": payload["event"]})
    why = matches(node, facts, {**payload, "id": payload.get("event")}, out) \
        or merge_refused(effects, facts)
    if why:
        return NO_GO, {"applied": False, "reason": why}
    after = [*after, *lesson_effects(ticket, name, payload)]  # after the outcome's effects: never its notification text
    effects, failures, posted = notify_effects(effects, tracker, ticket, name, payload.get("run_id"))
    if failures:  # keep the markers of the sends that went out; skip the stage change and the other effects
        for e in posted:
            tracker.apply(e)
        raise RuntimeError("notify failed: " + "; ".join(failures))
    written = [e["kind"] for e in effects + after if tracker.apply(e)]
    parent = parent_decision(name, ticket, effects, tracker, nodes)
    return GO, {"applied": True, "written": written, **({"parent": parent} if parent else {})}


def parent_decision(name, ticket, effects, tracker, nodes):
    """ADR-0021, automatic: when this outcome moves a ticket to a final stage, the same node decides on its parent
    (a split ticket's `children` guard), since nothing else wakes the parent when its last child closes."""
    if not definition_team() or not hasattr(tracker, "parent"):
        return None
    final = {s for s, k in (config_mod.load()["tracker"].get("stage_kinds") or {}).items() if k == "final"}
    if next((e["value"] for e in effects if e["kind"] == "stage"), None) not in final:
        return None
    p = tracker.parent(ticket)
    if not p or tracker.facts(p)["stage"] in final:
        return None
    if not any("children" in (o.get("if") or {}) for o in load_node(name, nodes)["outcomes"].values()):
        return None
    return {"ticket": p, **decide(name, p, tracker, nodes)[1]}


SWEEP_STAGES = ("ready", "approved", "split", "in-review", "needs-answers")


def commands():
    """Label prefixes of the developer commands: each starts a developer run."""
    declared = config_mod.load()["tracker"].get("commands") or {}  # a definition team has none of these (ADR-0021)
    return tuple(vocab.prefix(c) for c in ("implement-<tier>", "fix-<tier>", "update-<tier>") if c in declared)


def clear_commands(ticket):
    """Effects that remove every command label. A stopped ticket keeps none, so the first command after a
    person's factory:ready is a new label and fires its node (re-adding a present label fires nothing)."""
    declared = config_mod.load()["tracker"].get("commands") or {}
    if "implement-<tier>" not in declared:  # a definition team (ADR-0021): its routing labels on ticket and PR
        return [{"kind": "unlabel", "ticket": ticket, "value": next(iter(store.values())),
                 **({"on": "pr"} if "pr_label" in store else {})}
                for name, store in declared.items() if ":" not in name]
    return [{"kind": "unlabel", "ticket": ticket, "value": vocab.cmd("implement-<tier>", "*")},
            {"kind": "unlabel", "ticket": ticket, "value": vocab.cmd("fix-<tier>", "*"), "on": "pr"},
            {"kind": "unlabel", "ticket": ticket, "value": vocab.cmd("update-<tier>", "*"), "on": "pr"}]


def stop_reason(facts, fix=False):
    """Why no further developer run may start for this ticket, else None: the spend guard, and for a fix
    round the fix-round cap (`limits.fix_rounds`)."""
    if facts.get("agent_runs", 0) >= facts.get("run_cap", 8):
        return (f"[factory] Stopped: {facts['agent_runs']} developer runs since the last {vocab.stage('ready')} reached "
                f"limits.agent_runs_per_ticket. A person decides, then sets {vocab.stage('ready')}.")
    if fix and facts.get("fix_rounds", 0) >= facts.get("fix_cap", 3):
        return (f"[factory] Stopped: {facts['fix_rounds']} fix rounds reached limits.fix_rounds and the review "
                f"still requests changes. A person decides, then sets {vocab.stage('ready')}.")
    return None


def join_decision(facts):
    """The join's outcome and payload, or None while the round is open. Uses the adapter's `round_verdict`,
    which counts every required agent focus, human change requests and CI. A fix label takes the ticket's
    complexity tier (mid when unlabelled)."""
    rereviewed = has_dispute_rereview(facts)
    if (facts.get("handed_to_fix") and not rereviewed) or facts.get("pending_rereviews") \
            or not facts.get("round_complete") or facts.get("round_verdict") is None:
        return None  # an open round, or a head whose round already went to a fix (its push comes next)
    return ("changes" if facts["round_verdict"] == "changes" else "approved"), {"tier": fix_tier(facts)}


def has_dispute_rereview(facts):
    return any(r.get("agent") and r.get("rereview") and r.get("commit_id") == facts.get("head")
               for r in facts.get("reviews", []))


RISK_RANK = {"low": 0, "high": 1}


def merge_decision(facts, auto_merge):
    """(outcome, payload) of the `merge` node, else None while a gate fails: the current head approved by every required
    focus, green CI at it, no conflict. Then `waiting` (auto-merge off or risk above `max_risk`), `updated` (behind) or `merged`."""
    if auto_merge is None or facts.get("stage") != "approved" or facts.get("pr_state") != "open" or facts.get("conflict") \
            or facts.get("ci") != "green" or not facts.get("round_complete") or facts.get("round_verdict") != "approve" \
            or facts.get("round_head") is None or facts["round_head"] != facts.get("head"):
        return None
    if not auto_merge.get("enabled") or RISK_RANK.get(facts.get("risk", "high"), 1) > RISK_RANK.get(auto_merge.get("max_risk"), -1):
        next_step = f"Merge PR #{facts['pr']}" if facts.get("pr") else "waiting for a person to merge"
        return "waiting", {"reason": f"[factory] {next_step}: auto-merge is off or this PR exceeds its risk limit."}
    return ("updated" if facts.get("behind") else "merged"), {}


def retro_payload(facts):
    """The `finish` retro comment of a merged ticket, from its facts."""
    agent = [r for r in facts["reviews"] if r["agent"]]
    pr = f"PR #{facts['pr']} {facts['pr_state']}" if facts["pr"] else "no PR found"
    text = (f"Retro: {pr}; fix rounds {facts['fix_rounds']}; "
            f"reviews {len(agent)} ({sum(r['verdict'] == 'approve' for r in agent)} approve, "
            f"{sum(r['verdict'] == 'changes' for r in agent)} changes)")
    return {"retro": text, "run_id": "finish:retro"}


AFTER_MERGE_CHECK = re.compile(r"(?im)^\s*[-*] \[ \] Operator check after the merge \(not part of the PR\):\s*(.+?)\s*$")
AFTER_MERGE_MARKER = "<!-- factory:after-merge-checks -->"


def after_merge_checks(body):
    return AFTER_MERGE_CHECK.findall(body or "")


def after_merge_comment(comments):
    return next((c.get("body", "") for c in comments if AFTER_MERGE_MARKER in c.get("body", "")), "")


def after_merge_complete(comments):
    checks = re.findall(r"(?m)^- \[([ xX])\] .+$", after_merge_comment(comments))
    return bool(checks) and all(x.lower() == "x" for x in checks)


def blockers_mode(nodes):
    """The workflow's `blockers` key: `start` (default) waits for every blocker before the work starts, `merge`
    starts the work at once and waits at the merge (a node's own `when.blockers: closed` still applies)."""
    mode = (nodes or {}).get("blockers", "start")
    if mode not in ("start", "merge"):
        raise Bad(f"bad blockers mode {mode!r}: allowed start, merge")
    return mode


def sweep_plan(facts, auto_merge=None, default_tier="mid", blockers="start", start_waits=False):
    """(node, outcome, payload) of the first outcome whose facts hold, else None."""
    stage = facts["stage"]
    waits = blockers == "merge" and facts.get("blockers_open")  # approved, but a blocker is still open
    if facts.get("pr_state") == "merged" and config_mod.load()["tracker"]["stage_kinds"].get(stage) != "final":
        comments = facts.get("comments", [])
        return ("finish", "merged", {}) if not after_merge_checks(facts.get("body")) \
            or not after_merge_comment(comments) or after_merge_complete(comments) else None
    if stage == "ready" and (blockers == "merge" and not start_waits or not facts.get("blockers_open")) \
            and facts.get("issue_state", "open") == "open":
        return "start", "started", {"tier": fix_tier(facts, default_tier)}
    if stage == "needs-answers" and facts.get("answered"):
        return "finish", "answered", {}
    if stage == "approved" and facts.get("pr_state") == "open" and facts.get("conflict"):
        return "finish", "conflict", {}  # the cheap updater merges main; the reviewers then check the resolution
    if stage == "approved" and facts.get("pr_state") == "open" \
            and ((facts.get("round_head") is not None and facts.get("head") != facts.get("round_head"))
                 or facts.get("ci") == "red"):
        return "reopen", "reopened", {}
    if stage == "approved" and not waits and (m := merge_decision(facts, auto_merge)):
        return "merge", *m
    if stage == "split" and (kids := facts.get("children")):
        if bad := [c for c in kids if c["blocked"] or c["not_planned"]]:
            what = ", ".join(f"#{c['number']} ({'closed as not planned' if c['not_planned'] else 'blocked'})" for c in bad)
            return "finish", "child-blocked", {"reason": f"[factory] Blocked: child tickets need a person: {what}. "
                                                         f"Fix them, then set {vocab.stage('ready')} on this ticket."}
        if all(c["state"] == "closed" for c in kids):
            return "finish", "children-done", {}
    if stage == "in-review" and facts.get("waiting_reviewers") and (
            not facts.get("handed_to_fix") or has_dispute_rereview(facts)):
        return "join", "waiting", {}  # ask them again; nothing moves until they decide
    if stage == "in-review" and (j := join_decision(facts)):
        return "join", *j
    return None


def sweep(tracker, nodes, dry_run=False, closed_ticket=None, auto_merge=None, default_tier="mid", progress=None,
          issue=None):
    """Move every listed ticket through each step its facts allow (a merged PR goes approved to done in one
    step). A ticket that reaches done or blocked also lets its split parent finish. Returns (results, failed)."""
    results, failed, done_tickets = [], False, []
    stages = config_mod.load()["tracker"]["stages"]
    mode = blockers_mode(nodes)
    start_waits = ((nodes.get("nodes", {}).get("start") or {}).get("when") or {}).get("blockers") == "closed"
    queue, seen, merged = ([issue] if issue is not None else list(tracker.sweep_tickets(stages))), set(), False
    prefetch(tracker, queue)
    while queue:
        n = queue.pop(0)
        for _ in range(4):  # a ticket's longest walk: in-review, approved, done
            try:
                facts = tracker.facts(n)
                plan = sweep_plan(facts, auto_merge, default_tier, mode, start_waits)
                if plan is None or (n, facts["stage"]) in seen:
                    if progress:
                        progress({"ticket": n, "stage": facts["stage"], "outcome": "skipped"})
                    break
                if plan[:2] == ("merge", "merged") and merged:
                    if progress:
                        progress({"ticket": n, "stage": facts["stage"], "outcome": "skipped",
                                  "why": "one merge at a time"})
                    break  # one merge at a time; the next sweep reads the rest against the new main
                seen.add((n, facts["stage"]))
                node, outcome, payload = plan
                r = {"ticket": n, "stage": facts["stage"], "node": node, "outcome": outcome}
                results.append(r)
                if dry_run:
                    if progress:
                        progress(r)
                    break
                code, out = apply(node, outcome, {"ticket": n, **payload}, tracker, nodes)
                r.update(applied=out["applied"], written=out.get("written", []))
                if progress:
                    progress(r)
                if not out["applied"]:
                    break
                if node == "finish" and outcome == "merged" and hasattr(tracker, "config_change_merged"):
                    try:  # a reminder only: its failure must not stop the parent's step below
                        tracker.config_change_merged(facts.get("pr"))
                    except Exception as e:
                        failed = True
                        results.append({"ticket": n, "error": f"deploy check: {type(e).__name__}: {e}"})
                merged = merged or (node, outcome) == ("merge", "merged")
                if node == "finish" and outcome in ("merged", "children-done"):
                    done_tickets.append(n)
                if node == "finish" and outcome in ("merged", "children-done") and hasattr(tracker, "parent") and (p := tracker.parent(n)):
                    queue.append(p)
            except Exception as e:  # one ticket failing does not stop the others
                failed = True
                r = {"ticket": n, "error": f"{type(e).__name__}: {e}", **({"kind": e.kind} if hasattr(e, "kind") else {})}
                results.append(r)
                if progress:
                    progress(r)
                break
    wake_sources = list(dict.fromkeys(([closed_ticket] if closed_ticket is not None else []) + done_tickets))
    woken = set()
    if hasattr(tracker, "blocking"):
        for source in wake_sources:
            try:
                dependents = tracker.blocking(source)
                not_planned = hasattr(tracker, "not_planned") and tracker.not_planned(source)
            except Exception as e:  # a tracker lookup must not fail the whole sweep
                results.append({"ticket": source, "error": f"{type(e).__name__}: {e}"})
                continue
            for dependent in dependents:
                n = dependent["number"] if isinstance(dependent, dict) else dependent
                try:
                    if n in woken:
                        if progress:
                            progress({"ticket": n, "outcome": "skipped", "why": "already inspected"})
                        continue
                    facts = tracker.facts(n)
                    if mode == "merge" and facts["stage"] == "approved" and not facts["blockers_open"]:
                        woken.add(n)  # its last blocker closed: the merge it waited on may run now
                        if merged:  # one merge at a time; the next sweep reads the rest against the new main
                            continue
                        sub, sub_failed = sweep(tracker, nodes, dry_run, None, auto_merge, default_tier, progress, issue=n)
                        results += sub
                        merged = merged or any((r.get("node"), r.get("outcome")) == ("merge", "merged") for r in sub)
                        failed = failed or sub_failed
                        continue
                    if facts["stage"] != "ready" or facts["blockers_open"]:
                        if progress:
                            progress({"ticket": n, "stage": facts["stage"], "outcome": "skipped"})
                        continue
                    facts = tracker.stale_facts(n)
                    since = facts.get("stage_at")  # when this ticket last got its stage label, i.e. the re-fire
                    recent = since and datetime.now(timezone.utc) - datetime.fromisoformat(since.replace("Z", "+00:00")) <= timedelta(minutes=5)
                    if facts.get("issue_state", "open") != "open" or recent:  # closed, or this event's re-fire already landed
                        if progress:
                            progress({"ticket": n, "outcome": "skipped", "why": "closed" if facts.get("issue_state", "open") != "open" else "re-fired within 5 minutes"})
                        continue
                    woken.add(n)
                    outcome = "re-triage" if not_planned else "unblocked"
                    results.append({"ticket": n, "stage": "ready", "node": "start", "outcome": outcome})
                    if progress:
                        progress(results[-1])
                    if dry_run:
                        continue
                    effects = ({"kind": "comment", "ticket": n, "run_id": "finish:re-triage",
                                "value": f"[factory] Blocker #{source} was closed as not planned; re-triage this ticket"},
                               {"kind": "stage", "ticket": n, "value": "triage"}) if not_planned else ({"kind": "unlabel", "ticket": n, "value": vocab.stage("ready")},
                                   {"kind": "label", "ticket": n, "value": vocab.stage("ready")},
                                   {"kind": "comment", "ticket": n, "value": f"[factory] unblocked by #{source}",
                                    "run_id": "finish:unblocked"})
                    for effect in effects:
                        tracker.apply(effect)
                except Exception as e:
                    failed = True
                    r = {"ticket": n, "error": f"{type(e).__name__}: {e}"}
                    results.append(r)
                    if progress:
                        progress(r)
    return results, failed


def prefetch(tracker, numbers):
    """Let a tracker that batches its reads (GitHub: GraphQL) read `numbers` at once; the others read per ticket."""
    if numbers and hasattr(tracker, "prefetch"):
        tracker.prefetch(numbers)


STALE_STAGES = ("triage", "ready", "in-dev", "in-review", "changes-requested", "approved")
RECOVER = "[factory] recover:"


def recovered(facts, stage):
    """Re-fires already recorded for `stage` (the counter is the `[factory] recover: stage=<stage> re-fired ...` comment; comment-only notes do not count)."""
    return sum(f"stage={stage} re-fired" in c for c in facts.get("recover_comments", []))


def stale_plan(facts, now, hours, command_minutes=30):
    """(kind, label, on, text) when the ticket's waking event is visibly missing after `hours` in its stage:
    kind `refire` (remove, then add `label` on `on`), `comment` (no label to re-fire), `finish` (the PR merged)
    or `conflict` (an approved PR conflicts: the sweep's update-cheap),
    else None. `since` is the last label or commit event (ISO); an unknown one never makes a ticket stale."""
    stage, since = facts["stage"], facts.get("since")
    if facts.get("issue_state", "open") != "open" or not since:
        return None
    age = timedelta(minutes=command_minutes) if facts.get("command_pending") else timedelta(hours=hours)
    if now - datetime.fromisoformat(since.replace("Z", "+00:00")) <= age:
        return None
    cmd, cmd_at, commit_at = facts.get("command"), facts.get("command_at") or "", facts.get("commit_at") or ""
    if stage == "changes-requested" and facts.get("conflict") and cmd and cmd.startswith(vocab.prefix("update-<tier>")) \
            and cmd_at and cmd_at < since:
        return "refire", cmd, "pr", "conflicting PR has an update label with no later run"
    if stage == "triage":
        return "refire", vocab.stage("triage"), "issue", "no intake outcome"
    if stage == "ready" and not facts.get("blockers_open"):
        return "refire", vocab.stage("ready"), "issue", "no start outcome"
    if stage == "in-dev" and cmd and (not facts.get("pr") or commit_at < cmd_at):
        return "refire", cmd, "issue", "no PR or commit since the command label"
    if stage == "in-review" and facts.get("pr"):
        head = facts.get("head")
        if facts.get("ci_label") == "green" and cmd and cmd.startswith(vocab.prefix("review-<focus>")) \
                and cmd.removeprefix(vocab.prefix("review-<focus>")) in facts.get("focus_missing", []):
            return "refire", cmd, "pr", "no review for this focus at the head"
        if facts.get("ci_label") == "green" and not any(r["agent"] and r["commit_id"] == head for r in facts.get("reviews", [])):
            missing = facts.get("focus_missing", [])
            if missing:
                return "refire", vocab.cmd("review-<focus>", missing[0]), "pr", "no review for this focus at the head"
        if not facts.get("ci_label"):
            if facts.get("conflict"):
                return "comment", None, None, f"no CI label at head {head}: the PR conflicts with main (GitHub starts no CI on a conflict); the update runs after the join"
            return "comment", None, None, f"no CI label at head {head}: CI's job"
    if stage == "changes-requested" and cmd and commit_at < cmd_at:
        return "refire", cmd, "pr", "no commit since the fix label"
    if stage == "approved" and facts.get("pr_state") == "merged":
        return "finish", None, None, "the PR merged"
    if stage == "approved" and facts.get("pr_state") == "open" and facts.get("conflict"):
        return "conflict", None, None, "the approved PR conflicts with main"
    if stage == "ready" and facts.get("blockers_open"):
        return None
    if stage == "changes-requested" and cmd and commit_at >= cmd_at:
        return None
    return "human", None, None, f"no recovery rule re-fired after {hours} hours in {stage}"


def sweep_stale(tracker, nodes, hours, now=None, dry_run=False, command_minutes=30, progress=None):
    """Recover tickets whose Air run died: one re-fire per ticket and stage, a second stale finding blocks it.
    Returns (results, failed)."""
    now, results, failed = now or datetime.now(timezone.utc), [], False
    stale = tracker.sweep_tickets(STALE_STAGES)
    prefetch(tracker, stale)
    for n in stale:
        try:
            facts = tracker.stale_facts(n)
            plan = stale_plan(facts, now, hours, command_minutes)
            if plan is None:
                if progress:
                    progress({"ticket": n, "stage": facts["stage"], "recover": "skipped"})
                continue
            kind, label, on, why = plan
            stage = facts["stage"]
            r = {"ticket": n, "stage": stage, "recover": kind, "why": why}
            results.append(r)
            if dry_run:
                if progress:
                    progress(r)
                continue
            run = "recover:stale"
            if kind == "finish":
                _, out = apply("finish", "merged", {"ticket": n}, tracker, nodes)
                r["applied"] = out["applied"]
            elif kind == "conflict":
                _, out = apply("finish", "conflict", {"ticket": n}, tracker, nodes)
                r["applied"] = out["applied"]
            elif kind == "comment":
                r["applied"] = tracker.apply({"ticket": n, "kind": "comment", "run_id": run,
                                              "value": f"{RECOVER} stage={stage} {why}"})
            elif kind == "human":
                if vocab.needs_human() in facts.get("attrs", []):
                    if progress:
                        progress({**r, "recover": "skipped"})
                    continue  # preserve an existing human wait and its assignee
                tracker.apply({"ticket": n, "kind": "label", "value": vocab.needs_human()})
                tracker.apply({"ticket": n, "kind": "assign", "value": config_mod.owner()})
                tracker.apply({"ticket": n, "kind": "label", "value": vocab.needs_human(), "on": "pr"})
                tracker.apply({"ticket": n, "kind": "assign", "value": config_mod.owner(), "on": "pr"})
                text = f"{RECOVER} stage={stage}: {why}. A person decides, then sets {vocab.stage('ready')}."
                r["applied"] = tracker.apply({"ticket": n, "kind": "comment", "run_id": run, "value": text})
            elif recovered(facts, stage):
                r["recover"] = "blocked"
                _, out = apply("recover", "blocked", {"ticket": n, "run_id": run,
                                                          "reason": f"[factory] Stopped: the node did not respond twice in {stage}. A person decides, then sets {vocab.stage('ready')}."},
                               tracker, nodes)
                r["applied"] = out["applied"]
            else:  # the comment last: for `ready` the re-added stage label starts the stage, and the comment must follow it
                tracker.apply({"ticket": n, "kind": "unlabel", "value": label, "on": on})
                r["applied"] = tracker.apply({"ticket": n, "kind": "label", "value": label, "on": on})
                tracker.apply({"ticket": n, "kind": "comment", "run_id": run,
                               "value": f"{RECOVER} stage={stage} re-fired {label} ({why})"})
            if progress:
                progress(r)
        except Exception as e:  # one ticket failing does not stop the others
            failed = True
            r = {"ticket": n, "error": f"{type(e).__name__}: {e}"}
            results.append(r)
            if progress:
                progress(r)
    return results, failed


def head_ci(checks):
    """(green|red|pending|none, latest result time) of a head's check runs and commit statuses, each
    {conclusion, at} with conclusion None while it runs. One failure is red, as the gate's `ci` fact reads it."""
    done = [c["conclusion"] for c in checks]
    at = max((c["at"] for c in checks if c.get("at")), default=None)
    if not done:
        return "none", at
    if any(c in BAD_CI for c in done):
        return "red", at
    return ("green" if all(c in GOOD_CI for c in done) else "pending"), at


def ci_sync_pr(code_host, repository, pr, dry_run=False):
    """Set `factory:ci-green` or `factory:ci-red` on one factory PR {number, head, labels} from its head checks; a
    head with no result yet (pending, or no checks) keeps its labels. A label added before the head's latest result
    belongs to an earlier head: it is removed and added again, because re-adding a present label fires no event.
    The scheduled `ci_sync` calls this per PR; the event path (a finished check, after the #365 probe) calls it for one PR."""
    ci, at = head_ci(code_host.head_checks(repository, pr["head"]))
    r = {"repo": repository, "pr": pr["number"], "head": pr["head"], "ci": ci, "added": [], "removed": []}
    if ci not in ("green", "red"):
        return r
    want, other = vocab.ci(f"ci-{ci}"), vocab.ci("ci-red" if ci == "green" else "ci-green")
    stale = want in pr["labels"] and bool(at) and (code_host.label_added_at(repository, pr["number"], want) or "") < at
    if want in pr["labels"] and not stale:
        r["removed"] = [other] if other in pr["labels"] else []
    else:
        r["removed"], r["added"] = [x for x in (other, want) if x in pr["labels"]], [want]
    if not dry_run and (r["added"] or r["removed"]):
        code_host.edit_pr_labels(repository, pr["number"], add=r["added"], remove=r["removed"])
    return r


def ci_sync(code_host, repositories, dry_run=False, progress=None):
    """The CI label of every open factory PR in `repositories` (the `role: project` ones: the factory repository
    labels its own PRs from quality-gates). Returns (results, failed); one PR failing does not stop the others."""
    results, failed = [], False
    for repository in repositories:
        try:
            prs = code_host.factory_prs(repository)
        except Exception as e:
            failed = True
            results.append({"repo": repository, "error": f"{type(e).__name__}: {e}"})
            continue
        for pr in prs:
            try:
                r = ci_sync_pr(code_host, repository, pr, dry_run)
            except Exception as e:
                failed, r = True, {"repo": repository, "pr": pr["number"], "error": f"{type(e).__name__}: {e}"}
            results.append(r)
            if progress:
                progress(r)
    return results, failed


def run_ci_sync(dry_run=False, progress=None):
    """`ci-sync` (manual) and each `factory reconcile` pass (scheduled): the descriptor's project repositories on
    GitHub. Another code host has no sync yet: nothing is read."""
    config = config_mod.load()
    if (config.get("code_host") or {}).get("adapter") == "space":
        return [], False
    return ci_sync(GitHubCodeHost.from_config(), project_repositories(config), dry_run, progress)


def main(argv):
    p = argparse.ArgumentParser(prog="gate.py")
    p.add_argument("cmd", choices=["check", "apply", "sweep", "wake-reviewers", "ci-sync", "decide", "ci"])
    p.add_argument("--wait", action="store_true")
    p.add_argument("--node", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--stale", action="store_true")
    p.add_argument("--event", nargs="?", const="-")
    p.add_argument("--issue", type=int)
    p.add_argument("--pr", type=int)  # decide: the PR that woke the node; the gate finds its ticket
    p.add_argument("--outcome")
    p.add_argument("--payload")
    p.add_argument("--handoff")
    p.add_argument("--tracker", default="")
    p.add_argument("--nodes")
    p.add_argument("--spec")
    p.add_argument("--focus")  # the automation's focus and tier: the trigger payload carries neither
    p.add_argument("--tier")
    p.add_argument("--completed-head")  # CI's terminal label step runs before Actions marks its workflow completed
    try:
        a = p.parse_args(config_mod.pop_team(argv))
    except SystemExit:
        return BAD
    path = ""
    if a.cmd == "ci-sync":
        results, failed = run_ci_sync(a.dry_run, lambda r: print(json.dumps(r), file=sys.stderr, flush=True))
        print(json.dumps({"dry_run": a.dry_run, "ci_sync": results}))
        return FAILED if failed else GO
    code_host = None
    try:
        kind, _, path = (a.tracker or "github").partition(":")
        if kind == "github" and not path:
            tracker = GitHubGateTracker.from_config()
            if (config_mod.load().get("code_host") or {}).get("adapter") == "space":
                code_host = space_from_config(config_mod.load())
            else:
                code_host = GitHubCodeHost.from_config(**({"run": tracker.run} if hasattr(tracker, "run") else {}))  # one runner, so github_calls counts both
        elif kind == "memory" and path:
            try:
                tracker = MemoryTracker(dict(json.loads(Path(path).read_text())))
                code_host = tracker  # the memory tracker also maps a PR number to its ticket
            except (OSError, ValueError, TypeError) as e:
                raise Bad(f"malformed --tracker file: {e}")
        else:
            raise Bad("--tracker is github or memory:FILE")
        nodes = spec_nodes(a.spec) if a.spec else nodes_mod.load(a.nodes)
        config = config_mod.load()
        if a.cmd == "ci":
            if not a.issue:
                raise Bad("ci needs --issue TICKET")
            minutes = (config_mod.load().get("limits") or {}).get("ci_wait_minutes", 30)
            out = ci_state(tracker, a.issue, minutes * 60 if a.wait else 0)
            print(json.dumps(out))
            return GO if out["ci"] == "green" else FAILED if out["ci"] == "red" else NO_GO
        if a.cmd == "decide":
            ticket = a.issue or (pr_ticket(code_host, a.pr) if a.pr and code_host else None)
            if not (a.node and ticket):
                raise Bad("decide needs --node NODE and --issue TICKET or --pr PR")
            status, out = decide(a.node, ticket, tracker, nodes, dry_run=a.dry_run)
            print(json.dumps(out))
            return status
        if a.cmd == "wake-reviewers":
            if not a.issue:
                raise Bad("wake-reviewers needs --issue TICKET")
            facts = tracker.facts(a.issue)
            labels = review_wake_labels(facts, a.completed_head)
            for label in labels:
                tracker.apply({"ticket": a.issue, "kind": "label", "value": label, "on": "pr"})
            print(json.dumps({"ticket": a.issue, "labels": labels}))
            return GO
        if a.cmd == "sweep":
            if not hasattr(tracker, "sweep_tickets"):
                raise Bad("sweep needs --tracker github")
            issue, closed_ticket = a.issue, a.issue
            if a.event and issue is None:
                try:
                    raw = json.loads(sys.stdin.read() if a.event == "-" else Path(a.event).read_text())
                    if not isinstance(raw, dict):
                        raise ValueError("expected an event object")
                    event_name = raw.get("eventType") or raw.get("event_name")
                    raw = raw.get("data", raw)
                    if not isinstance(raw, dict) or not (event_name or isinstance(raw.get("action"), str)):
                        raise ValueError("expected an event name and issue or PR number")
                    issue_payload, pr_payload = raw.get("issue"), raw.get("pull_request")
                    is_pr = bool(pr_payload) or str(event_name or "").startswith("pull_request")
                    if not is_pr:
                        issue = (issue_payload or raw).get("number")
                        if raw.get("action") == "closed":
                            closed_ticket = issue
                    else:
                        pr_number = (pr_payload or raw).get("number")
                        ref_ticket = pr_ticket(code_host, pr_number, raw.get("repository")) if code_host else None
                        if ref_ticket is None:
                            raise ValueError("PR head ref does not identify an ez/<ticket> branch")
                        issue = ref_ticket
                    if not issue or (isinstance(issue, int) and issue < 1):
                        raise ValueError("event does not identify a ticket number")
                except (OSError, ValueError, TypeError) as e:
                    raise Bad(f"invalid sweep event: {e}")
            if issue is None and not a.stale:
                raise Bad("sweep needs --issue or --event; only stale recovery may be unscoped")
            progress = lambda item: print(json.dumps(item), file=sys.stderr, flush=True)
            results, failed = ([], False) if a.stale and issue is None else sweep(
                tracker, nodes, a.dry_run, closed_ticket, config["gates"].get("auto_merge"),
                config.get("defaults", {}).get("tier", "mid"), progress, issue)
            out = {"dry_run": a.dry_run, "sweep": results}
            if a.stale:
                out["stale"], stale_failed = sweep_stale(tracker, nodes, config["limits"].get("stale_hours", 2),
                                                         dry_run=a.dry_run,
                                                         command_minutes=config["limits"].get("stale_command_minutes", 30),
                                                         progress=progress)
                failed = failed or stale_failed
            print(json.dumps(out))
            print(json.dumps({"github_calls": getattr(getattr(tracker, "run", None), "calls", 0)}))  # last line: the load on the rate limit
            return FAILED if failed else GO
        if not a.node:
            raise Bad("--node is required")
        routine = bool(((nodes.get("nodes") or {}).get(a.node) or {}).get("routine"))
        if routine and a.cmd == "check":
            code, out = check_routine(a.node, nodes, config)
        elif routine:
            try:
                h = json.loads(Path(a.handoff).read_text()) if a.handoff else {}
            except (OSError, ValueError) as e:
                raise Bad(f"malformed handoff: {e}")
            if not isinstance(h, dict):
                raise Bad("malformed handoff: needs an object")
            code, out = apply_routine(a.node, a.outcome or h.get("outcome") or h.get("status") or "", nodes)
        elif a.cmd == "check":
            event = read(a.event or "", "event", raw=True, code_host=code_host)
            event.update({k: v for k, v in (("focus", a.focus), ("tier", a.tier)) if v})
            code, out = check(a.node, event, tracker, nodes, config, code_host)
        else:
            if a.handoff:
                try:
                    h = json.loads(Path(a.handoff).read_text())
                except (OSError, ValueError) as e:
                    raise Bad(f"malformed handoff: {e}")
                if not isinstance(h, dict):
                    raise Bad("malformed handoff: needs an object")
                ev = read(a.event, "event", raw=True, code_host=code_host) if a.event else {}
                if "ticket" not in {**h, **ev}:
                    raise Bad("apply --handoff needs `ticket` in the handoff or --event")
                payload = {**intake_fields(h), **h, **{k: v for k, v in ev.items() if v is not None and k != "id"},
                           **({"event": ev["id"]} if "id" in ev else {})}
                out_name = a.outcome or outcome_of(h)
            else:
                payload, out_name = read(a.payload or "", "payload"), a.outcome or ""
            code, out = apply(a.node, out_name, payload, tracker, nodes, a.focus, a.tier)
            path and Path(path).write_text(json.dumps(tracker.tickets))
    except NoTicket as e:
        print(json.dumps({"go": False, "reason": str(e)}))
        return NO_GO
    except MissingNumber as e:
        print(json.dumps({"go": False, "why": str(e)}))
        return 11
    except Bad as e:
        print(json.dumps({"error": str(e)}))
        return BAD
    except Exception as e:  # a failing effect: later effects and the stage did not run
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        if path:
            Path(path).write_text(json.dumps(tracker.tickets))
        return FAILED
    print(json.dumps(out))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

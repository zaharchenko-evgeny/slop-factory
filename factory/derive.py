"""The gate's facts, derived from the plain records a system reads (ADR-0026, factory/ports/records.py).

No call happens here: every fact comes from a TicketRecord and the ticket's PrRecords, so one rule holds for every
tracker and code host. A system reads a costly field only when the core asks for it (`pr_want`, `Lazy` maps).
Agent notes (a hidden `<!-- factory:agent-notes {...} -->` block on a bot's comment, review or PR text) are parsed
here as well: their format is the factory's, not a host's.
"""
import json
import re
from dataclasses import dataclass, field

import vocab
from gate_port import PR_DEFAULTS, combine_prs, fix_round_count

NOTE_FIELDS, NOTE_FOR, NOTE_MAX = ("for", "files", "repro", "hint", "context"), ("fixer", "reviewer", "developer", "any"), 2000
NOTES = re.compile(r"<!-- factory:agent-notes\s*(\{.*?\})\s*-->", re.S)
SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)|\bAIza[\w-]{35}\b|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{20,}"
                    r"|\bgithub_pat_\w{20,}|\bxox[abprs]-[\w-]{10,}|\bsk-[A-Za-z0-9_-]{20,}"
                    r'|(?i:\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b\s*[:=]\s*(?:"[^"]*"|[^\s",]+))', re.S)


def redact(v):
    """`v` with secret-looking text redacted in every string, before any serialisation."""
    if isinstance(v, str):
        return SECRET.sub("[redacted]", v)
    if isinstance(v, list):
        return [redact(x) for x in v]
    if isinstance(v, dict):
        return {k: redact(x) for k, x in v.items()}
    return v


def clean_notes(raw):
    """The notes object of one block: known fields only, secret-looking text redacted; ValueError when it is no
    object, `for` is unknown or it serialises past NOTE_MAX characters after redaction."""
    if not isinstance(raw, dict):
        raise ValueError("agent notes: needs an object")
    notes = {k: raw[k] for k in NOTE_FIELDS if k in raw}
    if notes.get("for") not in NOTE_FOR:
        raise ValueError(f"agent notes: `for` is one of {', '.join(NOTE_FOR)}")
    notes = {k: redact(v) for k, v in notes.items()}
    if len(json.dumps(notes)) > NOTE_MAX:
        raise ValueError(f"agent notes: over {NOTE_MAX} characters")
    return notes


def notes_block(notes):
    """The hidden block of cleaned notes; `<` and `>` are JSON-escaped so no text can close the comment early."""
    body = json.dumps(notes).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<!-- factory:agent-notes\n{body}\n-->"


def parse_notes(body, login="", kind="User"):
    """The valid notes blocks of `body`, only from a bot author: a person's text is no source of notes."""
    if kind != "Bot" and not login.endswith("[bot]"):
        return []
    out = []
    for m in NOTES.finditer(body or ""):
        try:
            out.append(clean_notes(json.loads(m[1])))
        except ValueError:
            pass
    return out


@dataclass
class Rules:
    """The team's settings the facts depend on: `stages` {stage: attr}, `risk_labels` {low, high}, `required`
    {risk: [review focus]}, the fix and agent-run caps, and the bots whose reviews count."""
    stages: dict
    risk_labels: dict = field(default_factory=lambda: {"low": "risk:low", "high": "risk:high"})
    required: dict = field(default_factory=dict)
    fix_cap: int = 3
    run_cap: int = 8
    trusted: set = field(default_factory=set)


def stage(ticket, stages):
    """The ticket's stage: the stage attr added last (a person's factory:ready on a running ticket wins over the
    attr it replaces), ordered by event position, not timestamp (the list is chronological, timestamps can tie)."""
    by_attr = {v: k for k, v in stages.items()}
    added = {}
    for i, e in enumerate(ticket.events):
        if e["kind"] == "labeled":
            added[e["value"]] = i
    attr = max((x for x in ticket.attrs if x in by_attr), key=lambda x: added.get(x, -1), default=None)
    return by_attr.get(attr, "none")


def pr_want(stage_):
    """The optional PR fields the facts of a ticket at `stage_` need: only the merge gates read `behind`."""
    return frozenset({"behind"}) if stage_ == "approved" else frozenset()


def agent_runs(ticket_events, pr_events, ready):
    """(runs, since): developer runs started since a person last set the `ready` attr: each `labeled` event of a
    command label (`implement-*`, `fix-*`, `update-*`) on the ticket or its PR after it. A person's `ready` starts a
    fresh budget. Reviews follow pushes, so this bounds them too."""
    cmds = (vocab.prefix("implement-<tier>"), vocab.prefix("fix-<tier>"), vocab.prefix("update-<tier>"))
    name = lambda e: (e.get("value") or "") if e.get("kind") == "labeled" else ""  # noqa: E731
    since = max((e.get("at") or "" for e in ticket_events if name(e) == ready), default="")
    runs = sum(1 for es in (ticket_events, pr_events or []) for e in es if name(e).startswith(cmds) and (e.get("at") or "") >= since)
    return runs, since


def fix_writes(events):
    """Times the join started a fix round on a PR: each `labeled fix-*` event (the fix node removes the label when
    it finishes, so every round adds it again)."""
    return [e["at"] for e in events if e["kind"] == "labeled" and (e["value"] or "").startswith(vocab.prefix("fix-<tier>"))]


def carry_approvals(reviews, head, clean_advance):
    """An agent approval of an earlier commit counts at `head` after a clean merge of the base (marked `carried`)."""
    for r in reviews:
        if r["agent"] and r["verdict"] == "approve" and r["commit_id"] != head and clean_advance[r["commit_id"]]:
            r.update(commit_id=head, carried=True)
    return reviews


def earlier(r, head, writes, commit_date):
    """True when change request `r` was counted by an earlier round: it names an older commit and a join started a
    fix round at or after it, so the fix was given it. A request sent during a fix, or with no fix round after it,
    counts now. Without any join write (a person pushed by hand), the push of the current head ends the earlier round."""
    if r["commit_id"] == head:
        return False
    at = r["submitted_at"] or ""
    if writes:
        return any(t >= at for t in writes)
    return at <= commit_date


def round_(reviews, required, head, requested, ci, writes, commit_date, can_write, trusted):
    """(complete, verdict, ignored, waiting) of the round at `head`. `waiting`: people whose change request a fix
    already received and who have not decided again; the round stays open until they do. The round is marked agent
    reviews of every required focus at head, no requested reviewer left (a host drops a person on review and adds them
    back on a re-request), CI concluded. A person (with write access) or trusted bot blocks with a latest change
    request on head or submitted since the round started; a comment never counts."""
    at_head = {r["focus"]: r for r in reviews if r["agent"] and r["commit_id"] == head}
    others = {r["author"]: r for r in reviews if not r["agent"] and r["verdict"]
              and (r["author"] in trusted if r["bot"] else can_write[r["author"]])}
    ignored = [r["author"] for r in reviews if r["bot"] and not r["agent"] and r["author"] not in trusted]
    if ci == "red":  # a red head needs a fix whatever the reviews say; reviewers only start on green
        return True, "changes", ignored, []
    complete = all(x in at_head for x in required) and not requested and ci == "green"
    if not complete:
        return False, None, ignored, []
    late = [r for r in others.values() if r["verdict"] == "changes"]
    if any(r["commit_id"] != head for r in late):
        # A request a fix already received stays open until its author decides again: the round waits.
        # A trusted bot cannot be asked again, so its handled request counts as resolved instead.
        waiting = [r["author"] for r in late if earlier(r, head, writes, commit_date) and not r["bot"]]
        late = [r for r in late if not (r["bot"] and earlier(r, head, writes, commit_date))]
        if waiting:
            return False, None, ignored, waiting
    changes = ci == "red" or any(at_head[x]["verdict"] != "approve" for x in required) or bool(late)
    return True, "changes" if changes else "approve", ignored, []


def notes(items, since):
    """(time, notes) of the bot-written items `(at, body, login, kind)` from `since` on."""
    return [(at or "", n) for at, body, login, kind in items if (at or "") >= since for n in parse_notes(body, login, kind)]


def pr_facts(pr, required, since, trusted):
    """(PR_DEFAULTS keys, agent notes) of the ticket's PR record `pr`."""
    head = pr.head
    reviews = carry_approvals([dict(r) for r in pr.reviews], head, pr.clean_advance)
    pr_labels = set(pr.labels)
    pending_rereviews = [focus for focus in required if vocab.cmd("rereview-<focus>", focus) in pr_labels]
    latest = {r["focus"]: r for r in reviews if r["agent"]}  # the later review of a focus wins
    heads = {latest[x]["commit_id"] for x in required if x in latest}
    requested = list(pr.requested_reviewers)
    every = fix_writes(pr.events)
    writes = [t for t in every if t >= since]  # a person's ready starts a fresh round count
    latest_review = max((r.get("submitted_at") or "" for r in reviews if r["commit_id"] == head), default="")
    complete, verdict, ignored, waiting = round_(reviews, required, head, requested, pr.ci, every, pr.commit_date,
                                                 pr.can_write, trusted)
    got = (notes([(c["at"], c["body"], c["author"], c["author_type"]) for c in pr.comments], since)
           + notes([(r.get("submitted_at"), r["body"], r["author"], "Bot" if r["bot"] else "User") for r in pr.reviews], since)
           + notes([(pr.updated_at, pr.body, pr.author, pr.author_type)], since))  # the PR body is edited after creation
    return {"branch": pr.branch, "handed_to_fix": bool(writes) and max(writes) > max(latest_review, pr.commit_date),
            "behind": bool(pr.behind),
            "pr_state": "merged" if pr.merged else "closed" if pr.state == "closed" else "open",
            "pr": pr.number, "head": head, "mergeable": pr.mergeable, "conflict": pr.mergeable is False,
            "ci": pr.ci, "reviews": reviews, "requested_reviewers": requested, "round_verdict": verdict,
            "ignored_reviews": ignored, "waiting_reviewers": waiting, "pending_rereviews": pending_rereviews,
            "pr_labels": sorted(pr_labels),
            "focus_missing": [x for x in required if latest.get(x, {}).get("commit_id") != head],
            "round_complete": complete,
            "round_head": heads.pop() if all(x in latest for x in required) and len(heads) == 1 else None,
            "fix_rounds": fix_round_count(reviews, since)}, got  # same-head dispute rereviews count as a new round


def risk(ticket, rules):
    return "low" if rules.risk_labels["low"] in ticket.attrs else "high"


def facts(ticket, prs, rules, children=None):
    """(facts, since) of `ticket` and its PRs `prs` [(repository name, PrRecord or None)] in the ticket's repository
    order, the primary first; `children` the sub-tickets when the stage is `split`. `since`: when a person last set
    `ready` (agent runs, fix rounds and notes count from it)."""
    stages, labels = rules.stages, ticket.attrs
    by_attr = {v: k for k, v in stages.items()}
    st = stage(ticket, stages)
    events = ticket.events
    actor_events = [e for e in events if e["kind"] in ("labeled", "unlabeled", "closed", "reopened")]
    primary = prs[0][1] if prs else None
    # a PR numbered like its ticket is read as the ticket (the events were keyed by number)
    runs, since = agent_runs(events, primary.events if primary and primary.number != ticket.id else None, stages.get("ready"))
    rk = risk(ticket, rules)
    required = rules.required[rk]
    f = {"stage": st, "attrs": [x for x in labels if x not in by_attr],
         "assignees": ticket.assignees, "blockers_open": len(ticket.blockers_open),
         "head": None, "pr": None, "mergeable": None, "conflict": False, "ci": "none", "reviews": [], "requested_reviewers": [],
         "focus_missing": [], "round_complete": False, "round_verdict": None, "round_head": None,
         "ignored_reviews": [], "waiting_reviewers": [], "fix_rounds": 0,
         "fix_cap": rules.fix_cap, "due": False, "seen": [], "pr_state": "none", "pending_rereviews": [],
         "agent_runs": runs, "run_cap": rules.run_cap,
         "handed_to_fix": False, "risk": rk, "behind": False,
         "issue_state": ticket.state, "updated_at": ticket.updated_at,
         "event_actor": actor_events[-1]["actor"] if actor_events else None,
         "title": ticket.title, "body": ticket.body, "author": ticket.author, "branch": None}
    comments = ticket.comments
    f["comments"] = comments
    got = [(c["at"], n) for c in comments if c["at"] >= since
           for n in parse_notes(c["body"], "", "Bot" if c["author_is_factory"] else "User")]
    if st == "needs-answers":
        waiting = [e for e in events if e["kind"] == "labeled" and e["value"] == stages["needs-answers"]]
        if waiting and comments:
            started = waiting[-1]["at"] or ""
            ready = [e for e in events if e["kind"] == "labeled" and e["value"] in (stages["triage"], stages["ready"])]
            setter = ready[-1]["actor"] if ready else None
            f["answered"] = any(c["at"] > started and not c["author_is_bot"] and
                                (c["author"] == setter or ticket.can_answer[c["author"]])
                                for c in comments)
    if st == "split":
        f.update(children=[{"number": k["number"], "state": k["state"], "not_planned": k["not_planned"],
                            "blocked": k["state"] != "closed" and stages["blocked"] in k["attrs"]}
                           for k in children or []])
    parts = []
    for name, pr in prs:
        part, found = pr_facts(pr, required, since, rules.trusted) if pr else ({}, [])
        parts.append({**PR_DEFAULTS, **part, "repo": name})
        got += found
    f.update({k: v for k, v in combine_prs(parts).items() if k != "repo"})
    f["agent_notes"] = [n for _, n in sorted(got, key=lambda t: t[0], reverse=True)]
    return f, since

"""Human-review attention of one ticket, from evidence, never from the stage or its age.

Review evidence (sufficient alone, in any stage, with or without the configured marker):
- `requested`: an outstanding requested reviewer on a PR (a bot login is not a person);
- `manual-gate`: an unresolved manual approval gate, which the engine's own `gate.merge_decision` reports as `waiting`.
The configured human-needed marker (`tracker.needs_human_label`) alone is the weaker kind `attention`. Ordinary agent
reviews are not evidence. Its reason is `question`, `unblocking` or `unknown`; a missing value is listed in `unknown`.
"""
import gate

# Engine-owned stage names that `derive.py` and the gate already read.
QUESTION_STAGE, UNBLOCKING_STAGE = "needs-answers", "blocked"


def _people(logins):
    return [x for x in logins or [] if isinstance(x, str) and x and not x.endswith("[bot]")]


def attention(facts, config):
    """{kind, label, reason, reviewer, sources, unknown} of a ticket's `facts`; kind is `review`, `attention` or None.
    `config` supplies the marker, `gates.auto_merge` and the owner."""
    marker = (config.get("tracker") or {}).get("needs_human_label")
    owner = config.get("owner") or (config.get("notify") or {}).get("github_user")
    have = set(facts.get("attrs") or []) | set(facts.get("pr_labels") or [])
    sources, reviewers = [], []
    people = _people(facts.get("requested_reviewers"))
    if people:
        sources.append({"kind": "requested-review", "evidence": f"PR #{facts.get('pr')} requests {', '.join(people)}"})
        reviewers += people
    auto = (config.get("gates") or {}).get("auto_merge")
    try:
        decision = gate.merge_decision(facts, auto) if auto is not None else None
    except (KeyError, TypeError):
        decision = None
    if decision and decision[0] == "waiting":
        sources.append({"kind": "manual-gate",
                        "evidence": "auto-merge is off or the PR exceeds its risk limit: a person merges"})
        reviewers += [owner] if owner and not people else []
    marked = bool(marker) and marker in have
    if marked:
        sources.append({"kind": "marker", "evidence": f"label {marker}"})
    if not sources:
        return {"kind": None, "label": None, "reason": None, "reviewer": None, "sources": [], "unknown": []}
    stage, review = facts.get("stage"), bool(people) or (decision is not None and decision[0] == "waiting")
    if review:
        reason = "review requested" if people else "manual approval gate"
    else:
        reason = "question" if stage == QUESTION_STAGE else "unblocking" if stage == UNBLOCKING_STAGE else "unknown"
    reviewer = ", ".join(dict.fromkeys(reviewers)) or None
    unknown = (["reviewer"] if reviewer is None else []) + (["reason"] if reason == "unknown" else [])
    return {"kind": "review" if review else "attention",
            "label": "Needs human review" if review else "Needs human attention",
            "reason": reason, "reviewer": reviewer, "sources": sources, "unknown": unknown}

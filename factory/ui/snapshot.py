"""`build`: the one normalized, read-only snapshot of a factory: its workflow graph and its tickets.

    snapshot = build(config, nodes, sources, now=datetime.now(timezone.utc))

Inputs are what the engine already loads: `config` (`config.load()`), `nodes` (`nodes.load()`, the compiler's graph)
and `sources` (`sources.live_sources`, or fakes of the same shape). The result is JSON-ready:
`{generated_at, workflow, blocker_policy, sources, tickets, attention, errors}`. A source that fails is listed in
`errors` and its tickets are missing; the other sources still give theirs. Each ticket is one card per
`(tracker identity, ticket id)` with its environments, stage age, human-review attention, PRs, CI, blockers, stage
history, links and, per node, trigger evidence, guards and execution (always apart; execution is `unknown`
without evidence).
"""
import gate
from ui import age, eligibility, graph, review, sources as sources_mod


def _message(e):
    return f"{type(e).__name__}: {e}"[:300]


def _ticket(source, ticket, envs, facts, rec, config, graph_nodes, evidence, poll_error, now, execution):
    tracker = config.get("tracker") or {}
    stages, kinds = tracker.get("stages") or {}, tracker.get("stage_kinds") or {}
    events = list(getattr(rec, "events", None) or [])
    stage = facts.get("stage", "none") if facts else "none"
    entered = age.entered_at(events, stages, stage) if rec is not None else None
    stage_age = age.stage_age(stage, kinds.get(stage), entered, now)
    human = review.attention(facts, config) if facts else {"kind": None, "label": None, "reason": None, "reviewer": None, "sources": [], "unknown": []}
    ref = str(ticket)
    repo = gate_split(ticket, source.repository)[0]
    prs = [{"repo": p.get("repo"), "number": p.get("pr"), "head": p.get("head"), "state": p.get("pr_state"), "ci": p.get("ci")}
           for p in (facts or {}).get("prs") or ([{"repo": None, "pr": facts["pr"], "head": facts.get("head"), "pr_state": facts.get("pr_state"), "ci": facts.get("ci")}]
                                                 if facts and facts.get("pr") else [])]
    url = source.ticket_url(ticket)
    missing = [name for name, value in (("title", (facts or {}).get("title")), ("repository", repo),
                                         ("stage_entered_at", entered), ("pr", prs or None)) if not value]
    nodes_view = [eligibility.evaluate(n, ticket, facts, evidence, poll_error, source.tracker, (execution or {}).get((source.identity, ticket, n["id"])))
                  for n in graph_nodes if n["triggers"] and facts] if facts else []
    return {"key": f"{source.id}#{ref}", "id": ref, "short_id": gate_split(ticket, None)[1], "tracker": source.id,
            "title": (facts or {}).get("title"), "repository": repo, "environments": envs,
            "state": (facts or {}).get("issue_state"), "stage": stage, "stage_kind": kinds.get(stage),
            "stage_age": stage_age, "warnings": ([{"kind": "stage-age", "level": "amber", "message": stage_age["reason"]}] if stage_age["warn"] else []),
            "human": human, "prs": prs, "ci": (facts or {}).get("ci"), "blockers_open": (facts or {}).get("blockers_open"),
            "history": age.stage_entries(events, stages), "links": {"ticket": url, "prs": []},
            "nodes": nodes_view, "execution": {"status": "unknown", "reason": "no execution evidence"},
            "missing": missing, "facts_error": None if facts else "facts unavailable"}


def gate_split(ticket, default):
    from gate_port import split_ticket
    return split_ticket(ticket, default)


def build(config, nodes, sources, now, run=None, execution=None):
    """The snapshot dict. `now` is the clock (a datetime); `run` the `gh` runner of the poll; `execution` an optional
    {(source identity, ticket, node): state} of execution evidence."""
    wf = graph.build(nodes, config)
    stage_names = [s["id"] for s in wf["stages"] if s["id"] != "none"]
    errors, per_source, source_info, evidence = [], [], [], {}
    for source in sources:
        try:
            ids = sources_mod.ticket_ids(source, stage_names)
            per_source.append((source, ids))
            source_info.append({"id": source.id, "environments": list(source.environments), "ok": True, "tickets": len(ids)})
        except Exception as e:
            errors.append({"source": source.id, "message": _message(e)})
            source_info.append({"id": source.id, "environments": list(source.environments), "ok": False, "tickets": 0})
        evidence[source.id] = eligibility.poll_evidence(config, nodes, [source.tracker], run)
    tickets = []
    for source, ticket, envs in sources_mod.merge(per_source):
        facts = rec = None
        try:
            facts = source.tracker.facts(ticket)
        except Exception as e:
            errors.append({"source": source.id, "ticket": ticket, "message": _message(e)})
        try:
            rec = sources_mod.record(source, ticket)
        except Exception as e:
            errors.append({"source": source.id, "ticket": ticket, "message": _message(e)})
        found, poll_error = evidence[source.id]
        tickets.append(_ticket(source, ticket, envs, facts, rec, config, wf["nodes"], found, poll_error, now, execution))
    try:
        policy = gate.blockers_mode(nodes)
    except gate.Bad as e:
        policy = None
        errors.append({"source": None, "message": _message(e)})
    kinds = [t["human"]["kind"] for t in tickets]
    return {"generated_at": now.isoformat(), "workflow": wf, "blocker_policy": policy, "sources": source_info,
            "tickets": tickets, "errors": errors,
            "attention": {"human_review": kinds.count("review"), "human_attention": kinds.count("attention")}}

"""The shared `State` field of a YouTrack project (#1260): the people's side of the factory's stage tags.

- `tracker.state_mirror: {<stage>: <State value>}`: the stage effect also writes that value (effects.py `stage`).
- `tracker.state_rules: {<State value>: stop | blocked}`: each reconcile pass (hook `reconcile.ticket_passes`) reads
  the State of every ticket with a stage tag; when a person (not one of `bot_logins`) last set a State that has a rule
  (never a mirrored value, so the gate's own writes wake none),
  `stop` removes the stage tag and closes the ticket's open PRs with a comment, `blocked` sets the stage `blocked`
  and keeps the person's State.
"""
import vocab

ACTIONS = ("stop", "blocked")
FIELD_KEYS = {"name", "from", "sizes", "type"}


def definition_errors(defn):
    """Hook `definition.errors`: a bad `state_rules` action, and a `field` effect that is not `{name, from[, sizes][, type]}`."""
    errs = []
    tracker = defn["factory"].get("tracker") or {}
    mirrored = set((tracker.get("state_mirror") or {}).values())
    for value, action in (tracker.get("state_rules") or {}).items():
        if action not in ACTIONS:
            errs.append(f"bad-state-rule: tracker.state_rules.{value}: {action!r} is not one of {', '.join(ACTIONS)}")
        if value in mirrored:  # the gate writes it, often with a person's token: its own write would wake the rule
            errs.append(f"bad-state-rule: tracker.state_rules.{value}: a value of tracker.state_mirror cannot have a rule")
    for name, n in (defn["workflow"].get("nodes") or {}).items():
        for o, oc in (n.get("outcomes") or {}).items():
            for e in (oc or {}).get("effects") or []:
                spec = e.get("field") if isinstance(e, dict) else None
                if spec is None:
                    continue
                sizes = spec.get("sizes") if isinstance(spec, dict) else None
                if not (isinstance(spec, dict) and set(spec) <= FIELD_KEYS and isinstance(spec.get("name"), str)
                        and isinstance(spec.get("from"), str)
                        and (sizes is None or isinstance(sizes, dict) and all(v is None or isinstance(v, int) for v in sizes.values()))):
                    errs.append(f"bad-effect: nodes.{name}.{o}: field is {{name, from[, sizes: {{value: limit or null}}][, type]}}")
    return errs


def apply_rules(tracker, rules, stages, final=(), dry_run=False, progress=None):
    """[result] per ticket whose person-set State has a rule; see the module."""
    store = tracker.tickets
    field = store.state_mirror_field
    tags = {tag: stage for stage, tag in stages.items()}
    results = []
    for n in store.tickets_by_stage([t for s, t in stages.items() if s not in final], open_only=True):
        try:
            state, who = store.last_field_change(n, field)
            action = rules.get(state)
            if not action or who is None or who in store.bots:
                continue
            stage = next((tags[x] for x in store.issue(n)["attrs"] if x in tags), None)
            if stage is None or (action == "blocked" and stage == "blocked"):
                continue
            r = {"ticket": n, "state": state, "stage": stage, "rule": action}
            results.append(r)
            if not dry_run:
                if action == "stop":
                    note = f"[factory] {field} is {state}: the factory stops work on this ticket."
                    tracker.close_prs(n, note)
                    store.remove_attr(n, *[x for x in store.issue(n)["attrs"] if x in tags])
                    store.comment(n, note)
                else:
                    tracker.apply({"ticket": n, "kind": "stage", "value": "blocked", "mirror": False, "run_id": "reconcile"})
            if progress:
                progress(r)
        except Exception as e:  # one ticket failing does not stop the others
            results.append({"ticket": n, "rule": "error", "error": f"{type(e).__name__}: {e}"})
    return results


def reconcile_pass(config, dry_run, progress):
    """Hook `reconcile.ticket_passes`: {"state_rules": (results, failed)} of a YouTrack team with `tracker.state_rules`."""
    t = config.get("tracker") or {}
    if t.get("adapter") != "youtrack" or not t.get("state_rules"):
        return {}
    try:
        import gate
        tracker = gate.tracker_of(config)
        stages = {s: vocab.stage(s) for s in t.get("stages") or {}}
        final = [s for s, k in (t.get("stage_kinds") or {}).items() if k == "final"]
        results = apply_rules(tracker, t["state_rules"], stages, final, dry_run, progress)
        return {"state_rules": (results, any(r.get("rule") == "error" for r in results))}
    except Exception as e:
        return {"state_rules": ([{"error": f"{type(e).__name__}: {e}"}], True)}


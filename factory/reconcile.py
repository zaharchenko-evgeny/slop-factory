#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""`factory reconcile`: re-fire the waking label of a node whose outcome is missing, from the state alone.

Usage: reconcile.py [--dry-run] [--tracker memory:FILE]
No model and no Air quota: the expected next step of each ticket comes from engine/nodes.yaml (the node whose
`when` matches the ticket's state and whose trigger is a label). A step with no outcome after `limits.run_timeout_minutes`
(a node's `timeout_minutes` overrides it) gets its label once; a second miss sets `blocked` and `factory:needs-human`.
A closed ticket in a PR stage with a merged PR is finished (its `finish` run was lost).
A ticket in a waiting stage, closed, or already marked `factory:needs-human` is left alone.
Each pass also runs the CI label sync (`gate.py ci-sync`) over the `role: project` repositories (not with memory:FILE).
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod
import definition
import nodes as nodes_mod
import vocab

PREFIX = "[factory] reconcile:"
SYSTEMIC_MISSES = 3  # second misses in one run that mean the factory, not the tickets, is broken
RUN = "reconcile"
LOST_FINISH = ("approved", "in-review", "changes-requested")
RED_TITLE = re.compile(r"Main is red(?: in Air)?: (.+)")
FACTS_ERROR = f"{PREFIX} ticket facts failed:"


def ticket_error(tracker, number, error, dry_run=False):
    """Report a ticket-local read failure once, without preventing the next ticket from running."""
    result = {"ticket": number, "error": f"{type(error).__name__}: {error}"}
    if dry_run:
        return result
    try:
        store = getattr(tracker, "tickets", None)
        if hasattr(store, "comments"):
            comments = [c["body"] for c in store.comments(number)]
        else:
            comments = [json.loads(value) for kind, value in store[number].get("log", []) if kind == "comment"]
        if not any(comment.startswith(FACTS_ERROR) for comment in comments):
            tracker.apply({"ticket": number, "run_id": RUN, "kind": "comment",
                           "value": f"{FACTS_ERROR} {result['error']}"})
    except Exception as comment_error:
        result["comment_error"] = f"{type(comment_error).__name__}: {comment_error}"
    return result


def wakes(nodes, facts):
    """(node, label, on, why) per waking label the graph expects for this state."""
    stage = facts["stage"]
    for name, n in nodes["nodes"].items():
        when = n.get("when") or {}
        stages = when.get("stage")
        if n.get("enabled") is False or n.get("routine") or n["role"] == "human" or stage not in (
                stages if isinstance(stages, list) else [stages]):
            continue
        ci = when.get("ci")
        if (when.get("blockers") == "closed" and facts.get("blockers_open")) or (
                ci and facts.get("ci") not in (ci if isinstance(ci, list) else [ci])) or (
                when.get("round") == "complete" and not facts.get("round_complete")):
            continue
        for t in n.get("triggers") or []:
            event, _, label = str(t).partition(" ")
            on = "issue" if event.startswith("issues.") else "pr"
            if not label.startswith(vocab.stage_prefix()):
                continue
            if "<focus>" in label:
                for focus in facts.get("focus_missing") or []:
                    yield name, label.replace("<focus>", focus), on, f"no {focus} review at the head"
            elif n.get("focuses"):
                continue  # its labels wake a focus only while that focus is missing
            elif "<tier>" in label:
                cmd = facts.get("command")
                if cmd and re.fullmatch(label.replace("<tier>", "[a-z]+"), cmd):
                    yield name, cmd, on, "no outcome after the command label"
            elif label not in (vocab.ci("ci-green"), vocab.ci("ci-red")) or (
                    facts.get("ci") in ("green", "red") and label == vocab.ci(f"ci-{facts['ci']}")):  # pending: CI's job
                yield name, label, on, f"no {name} outcome"


def reconcile(tracker, nodes, stages, limits, now=None, dry_run=False, progress=None, stage_kinds=None):
    """Returns (results, failed). One re-fire per ticket and stage; the second miss blocks the ticket."""
    now, results, failed = now or datetime.now(timezone.utc), [], False
    default = limits.get("run_timeout_minutes", 30)
    stage_kinds = stage_kinds or {}
    # Open tickets in an active or waiting stage only: each listed ticket costs a dozen API reads, and a closed or
    # done one is never re-fired, so listing them spent the hourly GitHub budget for nothing.
    listed = tracker.sweep_tickets([s for s in stages if stage_kinds.get(s) != "final"], open_only=True)
    import gate  # lazily, as finish_lost does
    gate.prefetch(tracker, listed)
    finish_lost(tracker, nodes, [s for s in LOST_FINISH if s in stages], listed, dry_run, results, progress)
    for n in listed:
        try:
            facts, stage = tracker.stale_facts(n), None
            stage, since = facts["stage"], facts.get("since")
            if stage_kinds.get(stage) == "waiting":
                stage_since = facts.get("stage_at") or since
                if (facts.get("issue_state", "open") != "open" or not stage_since
                        or not facts.get("assignees")):
                    continue
                age = now - datetime.fromisoformat(stage_since.replace("Z", "+00:00"))
                marker = f"{PREFIX} stage={stage} entry={stage_since} waiting-reminder"
                if (age >= timedelta(hours=limits.get("remind_after_hours", 24))
                        and not any(c.startswith(marker) for c in facts.get("recover_comments", []))):
                    people = " ".join(f"@{person}" for person in facts["assignees"])
                    r = {"ticket": n, "stage": stage, "reconcile": "remind", "why": "waiting for a person"}
                    results.append(r)
                    if not dry_run:
                        tracker.apply({"ticket": n, "run_id": RUN, "kind": "comment",
                                       "value": f"{marker}: {people}, this ticket is still waiting for you."})
                    if progress:
                        progress(r)
                continue
            if stage_kinds.get(stage) == "final":
                continue
            red = RED_TITLE.match(facts.get("title") or "")
            if red and facts.get("issue_state", "open") == "open" and tracker.main_green():
                r = {"ticket": n, "stage": stage, "reconcile": "stale-red", "why": f"{red[1]} is green on the main head"}
                results.append(r)  # reported only: the quality-gates workflow closes it on the next green push
                if progress:
                    progress(r)
                continue
            if (facts.get("issue_state", "open") != "open" or not since
                    or vocab.needs_human() in facts.get("attrs", [])):
                continue
            age = now - datetime.fromisoformat(since.replace("Z", "+00:00"))
            conflict = (stage == "in-review" and facts.get("conflict") and facts.get("pr")
                        and not facts.get("command") and facts.get("pr_state") in (None, "none", "open"))
            conflicting_fix = (stage == "changes-requested" and facts.get("conflict")
                               and (facts.get("command") or "").startswith(vocab.prefix("fix-<tier>")))
            if conflict:
                step = ("fix", None, "pr", "the unapproved PR conflicts with main")
                if age <= timedelta(minutes=default):
                    continue
            elif conflicting_fix:
                step = ("fix", facts["command"], "pr", "conflicting PR has a fix label with no later run")
                if age <= timedelta(minutes=default):
                    continue
            else:
                replay = gate.stale_review_plan(facts) if stage == "in-review" else None
                if replay:
                    node, outcome, payload = replay
                    if age <= timedelta(minutes=nodes["nodes"][node].get("timeout_minutes", default)):
                        continue
                    r = {"ticket": n, "stage": stage, "node": node, "outcome": outcome,
                         "reconcile": "reapply", "why": "review marker landed at the head; finish its outcome"}
                    results.append(r)
                    if not dry_run:
                        _, out = gate.apply(node, outcome, {"ticket": n, **payload, "run_id": RUN}, tracker, nodes)
                        r.update(applied=out["applied"], written=out.get("written", []))
                    if progress:
                        progress(r)
                    continue
                step = next((w for w in wakes(nodes, facts)
                             if age > timedelta(minutes=nodes["nodes"][w[0]].get("timeout_minutes", default))), None)
                stale_reviews = [label for label, at in facts.get("review_labels", [])
                                 if label.startswith((vocab.prefix("review-<focus>"), vocab.prefix("rereview-<focus>")))
                                 and now - datetime.fromisoformat(at.replace("Z", "+00:00")) > timedelta(minutes=default)]
                stale_focuses = {label.removeprefix(vocab.prefix("review-<focus>")).removeprefix(vocab.prefix("rereview-<focus>"))
                                 for label in stale_reviews}
                stale_step = next((w for w in wakes(nodes, facts)
                                   if w[0] == "review" and w[1].removeprefix(vocab.prefix("review-<focus>")) in stale_focuses), None)
                if stale_step:
                    step = stale_step
                if step is None:
                    continue
            node, label, on, why = step
            head = (facts.get("head") or "")[:12]
            missed = sum(f"stage={stage} head={head} re-fired {label} (" in c for c in facts.get("recover_comments", []))
            kind = "conflict" if conflict else "blocked" if missed else "refire"
            r = {"ticket": n, "stage": stage, "node": node, "reconcile": kind, "why": why}
            results.append(r)
            if not dry_run:
                send(tracker, n, nodes, stage, head, kind, label, on, why)
            if progress:
                progress(r)
        except Exception as e:  # one ticket failing does not stop the others
            failed = True
            results.append(ticket_error(tracker, n, e, dry_run))
            if progress:
                progress(results[-1])
    return results, failed


def finish_lost(tracker, nodes, stages, open_listed, dry_run, results, progress):
    """GitHub closes a ticket on merge; when the `finish` run was lost the ticket is closed but still in a PR stage.
    A closed ticket there whose PR is merged gets the `finish` outcome, as `gate.py sweep --issue <n>` would."""
    import gate
    still_open = set(open_listed)
    closed = [n for n in dict.fromkeys(tracker.sweep_tickets(stages)) if n not in still_open]  # ids mix numbers and refs
    gate.prefetch(tracker, closed)
    for n in closed:
        try:
            if tracker.facts(n).get("pr_state") != "merged":
                continue
            r = {"ticket": n, "reconcile": "finish", "why": "closed on merge with no finish outcome"}
            results.append(r)
            if not dry_run:
                gate.sweep(tracker, nodes, issue=n)
        except Exception as e:  # one ticket failing does not stop the others
            r = {"ticket": n, "error": f"{type(e).__name__}: {e}"}
            results.append(r)
        if progress:
            progress(r)


def fact_value(key, facts):
    """The fact a `when` key reads, as text for a person."""
    if key == "fix-rounds":
        return f"{facts.get('fix_rounds')}/{facts.get('fix_cap')}"
    if key == "label":
        return ", ".join(sorted({*(facts.get("attrs") or []), *(facts.get("pr_labels") or [])})) or "none"
    return facts.get({"blockers": "blockers_open", "pr-state": "pr_state"}.get(key, key))


def stuck_label(wakes, nodes, facts, on_pr, ago):
    """A waking label older than 2 timeouts that wakes no node: every node it triggers has a `when` that fails."""
    import gate
    by_label = {}
    for node, label, timeout in wakes:
        by_label.setdefault(label, []).append((node, timeout))
    for label, woken in by_label.items():
        on = "issue" if label in facts.get("attrs", []) else "pr" if label in on_pr else None
        at = (facts.get("label_at") or {}).get(label)
        if not on or not at or ago(at) <= timedelta(minutes=2 * max(t for _, t in woken)):
            continue
        if any(gate.holds(nodes[node].get("when") or {}, facts, {}) for node, _ in woken):
            continue
        guards = [f"{node} when.{k}: want {v}, have {fact_value(k, facts)}" for node, _ in woken
                  for k, v in (nodes[node].get("when") or {}).items() if not gate.holds({k: v}, facts, {})]
        return ("stuck", woken[0][0], label, on, f"{label} wakes no node. Why: {'; '.join(guards)}")
    return None


def last_end_failure(facts, node, head):
    """The error block of the last `[factory] end failed:` comment for `node` at `head`, else None (#997)."""
    for c in reversed(facts.get("recover_comments", [])):
        if c.startswith("[factory] end failed:") and f"node={node} " in c and f"head={head} " in c:
            body = c.split("\n", 1)[1] if "\n" in c else ""
            return body.split("The outcome did not land")[0].strip() or "no error output"
    return None


def send(tracker, n, nodes, stage, head, kind, label, on, why, fix_stage="changes-requested"):
    def apply(**e):
        return tracker.apply({"ticket": n, "run_id": RUN, **e})
    if kind == "end-failed":
        apply(kind="label", value=vocab.needs_human())
        apply(kind="comment", value=f"{PREFIX} stage={stage} head={head} end failed, not re-fired: {why}\n"
                                    f"Options: fix the cause (a new head, a token, a setting), then set {vocab.stage('ready')} "
                                    f"or re-add `{label}`.")
        apply(kind="stage", value="blocked")
    elif kind == "stuck":
        apply(kind="label", value=vocab.needs_human())
        apply(kind="comment", value=f"{PREFIX} stage={stage} head={head} stuck: {why}\nOptions: fix the cause and set "
                                    f"{vocab.stage('ready')}, or remove `{label}` and move the ticket by hand.")
        apply(kind="stage", value="blocked")
    elif kind == "conflict":
        apply(kind="label", value=label or vocab.cmd("fix-<tier>", "cheap"), on="pr")
        apply(kind="stage", value=fix_stage)
    elif kind == "blocked":
        apply(kind="label", value=vocab.needs_human())
        apply(kind="comment", value=f"{PREFIX} stage={stage} stopped: {why} twice. A person decides, then sets {vocab.stage('ready')}.")
        apply(kind="stage", value="blocked")
    else:  # the comment last: the counter for the second miss
        apply(kind="unlabel", value=label, on=on)
        apply(kind="label", value=label, on=on)
        apply(kind="comment", value=f"{PREFIX} stage={stage} head={head} re-fired {label} ({why})")


def _outcome_effects(node):
    for oc in (node.get("outcomes") or {}).values():
        yield (oc or {}).get("effects") or []


def definition_recovery(defn, tracker_stages, needs_human, default):
    """From the team's workflow.yaml: (fix, wakes). `fix` is (node, label, stage) of the node that runs in `fix` mode,
    the stage being the one its label arrives with (a review outcome's effects); `wakes` is (node, label, timeout) per
    label trigger of an agent node that an outcome of the node removes (a label no outcome removes proves nothing)."""
    nodes, agents = defn["workflow"].get("nodes") or {}, defn["factory"].get("agents") or {}
    skip = {needs_human, *tracker_stages.values()}
    fix, wakes = None, []
    for name, n in nodes.items():
        if (agents.get(n.get("agent")) or {}).get("harness") in definition.NO_ROLE:
            continue
        removed = {e["unlabel"] for fx in _outcome_effects(n) for e in fx if "unlabel" in e}
        for t in n.get("triggers") or []:
            label = t.get("label") if isinstance(t, dict) else None
            if label and label in removed and label not in skip:
                wakes.append((name, label, n.get("timeout_minutes", default)))
                if (n.get("settings") or {}).get("mode") == "fix" and fix is None:
                    stage = next((e["stage"] for m in nodes.values() for fx in _outcome_effects(m)
                                  if {"label": label, "on": "pr"} in fx for e in fx if "stage" in e), None)
                    fix = (name, label, stage) if stage else None
    return fix, wakes


def recover_definition(tracker, defn, stages, stage_kinds, limits, now=None, dry_run=False, progress=None):
    """ADR-0021 teams: the lost wakes `wake_ready` does not cover, read from the definition (no node, label or
    stage name here). A conflicting PR in a fix-able stage with no fix label goes to fix; a waking label whose node
    `when` holds and that outlived the node's timeout is re-fired once per head, the second miss blocks the ticket."""
    import gate
    now, results = now or datetime.now(timezone.utc), []
    default = limits.get("run_timeout_minutes", 30)
    fix, wakes = definition_recovery(defn, vocab._tracker()["stages"], vocab.needs_human(), default)
    nodes = defn["workflow"]["nodes"]
    listed = tracker.sweep_tickets([s for s in stages if stage_kinds.get(s) not in ("waiting", "final")], open_only=True)
    pending = []
    gate.prefetch(tracker, listed)
    for n in listed:
        try:
            facts = tracker.stale_facts(n)
            stage, since, head = facts["stage"], facts.get("since"), (facts.get("head") or "")[:12]
            if facts.get("issue_state", "open") != "open" or not since or vocab.needs_human() in facts.get("attrs", []):
                continue
            ago = lambda at: now - datetime.fromisoformat(at.replace("Z", "+00:00"))  # noqa: E731
            step = None
            on_pr = facts.get("pr_labels") or []
            replay = gate.stale_review_plan(facts, defn["workflow"]) if stage == "in-review" else None
            if replay:  # the review landed at this head but its outcome did not finish: apply it, never re-fire the reviewer
                node, outcome, payload = replay
                if ago(since) > timedelta(minutes=nodes[node].get("timeout_minutes", default)):
                    r = {"ticket": n, "stage": stage, "node": node, "outcome": outcome, "reconcile": "reapply",
                         "why": "review marker landed at the head; finish its outcome"}
                    results.append(r)
                    if not dry_run:
                        _, out = gate.apply(node, outcome, {"ticket": n, **payload, "run_id": RUN}, tracker, defn["workflow"])
                        r.update(applied=out["applied"], written=out.get("written", []))
                    if progress:
                        progress(r)
                continue
            if (fix and facts.get("conflict") and facts.get("pr") and facts.get("pr_state") in (None, "none", "open")
                    and fix[1] not in on_pr and gate.holds(nodes[fix[0]].get("when") or {}, facts, {})
                    and ago(since) > timedelta(minutes=default)):
                step = ("conflict", fix[0], fix[1], "pr", "the PR conflicts with main and has no fix label")
            else:
                for node, label, timeout in wakes:
                    on = "issue" if label in facts.get("attrs", []) else "pr" if label in on_pr else None
                    at = (facts.get("label_at") or {}).get(label)
                    if on and at and ago(at) > timedelta(minutes=timeout) and gate.holds(nodes[node].get("when") or {}, facts, {}):
                        step = ("refire", node, label, on, f"no {node} outcome")
                        break
                if step is None and not facts.get("blockers_open"):
                    step = stuck_label(wakes, nodes, facts, on_pr, ago)
            if step is None:
                continue
            kind, node, label, on, why = step
            if kind == "stuck" and any(f"stage={stage} head={head} stuck:" in c for c in facts.get("recover_comments", [])):
                continue
            failed = last_end_failure(facts, node, head)
            if failed and kind == "refire":  # #997: a retry on this head fails the same way; a person fixes the cause
                if any(f"stage={stage} head={head} end failed" in c for c in facts.get("recover_comments", [])):
                    continue
                kind, why = "end-failed", f"`factory end` of {node} failed on this head:\n{failed}"
            elif failed and kind == "stuck":
                why = f"{why}\nLast error of {node} on this head:\n{failed}"
            missed = sum(f"stage={stage} head={head} re-fired {label} (" in c for c in facts.get("recover_comments", []))
            kind = "blocked" if kind == "refire" and missed else kind
            r = {"ticket": n, "stage": stage, "node": node, "reconcile": kind, "why": why}
            results.append(r)
            pending.append((r, (tracker, n, None, stage, head, kind, label, on, why), fix[2] if fix else None))
        except Exception as e:  # one ticket failing does not stop the others
            results.append(ticket_error(tracker, n, e, dry_run))
            if progress:
                progress(results[-1])
    # Many second misses in one run are one factory failure (a broken install, #973), not a fault of each ticket:
    # re-fire them all and let reconcile_main open one Doctor ticket, instead of blocking every ticket (#985).
    systemic = sum(r["reconcile"] == "blocked" for r, _, _ in pending) >= SYSTEMIC_MISSES
    for r, args, fix_stage in pending:
        if systemic and r["reconcile"] == "blocked":
            r.update(reconcile="refire", systemic=True)
            args = (*args[:5], "refire", *args[6:])
        if not dry_run:
            send(*args, fix_stage=fix_stage)
        if progress:
            progress(r)
    return results


def wake_approved(tracker, nodes, minutes, now=None, dry_run=False):
    """`blockers: merge`: an `approved` ticket that had blockers, all now closed, whose merge label is older than
    `minutes` did not merge (a blocker closed by hand, or the wake was lost): re-fire the merge node's label."""
    if gate_blockers(nodes) != "merge":
        return []
    now, out = now or datetime.now(timezone.utc), []
    for n in tracker.sweep_tickets(["approved"], open_only=True):
        facts = tracker.stale_facts(n)
        had = tracker.blocked_by(n) if hasattr(tracker, "blocked_by") else facts.get("blocked_by") or []
        if facts["stage"] != "approved" or facts.get("blockers_open") or not had or facts.get("issue_state", "open") != "open":
            continue
        for node, label, on, why in wakes(nodes, facts):
            at = (facts.get("label_at") or {}).get(label) or facts.get("stage_at")
            if (nodes["nodes"][node].get("when") or {}).get("blockers") != "closed" or not at or \
                    now - datetime.fromisoformat(at.replace("Z", "+00:00")) < timedelta(minutes=minutes):
                continue
            if not dry_run:
                send(tracker, n, nodes, "approved", (facts.get("head") or "")[:12], "refire", label, on,
                     "the last blocker closed")
            out.append({"ticket": n, "node": node, "outcome": "refire", "label": label,
                        "why": "approved, unblocked and not merged"})
            break
    return out


def gate_blockers(nodes):
    import gate
    return gate.blockers_mode(nodes)


def wake_ready(tracker, minutes, now=None, dry_run=False):
    """ADR-0021: a `ready` ticket that had blockers, all now closed, whose label is older than `minutes` was not
    started (a blocker closed by hand, or the wake was lost): remove and re-add `ready` so its trigger fires again.
    A ready ticket that never had a blocker is left alone: nothing it waited on has changed."""
    now = now or datetime.now(timezone.utc)
    label, out = vocab.stage("ready"), []
    for n in tracker.sweep_tickets(["ready"], open_only=True):
        facts = tracker.stale_facts(n)
        since = facts.get("stage_at")
        had = tracker.blocked_by(n) if hasattr(tracker, "blocked_by") else facts.get("blocked_by") or []
        if facts["stage"] != "ready" or facts.get("blockers_open") or not had or not since or \
                now - datetime.fromisoformat(since.replace("Z", "+00:00")) < timedelta(minutes=minutes):
            continue
        if not dry_run:
            tracker.apply({"kind": "unlabel", "ticket": n, "value": label})
            tracker.apply({"kind": "label", "ticket": n, "value": label})
        out.append({"ticket": n, "outcome": "refire", "label": label, "why": "ready, unblocked and not started"})
    return out


def main(argv, run=None):
    """`factory reconcile`; a crash opens or updates `Doctor: reconcile fails`, and a clean run closes it (#845)."""
    argv = config_mod.pop_team(argv)
    if "--dry-run" in argv or any(a.startswith("memory:") for a in argv):
        return reconcile_main(argv)
    import doctor
    from factory_plugins.github import tracker as github_tracker
    run = run or github_tracker.gh
    settings = config_mod.load()
    repo = settings["tracker"].get("repository") or config_mod.vcs(settings)["repository"]
    try:
        code = reconcile_main(argv)
    except Exception as e:
        doctor.file_tickets(repo, [{"probe": "reconcile", "result": "fail", "detail": f"{type(e).__name__}: {e}"[:500]}],
                            run, run_id=RUN)
        raise
    doctor.close_ticket(repo, "reconcile", run, run_id=RUN)
    return code


def report_systemic(config, systemic, run=None):
    """Open `Doctor: runs end without an outcome` (no new comment while it is open), or close it when no run missed."""
    import doctor
    from factory_plugins.github import tracker as github_tracker
    run = run or github_tracker.gh
    repo = config["tracker"].get("repository") or config_mod.vcs(config)["repository"]
    probe = "runs-without-outcome"
    if not systemic:
        return doctor.close_ticket(repo, probe, run, run_id=RUN)
    title = f"Doctor: {probe} fails"
    found = json.loads(run(["issue", "list", "-R", repo, "--state", "open", "--search", f'"{title}" in:title',
                            "--json", "number,title"]))
    if not any(i["title"] == title for i in found):
        listed = ", ".join(f"#{r['ticket']} {r['node']}" for r in systemic)
        doctor.file_tickets(repo, [{"probe": probe, "result": "fail", "detail": f"{len(systemic)} runs ended twice "
                                    f"without an outcome in one reconcile: {listed}. Reconcile re-fired them instead of "
                                    f"blocking each; check the install or the environment first (#973)."}], run, run_id=RUN)


def reconcile_main(argv):
    dry_run = "--dry-run" in argv
    spec = next((a[7:] for a in argv if a.startswith("memory:")), None)
    if spec:
        from gate_port import MemorySystem
        tracker = MemorySystem(json.loads(Path(spec).read_text()))
    else:
        import gate
        tracker = gate.tracker_of(config_mod.load())
    config = config_mod.load()
    if definition.present(config_mod.team_root()):  # ADR-0021: the waits the gate cannot see, from the definition
        progress = lambda r: print(json.dumps(r), file=sys.stderr, flush=True)  # noqa: E731
        minutes = config["limits"].get("run_timeout_minutes", 30)
        results = wake_ready(tracker, minutes, dry_run=dry_run)
        results += wake_approved(tracker, nodes_mod.load(), minutes, dry_run=dry_run)
        results += recover_definition(tracker, definition.load(config_mod.team_root()), config["tracker"]["stages"],
                                      config["tracker"].get("stage_kinds", {}), config["limits"],
                                      dry_run=dry_run, progress=progress)
        if not dry_run and not spec:
            report_systemic(config, [r for r in results if r.get("systemic")])
        print(json.dumps({"dry_run": dry_run, "reconcile": results}))
        return 1 if any("error" in r for r in results) else 0
    progress = lambda r: print(json.dumps(r), file=sys.stderr, flush=True)  # noqa: E731
    results, failed = reconcile(tracker, nodes_mod.load(), list(config["tracker"]["stages"]), config["limits"],
                                stage_kinds=config["tracker"].get("stage_kinds", {}),
                                dry_run=dry_run, progress=progress)
    out = {"dry_run": dry_run, "reconcile": results}
    if not spec:  # the plugins' scheduled passes (github: the CI label sync); each reports its own errors
        import plugins
        for passes in plugins.registry(config).hook("reconcile.passes"):
            for name, (got, pass_failed) in passes(config, dry_run, progress).items():
                out[name], failed = got, failed or pass_failed
    print(json.dumps(out))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

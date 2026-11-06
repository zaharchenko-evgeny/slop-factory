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
import nodes as nodes_mod
import vocab

PREFIX = "[factory] reconcile:"
RUN = "reconcile"
LOST_FINISH = ("approved", "in-review", "changes-requested")
RED_TITLE = re.compile(r"Main is red(?: in Air)?: (.+)")


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
            if not label.startswith("factory:"):
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
            results.append({"ticket": n, "error": f"{type(e).__name__}: {e}"})
            if progress:
                progress(results[-1])
    return results, failed


def finish_lost(tracker, nodes, stages, open_listed, dry_run, results, progress):
    """GitHub closes a ticket on merge; when the `finish` run was lost the ticket is closed but still in a PR stage.
    A closed ticket there whose PR is merged gets the `finish` outcome, as `gate.py sweep --issue <n>` would."""
    import gate
    closed = sorted(set(tracker.sweep_tickets(stages)) - set(open_listed))
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


def send(tracker, n, nodes, stage, head, kind, label, on, why):
    def apply(**e):
        return tracker.apply({"ticket": n, "run_id": RUN, **e})
    if kind == "conflict":
        apply(kind="label", value=vocab.cmd("fix-<tier>", "cheap"), on="pr")
        apply(kind="stage", value="changes-requested")
    elif kind == "blocked":
        apply(kind="label", value=vocab.needs_human())
        apply(kind="comment", value=f"{PREFIX} stage={stage} stopped: {why} twice. A person decides, then sets {vocab.stage('ready')}.")
        apply(kind="stage", value="blocked")
    else:  # the comment last: the counter for the second miss
        apply(kind="unlabel", value=label, on=on)
        apply(kind="label", value=label, on=on)
        apply(kind="comment", value=f"{PREFIX} stage={stage} head={head} re-fired {label} ({why})")


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


def main(argv):
    argv = config_mod.pop_team(argv)
    dry_run = "--dry-run" in argv
    spec = next((a[7:] for a in argv if a.startswith("memory:")), None)
    if spec:
        from gate_port import MemoryTracker
        tracker = MemoryTracker(json.loads(Path(spec).read_text()))
    else:
        from gate_github import GitHubGateTracker
        tracker = GitHubGateTracker.from_config()
    config = config_mod.load()
    import definition
    if definition.present(config_mod.team_root()):  # ADR-0021: only the waits the gate cannot see happen
        results = wake_ready(tracker, config["limits"].get("run_timeout_minutes", 30), dry_run=dry_run)
        print(json.dumps({"dry_run": dry_run, "reconcile": results}))
        return 0
    progress = lambda r: print(json.dumps(r), file=sys.stderr, flush=True)  # noqa: E731
    results, failed = reconcile(tracker, nodes_mod.load(), list(config["tracker"]["stages"]), config["limits"],
                                stage_kinds=config["tracker"].get("stage_kinds", {}),
                                dry_run=dry_run, progress=progress)
    out = {"dry_run": dry_run, "reconcile": results}
    if not spec:
        import gate
        try:
            out["ci_sync"], sync_failed = gate.run_ci_sync(dry_run, progress)
        except Exception as e:  # the reconcile results above are still reported
            out["ci_sync"], sync_failed = [{"error": f"{type(e).__name__}: {e}"}], True
        failed = failed or sync_failed
    print(json.dumps(out))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""`factory reconcile`: re-fire the waking label of a node whose outcome is missing, from the state alone.

Usage: reconcile.py [--dry-run] [--tracker memory:FILE]
No model and no Air quota: the expected next step of each ticket comes from workflows/nodes.yaml (the node whose
`when` matches the ticket's state and whose trigger is a label). A step with no outcome after `limits.run_timeout_minutes`
(a node's `timeout_minutes` overrides it) gets its label once; a second miss sets `blocked` and `factory:needs-human`.
A ticket in a waiting stage, closed, or already marked `factory:needs-human` is left alone.
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod
import nodes as nodes_mod

PREFIX = "[factory] reconcile:"
RUN = "reconcile"


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
            elif not label.startswith("factory:ci-") or label == f"factory:ci-{facts.get('ci')}":
                yield name, label, on, f"no {name} outcome"


def reconcile(tracker, nodes, stages, limits, now=None, dry_run=False, progress=None, stage_kinds=None):
    """Returns (results, failed). One re-fire per ticket and stage; the second miss blocks the ticket."""
    now, results, failed = now or datetime.now(timezone.utc), [], False
    default = limits.get("run_timeout_minutes", 30)
    stage_kinds = stage_kinds or {}
    for n in tracker.sweep_tickets(stages):
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
            if (facts.get("issue_state", "open") != "open" or not since
                    or "factory:needs-human" in facts.get("attrs", [])):
                continue
            age = now - datetime.fromisoformat(since.replace("Z", "+00:00"))
            conflict = (stage == "in-review" and facts.get("conflict") and facts.get("pr")
                        and not facts.get("command") and facts.get("pr_state") in (None, "none", "open"))
            conflicting_fix = (stage == "changes-requested" and facts.get("conflict")
                               and (facts.get("command") or "").startswith("factory:fix-"))
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
                                 if label.startswith(("factory:review-", "factory:rereview-"))
                                 and now - datetime.fromisoformat(at.replace("Z", "+00:00")) > timedelta(minutes=default)]
                stale_focuses = {label.removeprefix("factory:review-").removeprefix("factory:rereview-")
                                 for label in stale_reviews}
                stale_step = next((w for w in wakes(nodes, facts)
                                   if w[0] == "review" and w[1].removeprefix("factory:review-") in stale_focuses), None)
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


def send(tracker, n, nodes, stage, head, kind, label, on, why):
    def apply(**e):
        return tracker.apply({"ticket": n, "run_id": RUN, **e})
    if kind == "conflict":
        apply(kind="label", value="factory:fix-cheap", on="pr")
        apply(kind="stage", value="changes-requested")
    elif kind == "blocked":
        apply(kind="label", value="factory:needs-human")
        apply(kind="comment", value=f"{PREFIX} stage={stage} stopped: {why} twice. A person decides, then sets factory:ready.")
        apply(kind="stage", value="blocked")
    else:  # the comment last: the counter for the second miss
        apply(kind="unlabel", value=label, on=on)
        apply(kind="label", value=label, on=on)
        apply(kind="comment", value=f"{PREFIX} stage={stage} head={head} re-fired {label} ({why})")


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
    results, failed = reconcile(tracker, nodes_mod.load(), list(config["tracker"]["stages"]), config["limits"],
                                stage_kinds=config["tracker"].get("stage_kinds", {}),
                                dry_run=dry_run, progress=lambda r: print(json.dumps(r), file=sys.stderr, flush=True))
    print(json.dumps({"dry_run": dry_run, "reconcile": results}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

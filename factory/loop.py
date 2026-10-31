#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The local runner's loop core: one tick over workflows/nodes.yaml.

Usage: loop.py --once [--node N] [--issue N]
Per ticket with a `factory:*` stage, for every enabled node: `gate.check`; on go run the node's role
(`run_role`, or a `script` node's function with no agent), then `gate.apply` with the outcome. A schedule node
runs when its cron fell due since `.factory/runs/loop/last-tick`. At most `limits.concurrent_deliveries`
role runs per tick. A tick with nothing to do writes nothing.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate  # noqa: E402
import nodes as nodes_mod  # noqa: E402

ROOT = nodes_mod.ROOT


def cron_match(expr, t):
    """True when the 5-field cron `expr` matches minute `t` (`*`, `*/n`, `a-b`, lists, numbers)."""
    vals = [t.minute, t.hour, t.day, t.month, (t.weekday() + 1) % 7]
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    def ok(field, v, b):
        for part in field.split(","):
            rng, _, step = part.partition("/")
            lo, hi = b if rng == "*" else (int(rng.split("-")[0]), int(rng.split("-")[-1]))
            if lo <= v <= hi and (v - lo) % int(step or 1) == 0:
                return True
        return False
    return all(ok(f, v, b) for f, v, b in zip(expr.split(), vals, bounds))


def cron_due(expr, last, now):
    """A matching minute in (last, now]; looks back at most a week."""
    t = max(last, now - timedelta(days=7)).replace(second=0, microsecond=0) + timedelta(minutes=1)
    while t <= now:
        if cron_match(expr, t):
            return True
        t += timedelta(minutes=1)
    return False


def script_join(ctx):
    """The `join` node: approved when every review at the head approves, else changes."""
    head = [r for r in ctx["reviews"] if r["agent"] and r["commit_id"] == ctx["head"]]
    return ("approved" if head and all(r["verdict"] == "approve" for r in head) else "changes"), {"tier": "mid"}


SCRIPTS = {"join": script_join}


class Due:
    """The tracker with `due` overridden for a schedule node."""
    def __init__(self, tracker, due):
        self.tracker, self.due = tracker, due

    def facts(self, n):
        return {**self.tracker.facts(n), "due": self.due}

    def apply(self, effect):
        return self.tracker.apply(effect)


def default_run_role(node, check, root=ROOT):
    """Run the node's role through factory/run_role.py; return (outcome, payload) from its handoff."""
    import run_role
    local = yaml.safe_load((root / "factory.yaml").read_text())["targets"]["local"]
    d = root / local["run_dir"] / str(check["ticket"])
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{node}.in.md").write_text(json.dumps(check, indent=2))
    (d / f"{node}.handoff.json").unlink(missing_ok=True)
    if run_role.run(root, check["ticket"], node):
        return None, {}
    h = json.loads((d / f"{node}.handoff.json").read_text())
    return outcome_of(h), {**intake_fields(h), **h}


def intake_fields(h):
    """The fields `intake`'s proceed outcome needs and the router handoff lacks: complexity and tier from the size."""
    if h.get("handoff") != "ROUTER-INTAKE":
        return {}
    n = int("".join(c for c in str(h.get("size_estimate", "")) if c.isdigit()) or 0)
    return {"complexity": "small" if n <= 150 else "medium" if n <= 400 else "large",
            "tier": "cheap" if n <= 150 else "mid" if n <= 400 else "strong"}


def outcome_of(h):
    """The node outcome a handoff names: `outcome`, a reviewer `verdict`, a router intake, else `status`."""
    if h.get("outcome"):
        return h["outcome"]
    if h.get("verdict"):
        return "approve" if h["verdict"] == "approved" else "changes"
    if h.get("handoff") == "ROUTER-INTAKE":
        if h.get("kind") == "bug" and h.get("ready") == "yes":
            return "bug"
        return "proceed" if h.get("ready") == "yes" and h.get("split") in (None, "none") else (
            "split" if h.get("split") not in (None, "none") else "grill")
    return h.get("status")


STALE = ("factory:implement-", "factory:fix-")


def drop_stale_commands(tracker, n, errors):
    """A ticket back in `ready` (a human's `factory:ready`) loses its old command labels (`implement-*`, `fix-*`)."""
    try:
        f = tracker.facts(n)
        if f["stage"] == "ready":
            for label in f.get("attrs", []):
                if label.startswith(STALE):
                    tracker.apply({"kind": "unlabel", "ticket": n, "value": label})
    except (Exception, SystemExit) as e:
        errors.append(("stale-labels", n, f"{type(e).__name__}: {e}"))


def tick(tracker, tickets, nodes, config, run_role=default_run_role, scripts=None, now=None, last=None,
         only_node=None):
    """One pass. Returns ran [(node, ticket, outcome)], due (schedule nodes that ran), errors."""
    scripts, now = SCRIPTS if scripts is None else scripts, now or datetime.now()
    cap, ran = config["limits"]["concurrent_deliveries"], []
    schedules = nodes.get("schedules") or {}
    roles, moved, errors, due_ran = 0, set(), [], set()  # a ticket moves at most once per tick
    for n in tickets:
        drop_stale_commands(tracker, n, errors)
    for name, node in nodes["nodes"].items():
        if node.get("enabled") is False or node["role"] == "human" or only_node not in (None, name):
            continue
        sched = next((t.split()[1] for t in node.get("triggers", []) if t.startswith("schedule ")), None)
        is_due = False
        if sched:
            is_due = last is None or cron_due(schedules[sched]["cron"], last, now)
            if not is_due:
                continue
        view = Due(tracker, is_due)
        for n in tickets:
            if n in moved:
                continue
            try:
                head = view.facts(n)["head"]
                event = f"loop:{name}:{n}:{head}"
                code, chk = gate.check(name, {"id": event, "ticket": n}, view, nodes, config)
                if code != gate.GO:
                    continue
                is_script = node["role"] == "script"
                if is_script and name not in scripts:
                    continue
                if not is_script:
                    if roles >= cap:
                        continue
                    roles += 1
                outcome, extra = scripts[name](chk["context"]) if is_script else run_role(name, chk)
                if outcome is None:
                    continue
                if is_due:
                    due_ran.add(name)  # a due node that ran is handled, applied or not
                payload = {**extra, "ticket": n, "event": event, "head": head}
                missing = chk["context"]["focus_missing"]
                if missing and "focus" not in payload:
                    payload["focus"] = missing[0]
                code, res = gate.apply(name, outcome, payload, view, nodes)
            except (Exception, SystemExit) as e:  # one ticket's error never stops the others
                errors.append((name, n, f"{type(e).__name__}: {e}"))
                continue
            if code != gate.GO:
                continue  # stale head or duplicate: nothing was applied
            ran.append((name, n, outcome))
            moved.add(n)
    return {"ran": ran, "due": sorted(due_ran), "errors": errors}


def update_stamp(stamp, res, restricted, now=None):
    """Write the last-tick time only after an unrestricted tick in which a due schedule node ran."""
    if res["due"] and not restricted:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(now or time.time()))


def main(argv):
    p = argparse.ArgumentParser(prog="loop.py")
    p.add_argument("--once", action="store_true", required=True)
    p.add_argument("--node")
    p.add_argument("--issue", type=int)
    a = p.parse_args(argv)
    from gate_github import GitHubGateTracker
    from github_tracker import gh
    config = yaml.safe_load((ROOT / "factory.yaml").read_text())
    tracker = GitHubGateTracker.from_config()
    stamp = ROOT / config["targets"]["local"]["run_dir"] / "loop" / "last-tick"
    last = datetime.fromtimestamp(float(stamp.read_text())) if stamp.exists() else None
    if a.issue:
        tickets = [a.issue]
    else:
        out = json.loads(gh(["issue", "list", "-R", config["tracker"]["repository"], "--state", "open",
                             "--limit", "200", "--json", "number,labels"]))
        stages = set(config["tracker"]["stages"].values())
        tickets = [i["number"] for i in out if stages & {x["name"] for x in i["labels"]}]
    res = tick(tracker, tickets, nodes_mod.load(), config, last=last, only_node=a.node)
    for r in res["ran"]:
        print(*r)
    for r in res["errors"]:
        print("error", *r, file=sys.stderr)
    update_stamp(stamp, res, bool(a.node or a.issue))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

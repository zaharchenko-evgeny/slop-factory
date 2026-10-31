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
    def ok(field, v):
        for part in field.split(","):
            rng, _, step = part.partition("/")
            lo, hi = (0, 59) if rng == "*" else (int(rng.split("-")[0]), int(rng.split("-")[-1]))
            if lo <= v <= hi and (v - lo) % int(step or 1) == 0:
                return True
        return False
    return all(ok(f, v) for f, v in zip(expr.split(), vals))


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
    if run_role.run(root, check["ticket"], node):
        return None, {}
    h = json.loads((d / f"{node}.handoff.json").read_text())
    return h.get("outcome") or h.get("status"), h


def tick(tracker, tickets, nodes, config, run_role=default_run_role, scripts=None, now=None, last=None,
         only_node=None):
    """One pass. Returns {"ran": [(node, ticket, outcome)], "due": [schedule nodes that were due]}."""
    scripts, now = SCRIPTS if scripts is None else scripts, now or datetime.now()
    cap, ran, due = config["limits"]["concurrent_deliveries"], [], []
    schedules = nodes.get("schedules") or {}
    roles, moved = 0, set()  # a ticket moves at most once per tick
    for name, node in nodes["nodes"].items():
        if node.get("enabled") is False or node["role"] == "human" or only_node not in (None, name):
            continue
        sched = next((t.split()[1] for t in node.get("triggers", []) if t.startswith("schedule ")), None)
        is_due = False
        if sched:
            is_due = last is not None and cron_due(schedules[sched]["cron"], last, now)
            if not is_due:
                continue
            due.append(name)
        view = Due(tracker, is_due)
        for n in tickets:
            if n in moved:
                continue
            code, chk = gate.check(name, {"id": f"loop:{name}:{n}", "ticket": n}, view, nodes, config)
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
            payload = {**extra, "ticket": n, "event": f"loop:{name}:{n}:{chk['context']['head']}",
                       "head": chk["context"]["head"]}
            code, res = gate.apply(name, outcome, payload, view, nodes)
            ran.append((name, n, outcome))
            moved.add(n)
    return {"ran": ran, "due": due}


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
    if last is None or (res["due"] and not a.node and not a.issue):
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(time.time()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

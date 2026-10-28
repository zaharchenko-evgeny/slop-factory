#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Drive one ticket through the state machine (factory/sm.py) against a tracker adapter.

The adapter supplies the factory operations of docs/agents/issue-tracker.md.
`run` claims the ticket and executes every action the machine starts until it
waits on a human or external actor (`approved`, `blocked`, `failed`): script
actions here, agent actions through run_role.py (each reviewer in its own run
dir and detached checkout, the fallback model after a malformed handoff). Stage
and vars (with the `_run` state) are written through the adapter after each step; the run dir keeps
`sm.json`, `<step>.in.md` and `checks-<round>.md`. `pi` and `gh` can be
replaced with $FACTORY_AGENT and $FACTORY_GH.

CLI: uv run factory/drive.py run <ticket> [--tracker local:<effort dir>|github] [--executor NAME]
     uv run factory/drive.py branch <ticket> <ticket.json>  prints the ticket's branch name.
"""
import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

import preflight
import run_role
import sm
from local_tracker import LocalTracker

STOPWORDS = {"a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "with", "from", "by"}


def drive(m, tracker, nn, executor, results=(), execute=None):
    """Claim ticket `nn`, resume from the stored state, vars and run state (`sm.persisted`),
    apply each result with `sm.step`, write them back after every step, and release the claim
    once the ticket waits on a human or external actor, or when reading the ticket fails.
    `results` items use sm.parse_event's forms; once they run out, `execute(ticket)` gives the
    next one (None: stop). Return (ticket, trace)."""
    tracker.claim(nn, executor)  # raises before any write when another executor holds it
    try:
        ticket = sm.new_ticket(m, {"state": tracker.get_state(nn), "vars": tracker.read_vars(nn)})
    except BaseException:
        tracker.release(nn, executor)
        raise
    trace = []
    try:
        items, end = iter(results), object()
        while (item := next(items, end)) is not end or (item := execute and execute(ticket)) is not None:
            ticket, rep = sm.step(m, ticket, *sm.parse_event(item))
            trace.append(rep)
            tracker.set_state(nn, ticket["state"])
            tracker.write_vars(nn, sm.persisted(ticket))
            if rep["wait"]:
                break
    except BaseException:
        tracker.release(nn, executor)  # an exception must not leave the ticket claimed
        raise
    if m["states"][ticket["state"]]["actor"] != "agent":
        tracker.release(nn, executor)
    return ticket, trace


def slug(title, words=4):
    """A short branch slug from the title: lower-case words without stopwords."""
    kept = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in STOPWORDS]
    return "-".join(kept[:words]) or "ticket"


def branch_name(pattern, n, title, body=""):
    """`pattern` with the ticket's `slug:` line if the body has one, else a slug of the title."""
    m = re.search(r"^slug:[ \t]*([a-z0-9][a-z0-9-]*)[ \t]*$", body or "", re.M)
    return pattern.format(ticket=n, slug=m.group(1) if m else slug(title))


def sh(cmd, cwd):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"{' '.join(map(str, cmd[:3]))} failed: {(r.stderr or r.stdout).strip()[-500:]}")
    return r.stdout


def gh():
    return os.environ.get("FACTORY_GH", "gh")


SAFE_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
REVIEWERS = ("review_full", "review_behaviour", "review_structure_security")


class Delivery:
    """Executes the actions of one ticket; called by `drive` with the ticket, returns the next event."""

    def __init__(self, root, nn, tracker, run_id):
        self.root, self.nn, self.tracker, self.run_id = Path(root).resolve(), nn, tracker, run_id
        self.config = yaml.safe_load((self.root / "factory.yaml").read_text())
        self.m = sm.load_machine(self.root / "workflows/delivery.sm.yaml", self.root / "factory.yaml")
        local = self.config["targets"]["local"]
        self.run_dir = self.root / local["run_dir"] / str(nn)
        self.wt = self.root / local["worktree_dir"] / str(nn)

    def __call__(self, t):
        if not t["pending"]:
            return {"stage.ready": {"run_id": self.run_id}} if t["state"] == self.m["initial"] else None
        action = t["pending"][0]
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "sm.json").write_text(json.dumps(t, indent=2))  # run_role.py reads risk and pr_urls
        spec = self.m["actions"][action]
        if spec["kind"] != "script":
            result, payload = self.agent(t, action, spec)
        else:
            try:
                result, payload = getattr(self, action)(t)
            except (RuntimeError, OSError, ValueError) as e:
                result, payload = "failed", {"error": str(e)}
        return {f"{action}.{result}": payload}

    def issue(self):
        """(title and body, base branch): the body's `base:` line, else the primary repository's default."""
        i = self.tracker.issue(self.nn)
        m = re.search(r"^base:[ \t]*(\S+)", i.get("body") or "", re.M)
        primary = next(r for r in self.config["repositories"] if r.get("role") == "primary")
        base = m.group(1) if m else primary["default_branch"]
        # The body is untrusted: a leading `-` would reach git as an option (`--upload-pack=...`).
        if not SAFE_BRANCH.fullmatch(base) or ".." in base:
            raise ValueError(f"unsafe base branch {base!r} in the ticket")
        return i, base

    def branch(self, i):
        return branch_name(self.config["branch_pattern"], self.nn, i["title"] or "", i.get("body") or "")

    # ---- Script actions ----

    def preflight(self, t):
        bad = [m for m in preflight.run_models(self.config) if not preflight.check(m)[0]]
        if bad:
            return "failed", {"error": f"models not ready: {', '.join(bad)}"}
        b = self.branch(self.issue()[0])
        if sh(["git", "ls-remote", "--heads", "origin", b], self.root).strip():
            return "failed", {"error": f"branch {b} already exists on origin"}
        if json.loads(sh([gh(), "pr", "list", "--head", b, "--state", "open", "--json", "url"], self.root) or "[]"):
            return "failed", {"error": f"an open PR exists for {b}"}
        return "done", {}

    def start(self, t):
        i, base = self.issue()
        b = self.branch(i)
        if not self.wt.is_dir():
            sh(["git", "fetch", "origin", base], self.root)
            sh(["git", "worktree", "add", "-b", b, str(self.wt), f"origin/{base}"], self.root)
        return "done", {"branch": b}

    def checks(self, t):
        base, rows, report, failed = self.issue()[1], [], [], False
        for c in self.config.get("checks") or []:
            if c.get("stage", "pre-review") != "pre-review":
                continue
            if c.get("requires") and not shutil.which(c["requires"]):
                status, text = "skipped", f"{c['requires']} is not installed"
            else:
                r = subprocess.run(["bash", "-c", c["run"]], cwd=self.wt, capture_output=True, text=True,
                                   env={**os.environ, "FACTORY_BASE": base})
                status, text = ("pass" if r.returncode == 0 else "fail"), r.stdout + r.stderr
            failed |= status == "fail" and c.get("mode") == "blocking"
            rows.append({"name": c["name"], "mode": c.get("mode"), "status": status})
            tail = "\n".join(text.rstrip().splitlines()[-15:])
            report.append(f"## {c['name']} ({c.get('mode')}): {status}\n~~~~\n{tail}\n~~~~\n")
        path = self.run_dir / f"checks-{t['vars']['fix_round']}.md"
        path.write_text("\n".join(report))
        return ("failed" if failed else "passed"), {"checks": rows, "report": str(path)}

    def publish(self, t):
        i, base = self.issue()
        b = t["vars"]["branch"]
        sh(["git", "push", "-u", "origin", b], self.wt)
        out = sh([gh(), "pr", "create", "--head", b, "--base", base, "--title", f"#{self.nn} {i['title']}",
                  "--body", self.pr_body(t, i)], self.wt)
        return "done", {"pr_urls": [out.strip().splitlines()[-1]]}

    def pr_body(self, t, i):
        out, v = t["out"], t["vars"]
        dev = out.get("fix") or out.get("implement") or {}
        lines = [f"[factory] {i['title']}", "", f"Refs #{self.nn}", "", "## What changed",
                 dev.get("commits") or dev.get("files") or "See the commits.", "", "## Checks"]
        lines += [f"- {c['name']} ({c['mode']}): {c['status']}" for c in (out.get("checks") or {}).get("checks", [])]
        lines += ["", "## Review"]
        for a in (a for a in REVIEWERS if a in out):
            r = out[a]
            lines.append(f"- {a}, {r.get('model')}: {r.get('verdict')}, risk {r.get('risk')}, at {r.get('reviewed_sha')}")
            if r.get("fallback_from"):
                lines.append(f"  Review: {a} fell back from {r['fallback_from']} to {r.get('model')}")
        lines += [f"- Fix rounds: {v['fix_round']}", "", f"Factory-Run: {v['run_id'] or self.run_id}"]
        return "\n".join(lines) + "\n"

    # ---- Agent actions ----

    def role_input(self, t, action, spec):
        v, out = t["vars"], t["out"]
        lines = [f"mode: {spec.get('mode')}", f"ticket: {self.nn}", f"base: {self.issue()[1]}",
                 f"run_id: {v['run_id'] or self.run_id}"]
        if v["branch"]:
            lines.append(f"branch: {v['branch']}")
        if spec["role"] == "reviewer" and v["reviewed_sha"]:
            lines.append(f"reviewed_sha: {v['reviewed_sha']} (re-review: read the changes since it)")
        blocks = {}
        if action == "fix" and (out.get("checks") or {}).get("result") == "failed":
            blocks["checks"] = Path(out["checks"]["report"]).read_text()
        elif action == "fix":
            blocks = {a: out[a] for a in REVIEWERS if (out.get(a) or {}).get("verdict") == "changes-requested"}
        elif action == "summary":
            blocks = {a: out[a] for a in ("implement", "fix", "checks", *REVIEWERS, "publish") if a in out}
        for name, block in blocks.items():
            text = block if isinstance(block, str) else json.dumps(block, indent=2)
            lines += ["", f"## {name}", "", "~~~~", text.rstrip(), "~~~~"]
        return "\n".join(lines) + "\n"

    def agent(self, t, action, spec):
        n = t["vars"]["fix_round"]
        step = f"{action}-{n}" if n else action
        (self.run_dir / f"{step}.in.md").write_text(self.role_input(t, action, spec))
        text = self.tracker.path(self.nn).read_text() if isinstance(self.tracker, LocalTracker) else None
        if run_role.run(self.root, self.nn, action, step, ticket_text=text):
            return "error", {}
        h = json.loads((self.run_dir / f"{step}.handoff.json").read_text())
        if spec["role"] == "reviewer":
            h["blocking_ids"] = re.findall(r"^\s*-?\s*id:\s*(\S+)", h.get("blocking") or "", re.M)
        if spec["role"] != "developer":
            return "done", h
        return (h["status"] if h.get("status") in ("done", "blocked") else "error"), h


def run_ticket(root, nn, tracker, executor, run_id=None):
    """Deliver ticket `nn` until the machine waits on a human or external actor. Return (ticket, trace)."""
    d = Delivery(root, nn, tracker, run_id or f"local:{time.strftime('%Y%m%dT%H%M%S')}-{nn}")
    return drive(d.m, tracker, nn, executor, execute=d)


def main(argv=None):
    p = argparse.ArgumentParser(prog="drive.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="deliver a ticket until it waits on a human or external actor")
    r.add_argument("ticket")
    r.add_argument("--tracker", default="github", help="local:<effort dir> or github (default)")
    r.add_argument("--executor", help="claim name (default: the gh login, or the OS user for local)")
    b = sub.add_parser("branch", help="print the ticket's branch name")
    b.add_argument("ticket")
    b.add_argument("ticket_json")
    args = p.parse_args(argv)
    if not args.ticket.isdigit():
        p.error(f"ticket {args.ticket!r}: use the issue number")
    if args.cmd == "branch":
        pattern = yaml.safe_load((sm.ROOT / "factory.yaml").read_text())["branch_pattern"]
        t = json.loads(Path(args.ticket_json).read_text())
        print(branch_name(pattern, args.ticket, t.get("title") or "", t.get("body") or ""))
        return 0
    if args.tracker.startswith("local:"):
        tracker, executor = LocalTracker(args.tracker[len("local:"):]), args.executor or getpass.getuser()
    elif args.tracker == "github":
        from github_tracker import GitHubTracker
        tracker = GitHubTracker.from_config(sm.ROOT / "factory.yaml")
        executor = args.executor or tracker.login()
    else:
        p.error(f"--tracker {args.tracker!r}: use local:<effort dir> or github")
    t, trace = run_ticket(sm.ROOT, args.ticket, tracker, executor)
    for rep in trace:
        print(f"{rep['event']:<26} {rep['from']} -> {rep['to']}  {' '.join(rep['run'])}  {rep['note'] or ''}".rstrip())
    print(f"final: {t['state']}")
    return 0 if t["state"] == "approved" else 1


if __name__ == "__main__":
    sys.exit(main())

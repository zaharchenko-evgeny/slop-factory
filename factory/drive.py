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
`sm.json`, `<step>.in.md` and `checks-<round>.md`. After every reviewer approves, the approver (factory/approver.py, dry-run)
posts its verdict on the ticket (after the doc-writer, when a reviewer reported docs impact) and keeps the PR summary as `approve-<round>.json`. Publish relabels the ticket's risk and
docs-impact and posts the reviewers' nits as one PR comment; a later publish (after a fix round)
pushes and rebuilds the PR body. `ci` runs the blocking checks on the branch merged with the base, then
watches CI (`gh pr checks`, after waiting for the checks to register); a failure goes to a fix round with
the failing job log (`ci-<round>.md`). The reviewers of a fork run in parallel. Once `ci` moves the
ticket to `approved`, the driver runs the router summary (input: final stage, CI result, commits, event
log), posts its comment on the ticket, sets the PR body link to `Closes #N` (`Refs #N` when the summary says
`complete: no` or the intake proposed a split), and removes the worktree once CI passed. After `pr.merged` (stage `merged`) the retro
writes one entry: the run's numbers (cost, wall time, fix rounds, blocking findings, reviewer agreement,
the approver's verdict) and the retro role's manual steps and lessons, under `knowledge/<repo>/` on
factory.yaml `knowledge.branch` (never a default branch), pushed; then stage `done`, even if it failed. `pi` and `gh` can be replaced with
$FACTORY_AGENT and $FACTORY_GH.

When a step moves the ticket into a state with `on_enter` actions (e.g. `diagnose`, `notify`), the driver
runs them once at the end of the run, posts an agent action's `comment` on the ticket, and clears them.

A human state with `resume_on: comment` (`blocked`) also polls the ticket's comments (see
workflows/delivery.v1.sm.yaml): the new ones go to one cheap router `recheck`, then `comment.added` fires.

In a human or external state, `run` polls the machine's external events on that state's transitions
that have a detector in factory.yaml `events` (`github-pr-merged`: every PR in `pr_urls` is merged, read
with `gh pr view --json state,mergedAt`; `{run: <bash>}`: exit 0). The first one detected is fired; with
none it prints "waiting for <events>" and writes nothing. `loop` does this for every ticket whose stage
the machine can move (open and closed), and runs the tickets in agent states (the initial state: the
frontier), at most `limits.concurrent_deliveries` at a time and `limits.runs_per_day` per day, each at
most once per call; it cycles until a cycle runs nothing and no event moved a ticket (`--once`: one cycle).
`loop` first runs `distill` once per factory.yaml `knowledge.distill_every_days` (weekly): one PR to the
default branch that merges the new knowledge-inbox entries into the knowledge dir (the distill role).

CLI: uv run factory/drive.py run <ticket> [--tracker local:<effort dir>|github] [--executor NAME] [--resume]
     uv run factory/drive.py loop [--once] [--tracker ...] [--executor NAME]
     uv run factory/drive.py distill  opens the distill PR now, if there is a new inbox entry.
     uv run factory/drive.py branch <ticket> <ticket.json>  prints the ticket's branch name.
"""
import argparse
import copy
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import yaml

import approver
import harnesses
import model_access
import preflight
import run_role
import runlog
import sm_v1 as sm
from local_tracker import ClaimError, LocalTracker

MAIN_RED = "Main is red"


class ResumeError(Exception):
    """--resume on a ticket that has no resume point."""


STOPWORDS = {"a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "with", "from", "by"}


def drive(m, tracker, nn, executor, results=(), execute=None, resume=False, on_step=None):
    """Claim ticket `nn`, resume from the stored state, vars and run state (`sm.persisted`),
    apply each result with `sm.step`, write them back after every step, and release the claim
    once the ticket waits on a human or external actor, or when reading the ticket fails.
    A step that ends in `failed` also stores `_resume`: the stage and the actions that were running.
    `resume` restarts a `failed` ticket from it, with fresh retries.
    `results` items use sm.parse_event's forms; once they run out, `execute(ticket)` gives the
    next one (None: stop). `on_step(prev, ticket, report)` sees each applied event. Return (ticket, trace)."""
    tracker.claim(nn, executor)  # raises before any write when another executor holds it
    try:
        state, vars_ = tracker.get_state(nn), tracker.read_vars(nn)
        point = vars_.pop("_resume", None)
        if resume:
            if state != "failed" or not point:
                raise ResumeError(f"ticket {nn}: --resume needs a failed ticket with a stored resume point")
            state, vars_["_run"] = point["state"], {**vars_.get("_run", {}), **point["run"], "retries": {}}
        ticket = sm.new_ticket(m, {"state": state, "vars": vars_})
        if resume:
            tracker.set_state(nn, state)
    except BaseException:
        tracker.release(nn, executor)
        raise
    trace = []
    try:
        items, end = iter(results), object()
        while (item := next(items, end)) is not end or (item := execute and execute(ticket)) is not None:
            prev = ticket
            ticket, rep = sm.step(m, ticket, *sm.parse_event(item))
            trace.append(rep)
            if on_step:
                on_step(prev, ticket, rep)
            tracker.set_state(nn, ticket["state"])
            tracker.write_vars(nn, {**sm.persisted(ticket), **(
                {"_resume": resume_point(prev)} if ticket["state"] == "failed" else {})})
            if rep["wait"]:
                break
    except BaseException:
        tracker.release(nn, executor)  # an exception must not leave the ticket claimed
        raise
    if m["states"][ticket["state"]]["actor"] != "agent":
        tracker.release(nn, executor)
    return ticket, trace


def resume_point(t):
    """The stage and running actions of `t`; a fork reruns every branch that did not end `done`."""
    fork = copy.deepcopy(t["fork"])
    pending = list(t["pending"])
    if fork:
        pending = [b for b, r in fork["results"].items() if r != "done"]
        fork["results"].update({b: None for b in pending})
    return {"state": t["state"], "run": {"pending": pending, "fork": fork}}


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


def approval_lines(a):
    return [f"- {r}" for r in a["reasons"]] or ["- Every hard and soft rule passed; a human still merges."]
CI_WAIT = 1800  # seconds for CI to register and finish its checks
CI_POLL = 15  # seconds between reads while no checks are registered
NO_CHECKS = "no checks reported"


def cost_table(run_dir):
    """Per-call usage rows of `events.log` as a text table with a total ('' when nothing is recorded)."""
    f = Path(run_dir) / "events.log"
    rows = [r for r in map(json.loads, f.read_text().splitlines()) if r.get("event") == "usage"] if f.exists() else []
    if not rows:
        return ""
    head = f"{'step':<16}{'role':<10}{'model':<34}{'input':>9}{'output':>8}{'cacheR':>9}{'cacheW':>9}{'cost $':>8}"
    lines = [head] + [f"{r['step']:<16}{r['role']:<10}{r['model']:<34}{r['input']:>9}{r['output']:>8}"
                      f"{r['cacheRead']:>9}{r['cacheWrite']:>9}{r['cost']:>8.3f}" for r in rows]
    return "\n".join(lines + [f"{'total':<95}{sum(r['cost'] for r in rows):>8.3f}"])


def event_line(rep):
    return f"{rep['event']:<26} {rep['from']} -> {rep['to']}  {' '.join(rep['run'])}  {rep['note'] or ''}".rstrip()


class Delivery:
    """Executes the actions of one ticket; called by `drive` with the ticket, returns the next event."""

    def __init__(self, root, nn, tracker, run_id):
        self.root, self.nn, self.tracker, self.run_id = Path(root).resolve(), nn, tracker, run_id
        self.config = yaml.safe_load((self.root / "factory.yaml").read_text())
        self.m = sm.load_machine(self.root / "workflows/delivery.v1.sm.yaml", self.root / "factory.yaml")
        local = self.config["targets"]["local"]
        self.run_dir = self.root / local["run_dir"] / str(nn)
        self.wt = self.root / local["worktree_dir"] / str(nn)
        self.done = []  # (action, (result, payload)) of fork branches that ran together, not yet reported
        self.trace = []
        self.comments = []  # the new comments of a `recheck`

    def __call__(self, t):
        if not t["pending"]:
            return {"stage.ready": {"run_id": self.run_id}} if t["state"] == self.m["initial"] else None
        self.save(t)
        self.done = [d for d in self.done if d[0] in t["pending"]]
        if not self.done and t["fork"] and len(t["pending"]) > 1:
            with ThreadPoolExecutor(len(t["pending"])) as ex:  # each reviewer has its own dirs (run_role.py)
                self.done = list(zip(t["pending"], ex.map(lambda a: self.run(t, a), t["pending"])))
        action, (result, payload) = self.done.pop(0) if self.done else (t["pending"][0], self.run(t, t["pending"][0]))
        return {f"{action}.{result}": payload}

    def save(self, t):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "sm.json").write_text(json.dumps(t, indent=2))  # run_role.py reads risk and pr_urls

    def run(self, t, action):
        spec = self.m["actions"][action]
        if spec["kind"] != "script":
            return self.agent(t, action, spec)
        t0 = time.monotonic()
        try:
            return getattr(self, action)(t)
        except (RuntimeError, OSError, ValueError) as e:
            # `ci` failed means a fix round, so its unexpected errors map to `error` (stage failed).
            return ("error" if action == "ci" else "failed"), {"error": str(e)}
        finally:
            if action == "ci":
                self.emit("wait", what="ci", duration_s=round(time.monotonic() - t0, 1))

    def emit(self, event, **fields):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        runlog.emit(self.run_dir / "events.log", self.run_id, event, **fields)

    def log_merge(self):
        """The human merged: a `wait` (since the last stamped event, e.g. approval) and a `human` line."""
        elog = self.run_dir / "events.log"
        stamps = [e["ts"] for e in (json.loads(x) for x in elog.read_text().splitlines() if x.strip()) if e.get("ts")] \
            if elog.exists() else []  # old logs have usage lines without ts
        if stamps:
            now = datetime.now(timezone.utc).isoformat()
            self.emit("wait", what="merge", duration_s=round(runlog._secs(stamps[-1], now), 1))
        sm_file = self.run_dir / "sm.json"
        verdict = (json.loads(sm_file.read_text())["out"].get("approve") or {}).get("verdict") if sm_file.exists() else None
        self.emit("human", what="merge", decision="merged", approver=verdict)

    def on_step(self, prev, t, rep):
        """Log the transition, and a retry when the engine counted one."""
        self.emit("transition", **{"from": rep["from"], "on": rep["event"], "to": rep["to"], "note": rep["note"]})
        for action, n in t["retries"].items():
            if n > prev["retries"].get(action, 0):
                self.emit("retry", action=action, count=n)

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
        try:
            env = model_access.pi_env(self.config, self.root)
        except model_access.ModelAccessError as e:
            return "failed", {"error": f"model access: {e}"}
        pairs = preflight.run_pairs(self.config)
        stubs = [harnesses.STUBS[h] for h, _ in pairs if h in harnesses.STUBS]
        if stubs:  # one line, before any model call
            return "failed", {"error": stubs[0]}
        checks = [(h, m, preflight.check(m, env=env, harness=h)) for h, m in pairs]
        bad = [m if h == "pi" else f"{h} {m}" for h, m, (ok, _) in checks if not ok]
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
        base = self.issue()[1]
        failed, rows, report = self.run_checks(self.wt, base)
        path = self.run_dir / f"checks-{t['vars']['fix_round']}.md"
        path.write_text(report)
        if failed and not (self.issue()[0]["title"] or "").startswith(MAIN_RED):
            names = [r["name"] for r in rows if r["status"] == "fail" and r["mode"] == "blocking"]
            red = self.red_on_base(base, names)
            if red:
                name, text, commit = red
                owner = self.park(name, text, commit)
                return "main_red", {"check": name, "main_red": f"main_red: {name}", "owner": owner,
                                    "checks": rows, "report": str(path)}
        return ("failed" if failed else "passed"), {"checks": rows, "report": str(path)}

    def red_on_base(self, base, names):
        """Rerun the failed blocking checks on origin/<base> in a detached worktree: (check, report, breaking
        commit) of the first one that fails there, else None. Measured, not inferred from the diff."""
        tmp = self.wt.with_name(f"{self.nn}-base")
        sh(["git", "fetch", "origin", base], self.root)
        if tmp.exists():
            sh(["git", "worktree", "remove", "--force", str(tmp)], self.root)
        sh(["git", "worktree", "add", "--detach", str(tmp), f"origin/{base}"], self.root)
        try:
            for name in names:
                failed, _, report = self.run_checks(tmp, base, only=[name])
                if failed:
                    files = [f for f in dict.fromkeys(re.findall(r"[\w./-]+\.\w+", report)) if (tmp / f).is_file()]
                    try:
                        commit = sh(["git", "log", "-1", "--format=%h %s", f"origin/{base}", "--", *files], self.root).strip()
                    except (RuntimeError, OSError):
                        commit = ""
                    return name, report, commit or "unknown"
        finally:
            sh(["git", "worktree", "remove", "--force", str(tmp)], self.root)
        return None

    def park(self, name, report, commit):
        """The open `Main is red: <check>` ticket (created when missing); this ticket is blocked by it."""
        title = f"{MAIN_RED}: {name}"
        owner = self.tracker.find_open(title)
        if owner is None:
            owner = self.tracker.create_issue(
                title, f"[factory] Check `{name}` fails on the base branch.\n\nLast commit on the files involved: "
                       f"{commit}\n\n{report}\n\nFactory-Run: {self.run_id}", "low", "ready")
        self.tracker.add_blocker(self.nn, owner)
        return str(owner)

    def run_checks(self, cwd, base, blocking_only=False, only=None):
        """Run the pre-review checks in `cwd`: (a blocking one failed, rows, markdown report)."""
        rows, report, failed = [], [], False
        for c in self.config.get("checks") or []:
            if c.get("stage", "pre-review") != "pre-review" or (blocking_only and c.get("mode") != "blocking") \
                    or (only is not None and c["name"] not in only):
                continue
            if c.get("requires") and not shutil.which(c["requires"]):
                status, text = "skipped", f"{c['requires']} is not installed"
            else:
                r = subprocess.run(["bash", "-c", c["run"]], cwd=cwd, capture_output=True, text=True,
                                   env={**os.environ, "FACTORY_BASE": base})
                status, text = ("pass" if r.returncode == 0 else "fail"), r.stdout + r.stderr
            failed |= status == "fail" and c.get("mode") == "blocking"
            skips = re.findall(r"skipped=(\d+)|^.*\.\.\. skipped '(.*)'$", text, re.M)
            if skips:  # a passing check that skipped tests (e.g. a missing tool) did not run them all
                text = f"NOTE: {len(skips)} skip report(s) in the output, those tests did not run\n" + text
            rows.append({"name": c["name"], "mode": c.get("mode"), "status": status})
            lines = text.rstrip().splitlines()
            tail = "\n".join(lines[:1] * bool(skips) + lines[-15:])
            report.append(f"## {c['name']} ({c.get('mode')}): {status}\n~~~~\n{tail}\n~~~~\n")
        return failed, rows, "\n".join(report)

    def approve(self, t):
        """The approver (dry-run): record its verdict and reasons on the ticket; publish adds them to the PR."""
        i, base = self.issue()
        out = t["out"]
        ci = "passed" if (out.get("checks") or {}).get("result") == "passed" else "unknown"
        a = approver.decide(self.root, self.config, self.wt, base, {r: out[r] for r in REVIEWERS if r in out}, ci,
                            t["vars"]["risk"], f"{i['title']}\n\n{i.get('body') or ''}",
                            i.get("labels") or (), (out.get("intake") or {}).get("complexity") or "")
        path = self.run_dir / f"approve-{t['vars']['fix_round']}.json"
        path.write_text(json.dumps(a["summary"], indent=2))
        self.tracker.comment(self.nn, "\n".join([f"[factory] Approver{' (dry-run)' * bool(self.config['roles']['approver'].get('dry_run', True))}: {a['verdict']}", "",
                                                 *approval_lines(a), "", self.trailer(t)]))
        return "done", {"verdict": a["verdict"], "reasons": a["reasons"], "summary": str(path)}

    def publish(self, t):
        i, base = self.issue()
        b, out = t["vars"]["branch"], t["out"]
        sh(["git", "push", "-u", "origin", b], self.wt)
        reviews = [out[a] for a in REVIEWERS if a in out]
        self.tracker.set_review_labels(self.nn, t["vars"]["risk"], any(r.get("docs_impact") == "yes" for r in reviews))
        if t["vars"]["pr_urls"]:  # after a fix round: the PR exists, rebuild its body
            # shortcut: nits of a fix-round review are not posted again; post them if they matter.
            for url in t["vars"]["pr_urls"]:
                sh([gh(), "pr", "edit", url, "--body", self.pr_body(t, i, base)], self.wt)
            return "done", {"pr_urls": t["vars"]["pr_urls"]}
        url = sh([gh(), "pr", "create", "--head", b, "--base", base, "--title", f"#{self.nn} {i['title']}",
                  "--body", self.pr_body(t, i, base)], self.wt).strip().splitlines()[-1]
        nits = [f"### {a}\n\n" + "\n".join(f"- {x['file']} — {x['finding']}" for x in minor)
                for a in REVIEWERS if (minor := [x for x in (out.get(a) or {}).get("findings", [])
                                                 if x["severity"] == "minor"])]
        if nits:
            body = "\n\n".join(["[factory] Review nits (advisory, not blocking)", *nits, self.trailer(t)])
            sh([gh(), "pr", "comment", url, "--body", body], self.wt)
        return "done", {"pr_urls": [url]}

    def ci(self, t):
        """`failed` when the blocking checks fail on the branch merged with the current base, else the PR's
        CI result. A failure's report (`ci-<round>.md`) is the fix round's checks block."""
        base = self.issue()[1]
        status, text = self.merged_checks(base)
        for url in t["vars"]["pr_urls"] if status == "passed" else ():
            status, text = self.read_ci(url)
            if status != "passed":
                break
        path = self.run_dir / f"ci-{t['vars']['fix_round']}.md"
        path.write_text(text)
        hold = self.config["tracker"].get("hold_label", "factory:hold")
        merge = status == "passed" and approver.may_merge(self.config, t["vars"]["risk"],
                                                          self.issue()[0].get("labels") or (), hold)
        return status, {"ci": text.splitlines()[0] if text else status, "report": str(path),
                        "merge": "yes" if merge else "no"}

    def merge(self, t):
        """Auto-merge: `moved` when the branch head or the base is not the one that was reviewed and checked,
        `failed` when the blocking checks fail on the branch merged with the base, else merge every PR
        pinned to the reviewed head (`gh` refuses a moved head: `failed`). A ticket that did not merge goes to `approved`."""
        base, sha = self.issue()[1], t["vars"]["reviewed_sha"]
        sh(["git", "fetch", "origin", base], self.root)
        before = sh(["git", "rev-parse", f"origin/{base}"], self.root)
        if sh(["git", "rev-parse", "HEAD"], self.wt).strip() != sha:
            return "moved", {"error": f"the branch head is not the reviewed {sha}"}
        status, text = self.merged_checks(base)
        sh(["git", "fetch", "origin", base], self.root)
        if status != "passed":
            return "failed", {"error": text.splitlines()[0]}
        if sh(["git", "rev-parse", f"origin/{base}"], self.root) != before:
            return "moved", {"error": f"origin/{base} moved during the checks"}
        if self.config["tracker"].get("hold_label", "factory:hold") in (self.issue()[0].get("labels") or ()):
            return "failed", {"error": "the ticket is on hold"}  # added while the checks ran
        for url in t["vars"]["pr_urls"]:
            sh([gh(), "pr", "merge", url, "--merge", "--match-head-commit", sha], self.root)
        return "done", {"merged": t["vars"]["pr_urls"]}

    def merged_checks(self, base):
        """(`passed` | `failed`, report): the blocking checks on HEAD merged with origin/<base>, in a
        temporary detached worktree, so two PRs that pass alone cannot break the base together."""
        tmp = self.wt.with_name(f"{self.nn}-merge")
        sh(["git", "fetch", "origin", base], self.root)
        if tmp.exists():
            sh(["git", "worktree", "remove", "--force", str(tmp)], self.root)
        sh(["git", "worktree", "add", "--detach", str(tmp), "HEAD"], self.wt)
        try:
            r = subprocess.run(["git", "-c", "user.name=factory", "-c", "user.email=factory@invalid", "merge",
                                "--no-edit", f"origin/{base}"], cwd=tmp, capture_output=True, text=True)
            if r.returncode:
                return "failed", f"failed: the branch does not merge with origin/{base}\n\n{r.stdout}{r.stderr}"
            failed, _, report = self.run_checks(tmp, base, blocking_only=True)
        finally:
            sh(["git", "worktree", "remove", "--force", str(tmp)], self.root)
        if failed:
            return "failed", f"failed: blocking checks on the branch merged with origin/{base}\n\n{report}"
        return "passed", report

    def read_ci(self, url):
        """(`passed` | `pending` | `failed`, text): wait until CI has registered checks for the PR head
        ("no checks reported" right after `gh pr create` is pending), watch them, then read them once more.
        A failure's text has the `gh run view --log-failed` tail of each failed run."""
        # shortcut: waits at most CI_WAIT seconds, then reports CI as pending; raise it if CI gets slower.
        deadline = time.monotonic() + CI_WAIT

        def checks(*flags):
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired("gh pr checks", CI_WAIT)
            return subprocess.run([gh(), "pr", "checks", url, *flags], cwd=self.wt, capture_output=True,
                                  text=True, timeout=left)
        try:
            while NO_CHECKS in (r := checks()).stdout + r.stderr:
                time.sleep(max(0, min(CI_POLL, deadline - time.monotonic())))
            checks("--watch")
            r = checks()  # the --watch stream can still hold the first pending snapshot
        except subprocess.TimeoutExpired:
            return "pending", f"pending after {CI_WAIT}s"
        if r.returncode == 0:
            return "passed", "passed\n\n" + r.stdout
        if r.returncode == 8:  # gh: checks still pending
            return "pending", "pending\n\n" + r.stdout
        text = [f"failed (exit {r.returncode}) at {url}", "", "~~~~", (r.stdout + r.stderr).strip(), "~~~~"]
        runs = dict.fromkeys(re.findall(r"/actions/runs/(\d+)", "\n".join(
            ln for ln in r.stdout.splitlines() if re.search(r"\sfail\s", ln))))
        for run in runs:
            # shortcut: no timeout on this read; add one if `gh run view` hangs.
            log = subprocess.run([gh(), "run", "view", run, "--log-failed"], cwd=self.wt, capture_output=True,
                                 text=True).stdout
            text += ["", f"## Failed log of run {run} (last 80 lines)", "~~~~", *log.splitlines()[-80:], "~~~~"]
        return "failed", "\n".join(text) + "\n"

    def retro(self, t):
        """One entry per run: the run's numbers and the retro role's lessons (after its retries; without
        a handoff the entry has the numbers only), committed under `<dir>/<repo>/` on the knowledge branch."""
        k, base = self.config["knowledge"], self.issue()[1]
        knowledge_branch(self.config, k["branch"], base)
        spec = self.m["actions"]["retro"]
        for _ in range(1 + spec.get("retry", 0)):
            result, h = self.agent(t, "retro", spec)
            if result == "done":
                break
        else:
            h = {}
        repo = primary(self.config)["name"]
        knowledge_dir(k)
        run_id = t["vars"]["run_id"] or self.run_id
        rel = f"{k['dir']}/{repo}/retro-{self.nn}-{re.sub(r'[^A-Za-z0-9.-]+', '-', run_id)}.md"
        text = "\n".join([f"# Retro: #{self.nn} {self.issue()[0]['title']}", "", f"run: {run_id}", "",
                          self.run_numbers(t), "", "## Manual steps", "", (h.get("manual_steps") or "unknown: the retro role gave no handoff").strip(),
                          "", "## Lessons", "", (h.get("lessons") or "unknown: the retro role gave no handoff").strip()]) + "\n"
        msg = f"{self.config['commit_prefix'].format(ticket=self.nn)}Retro entry for run {run_id}"
        self.push_file(k["branch"], base, rel, text, msg)
        return "done", {"entry": rel, "branch": k["branch"]}

    def run_numbers(self, t):
        """Cost if recorded, wall time (the run dir's oldest file to the last CI read, i.e. to approved), fix rounds, blocking findings and
        reviewer agreement from every review handoff, and the approver's dry-run verdict, as markdown lines."""
        files = [p for p in self.run_dir.iterdir() if p.is_file()]
        ends = [p.stat().st_mtime for p in self.run_dir.glob("ci-*.md")]
        wall = int((max(ends) if ends else time.time()) - min(p.stat().st_mtime for p in files))
        log = self.run_dir / "events.log"
        costs = [r["cost"] for r in map(json.loads, log.read_text().splitlines()) if r.get("event") == "usage"] \
            if log.exists() else []
        rounds, ids = {}, []
        for p in sorted(self.run_dir.glob("review_*.handoff.json")):
            action, _, n = p.name.removesuffix(".handoff.json").partition("-")
            h = json.loads(p.read_text())
            rounds.setdefault(int(n or 0), {})[action] = h.get("verdict")
            ids += [f"{action}-{n or 0}:{i}" for i in dict.fromkeys(
                x["id"] for x in h.get("findings", []) if x["severity"] == "blocking")]  # per reviewer and round
        shared = [set(v.values()) for v in rounds.values() if len(v) > 1]
        a = t["out"].get("approve") or {}
        return "\n".join([
            f"- cost: {'$%.3f' % sum(costs) if costs else 'unknown'}",
            f"- wall time: {wall // 60}m {wall % 60}s (start to approved)",
            f"- fix rounds: {t['vars']['fix_round']}",
            f"- blocking findings: {len(ids)}{' (' + ', '.join(ids) + ')' if ids else ''}",
            f"- reviewer agreement: {sum(len(s) == 1 for s in shared)}/{len(shared)} rounds with several reviewers"
            if shared else "- reviewer agreement: n/a (one reviewer per round)",
            f"- approver (dry-run): {a.get('verdict', 'unknown')}{'; ' + '; '.join(a['reasons']) if a.get('reasons') else ''}",
            f"- ci: {(t['out'].get('ci') or {}).get('result', 'unknown')}"])

    def push_file(self, branch, base, rel, text, msg):
        """Commit `rel` on `branch` (from origin/<base> when it does not exist yet) in a temporary detached
        worktree and push it; after a rejected push (another run pushed first) rebase once and push again."""
        tmp = self.wt.with_name(f"{self.nn}-retro")
        if tmp.exists():
            sh(["git", "worktree", "remove", "--force", str(tmp)], self.root)
        start = branch if sh(["git", "ls-remote", "--heads", "origin", branch], self.root).strip() else base
        sh(["git", "fetch", "origin", start], self.root)
        sh(["git", "worktree", "add", "--detach", str(tmp), f"origin/{start}"], self.root)
        try:
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_text(text)
            sh(["git", "add", rel], tmp)
            sh(["git", "commit", "-qm", msg], tmp)
            try:
                sh(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], tmp)
            except RuntimeError:
                sh(["git", "pull", "--rebase", "origin", branch], tmp)
                sh(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], tmp)
        finally:
            sh(["git", "worktree", "remove", "--force", str(tmp)], self.root)

    def summarize(self, t, trace):
        """After `ci` moved the ticket to `approved`: the router summary (with the action's retries),
        then `finish`. The summary is best effort: its result never changes the stage."""
        self.trace = trace
        self.save(t)
        self.finish(t, self.attempt(t, "summary"))

    def attempt(self, t, action):
        """Run `action` outside the machine, with its retries: its handoff with `result`."""
        for _ in range(1 + self.m["actions"][action].get("retry", 0)):
            result, h = self.run(t, action)
            if result == "done":
                break
        return {"result": result, **h}

    def hooks(self, t):
        """The `on_enter` actions of the state the run stopped in, once and best effort: post an agent
        action's `comment` on the ticket. Clear them and return the ticket."""
        self.save(t)
        split = (t["out"].get("intake") or {}).get("split") or "none"
        if t["hooks"] and t["state"] == "blocked" and split != "none":
            try:
                self.tracker.comment(self.nn, "\n".join([
                    "[factory] Intake stopped this ticket: it is above the size limit or needs a split."
                    " Proposed slices (create them as linked sub-issues and retitle this ticket to the first one):",
                    "", split, "", self.trailer(t)]))
            except (Exception, SystemExit) as e:  # noqa: BLE001 best effort, like the other hooks
                print(f"ticket {self.nn}: split comment failed: {e}", file=sys.stderr)
        for action in t["hooks"]:
            try:
                h = self.attempt(t, action)
                if h["result"] == "done" and h.get("comment"):
                    self.tracker.comment(self.nn, h["comment"])
            except (Exception, SystemExit) as e:  # noqa: BLE001 best effort: the stage is already written
                print(f"ticket {self.nn}: {action} failed: {e}", file=sys.stderr)
        return {**t, "hooks": []}

    def notify(self, t):
        """One line per `channels` entry; `local`: stdout and `notify.log` in the run dir."""
        line = f"[factory] ticket {self.nn}: {t['state']} ({self.trailer(t)})"
        for ch in self.config.get("channels") or []:
            if ch != "local":
                raise ValueError(f"no notifier for channel {ch!r}")
            print(line)
            with (self.run_dir / "notify.log").open("a") as f:
                f.write(line + "\n")
        return "done", {}

    def finish(self, t, s):
        """After the summary: post its comment on the ticket, switch the PR body to `Closes #N` when the
        summary says `complete: yes`, and remove the worktree once CI passed (the run dir stays)."""
        if s.get("comment"):
            self.tracker.comment(self.nn, s["comment"])
        if s.get("complete") in ("yes", "no"):  # the PR body starts with `Closes` unless the intake split
            i, base = self.issue()
            for url in t["vars"]["pr_urls"]:
                sh([gh(), "pr", "edit", url, "--body", self.pr_body(t, i, base, closes=s["complete"] == "yes")],
                   self.root)
        if self.wt.is_dir() and (t["out"].get("ci") or {}).get("result") == "passed":
            sh(["git", "worktree", "remove", str(self.wt)], self.root)  # no --force: keep uncommitted work

    def trailer(self, t):
        return f"Factory-Run: {t['vars']['run_id'] or self.run_id}"

    def commits(self, t, base):
        """The branch's commits since origin/<base>, one `sha subject` per line (the developer's handoff
        lists only its own round)."""
        return sh(["git", "log", "--reverse", "--format=%h %s", f"origin/{base}..{t['vars']['branch']}"],
                  self.root).strip()

    @staticmethod
    def acceptance(i):
        """The ticket's acceptance checks: the bullets under its `Acceptance checks` heading or line."""
        m = re.search(r"acceptance checks?:?\**[ \t]*\n(.*?)(?:\n\s*\n(?=\S)|\Z)", i.get("body") or "", re.I | re.S)
        items = [ln.strip() for ln in (m.group(1) if m else "").splitlines() if ln.strip().startswith(("-", "*"))]
        return items or ["See the ticket."]

    def pr_body(self, t, i, base, closes=None):
        out, v = t["out"], t["vars"]
        if closes is None:  # the ticket is not split further
            closes = ((out.get("intake") or {}).get("split") or "none") == "none"
        lines = [f"[factory] {i['title']}", "", "## What changed", self.commits(t, base) or "See the commits.", "",
                 "## Checks"]
        lines += [f"- {c['name']} ({c['mode']}): {c['status']}" for c in (out.get("checks") or {}).get("checks", [])]
        lines += ["", "## Acceptance", *self.acceptance(i)]
        lines += ["", "## Review"]
        for a in (a for a in REVIEWERS if a in out):
            r = out[a]
            lines.append(f"- {a}, {r.get('model')}: {r.get('verdict')}, risk {r.get('risk')}, at {r.get('reviewed_sha')}")
            if r.get("fallback_from"):
                lines.append(f"  Review: {a} fell back from {r['fallback_from']} to {r.get('model')}")
        old = (out.get("intake") or {}).get("risk") or "low"
        if v["risk"] != old:
            lines.append(f"- Risk: raised from {old} to {v['risk']} by the review")
        lines.append(f"- Fix rounds: {v['fix_round']}")
        if out.get("approve"):
            lines += ["", f"## Approver (dry-run): {out['approve']['verdict']}", "", *approval_lines(out["approve"])]
        # The link stays last before the trailer: the driver swaps it for `Closes` after the summary.
        lines += ["", f"{'Closes' if closes else 'Refs'} #{self.nn}", "", self.trailer(t)]
        return "\n".join(lines) + "\n"

    # ---- Agent actions ----

    def role_input(self, t, action, spec):
        v, out = t["vars"], t["out"]
        base = self.issue()[1]
        lines = [f"mode: {spec.get('mode')}", f"ticket: {self.nn}", f"base: {base}",
                 f"run_id: {v['run_id'] or self.run_id}"]
        if v["branch"]:
            lines.append(f"branch: {v['branch']}")
        if spec["role"] == "reviewer" and v["reviewed_sha"]:
            lines.append(f"reviewed_sha: {v['reviewed_sha']} (re-review: read the changes since it)")
        blocks = {}
        # A fix round's cause: failed checks, else blocking reviews, else failed CI (an older CI failure
        # stays in out.ci, but a review that blocks after it is newer; publish needs every review approved).
        reviews = {a: out[a] for a in REVIEWERS if (out.get(a) or {}).get("verdict") == "changes-requested"}
        if action == "fix" and (out.get("checks") or {}).get("result") == "failed":
            blocks["checks"] = Path(out["checks"]["report"]).read_text()
        elif action == "fix" and reviews:
            blocks = reviews
        elif action == "fix" and (out.get("ci") or {}).get("result") == "failed":
            blocks["checks"] = Path(out["ci"]["report"]).read_text()  # CI or the merge with the base failed
        elif action == "diagnose":
            lines.append(f"stage: {t['state']}")
            blocks = {a: o for a, o in out.items() if a != "diagnose"}
        elif action == "recheck":  # the new comments, oldest first, are untrusted data
            lines.append(f"stage: {t['state']}")
            blocks = {"comments": "\n\n".join(f"#{c['id']} by {c['author'] or 'unknown'}:\n{c['body']}" for c in self.comments),
                      **{a: o for a, o in out.items() if a in ("intake", "diagnose")}}
        elif action == "document":  # the reviewers' handoffs say what has docs impact
            blocks = {a: out[a] for a in REVIEWERS if a in out}
        elif action == "retro":
            blocks = {a: out[a] for a in ("implement", "fix", "checks", *REVIEWERS, "document", "approve", "ci")
                      if a in out}
            blocks["numbers"] = self.run_numbers(t)
        elif action == "summary":
            lines += [f"stage: {t['state']} (the summary runs after the stage change)",
                      f"ci: {(out.get('ci') or {}).get('ci', 'unknown')}"]
            blocks = {a: out[a] for a in ("implement", "fix", "checks", *REVIEWERS, "document", "approve", "publish", "ci") if a in out}
            blocks["commits"] = self.commits(t, base)
            blocks["events"] = "\n".join(map(event_line, self.trace))
            if cost_table(self.run_dir):
                blocks["cost"] = cost_table(self.run_dir)
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
            h["blocking_ids"] = blocking_ids(h)
        if spec["role"] not in ("developer", "doc-writer"):
            return "done", h
        return (h["status"] if h.get("status") in ("done", "blocked") else "error"), h


def blocking_ids(handoff):
    """`<id> <file>` of each blocking finding: what the "same findings twice" guard compares."""
    return [f"{x['id']} {x['file']}" for x in handoff.get("findings", []) if x["severity"] == "blocking"]


def primary(config):
    return next(r for r in config["repositories"] if r.get("role") == "primary")


def knowledge_branch(config, name, base):
    """`name`, or ValueError when it is unsafe or a default branch (knowledge never goes there directly)."""
    if name in {r["default_branch"] for r in config["repositories"]} | {base} or not SAFE_BRANCH.fullmatch(name) \
            or ".." in name:
        raise ValueError(f"knowledge branch {name!r} is unsafe or a default branch")
    return name


def knowledge_dir(k):
    if ".." in k["dir"].split("/") or k["dir"].startswith("/") or not k["dir"].strip("/"):
        raise ValueError(f"knowledge dir {k['dir']!r} must stay inside the repository")
    return k["dir"].strip("/")


DISTILLED = "distilled.txt"  # under the knowledge dir on the default branch: the entries already merged


def distill(root, run_id=None):
    """Open one PR to the default branch that merges the new knowledge-inbox entries (the retro entries
    on `knowledge.branch` not listed in `<dir>/distilled.txt` on the default branch) into `<dir>/`: the
    distill role edits a worktree of `<distill_branch>-<date>`, the driver rejects edits outside `<dir>/`,
    lists the entries in `distilled.txt`, commits, pushes and runs `gh pr create`. Return the PR url, or
    None when there is no new entry or a distill PR is still open. The default branch is never written."""
    root = Path(root).resolve()
    config, m = load(root)
    k, base = config["knowledge"], primary(config)["default_branch"]
    inbox, prefix, kdir = knowledge_branch(config, k["branch"], base), knowledge_branch(
        config, k["distill_branch"], base), knowledge_dir(k)
    if not sh(["git", "ls-remote", "--heads", "origin", inbox], root).strip():
        return None
    sh(["git", "fetch", "origin", base, inbox], root)
    seen = subprocess.run(["git", "show", f"origin/{base}:{kdir}/{DISTILLED}"], cwd=root, capture_output=True,
                          text=True).stdout.split()
    new = [p for p in sh(["git", "ls-tree", "-r", "--name-only", f"origin/{inbox}", "--", f"{kdir}/"], root).split()
           if re.fullmatch(rf"{re.escape(kdir)}/[^/]+/retro-[^/]+\.md", p) and p not in seen]
    prs = json.loads(sh([gh(), "pr", "list", "--base", base, "--state", "open", "--json", "url,headRefName"], root)
                     or "[]")
    if not new or any(p["headRefName"].startswith(f"{prefix}-") for p in prs):
        return None
    run_id = run_id or f"local:{time.strftime('%Y%m%dT%H%M%S')}-distill"
    branch, local = f"{prefix}-{time.strftime('%Y%m%d')}", config["targets"]["local"]
    run_dir, wt = root / local["run_dir"] / "distill", root / local["worktree_dir"] / "distill"
    run_dir.mkdir(parents=True, exist_ok=True)
    lines = ["mode: merge", f"run_id: {run_id}", f"knowledge_dir: {kdir}",
             f"repositories: {', '.join(r['name'] for r in config['repositories'])}"]
    for p in new:
        lines += ["", f"## {p}", "", "~~~~", sh(["git", "show", f"origin/{inbox}:{p}"], root).rstrip(), "~~~~"]
    (run_dir / "distill.in.md").write_text("\n".join(lines) + "\n")
    if wt.exists():
        sh(["git", "worktree", "remove", "--force", str(wt)], root)
    sh(["git", "worktree", "add", "-B", branch, str(wt), f"origin/{base}"], root)
    try:
        for _ in range(1 + m["actions"]["distill"].get("retry", 0)):
            try:
                if not run_role.run(root, "distill", "distill"):
                    break
            except SystemExit as e:  # model access or a missing worktree
                raise RuntimeError(str(e)) from e
        else:
            raise RuntimeError("the distill role gave no handoff")
        h = json.loads((run_dir / "distill.handoff.json").read_text())
        if h.get("status") != "done":
            raise RuntimeError(f"distill {h.get('status')}: {h.get('blocked_reason')}")
        outside = [ln[3:] for ln in sh(["git", "status", "--porcelain", "-uall"], wt).splitlines()
                   if not ln[3:].startswith(f"{kdir}/")]
        if outside:
            raise ValueError(f"distill changed files outside {kdir}/: {', '.join(outside)}")
        (wt / kdir).mkdir(parents=True, exist_ok=True)
        (wt / kdir / DISTILLED).write_text("\n".join(sorted({*seen, *new})) + "\n")
        sh(["git", "add", "-A", "--", kdir], wt)
        sh(["git", "commit", "-qm", f"Distill {len(new)} knowledge-inbox entries"], wt)
        sh(["git", "push", "-u", "origin", branch], wt)
        body = "\n".join(["[factory] Distill: the knowledge-inbox entries merged into the knowledge base.", "",
                          "## Entries", *[f"- {p}" for p in new], "", "## Lessons", "",
                          (h.get("summary") or "none").strip(), "", f"Factory-Run: {run_id}"]) + "\n"
        return sh([gh(), "pr", "create", "--head", branch, "--base", base, "--title",
                   f"Distill {len(new)} knowledge-inbox entries", "--body", body], wt).strip().splitlines()[-1]
    finally:
        sh(["git", "worktree", "remove", "--force", str(wt)], root)


def distill_due(root, config):
    """True at most once per `knowledge.distill_every_days` (unset: never); records the attempt."""
    days = (config.get("knowledge") or {}).get("distill_every_days")
    f = Path(root) / config["targets"]["local"]["run_dir"] / "distill" / "last.json"
    if not days or (f.exists() and time.time() - json.loads(f.read_text()) < days * 86400):
        return False
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(time.time()))
    return True


def run_ticket(root, nn, tracker, executor, run_id=None, resume=False, results=()):
    """Deliver ticket `nn` until the machine waits on a human or external actor. Return (ticket, trace)."""
    d = Delivery(root, nn, tracker, run_id or f"local:{time.strftime('%Y%m%dT%H%M%S')}-{nn}")
    if any("pr.merged" in r for r in results if isinstance(r, dict)):
        d.log_merge()
    t, trace = drive(d.m, tracker, nn, executor, results, execute=d, resume=resume, on_step=d.on_step)
    d.save(t)  # sm.json shows the final stage
    if trace and trace[-1]["event"] in ("ci.passed", "ci.pending", "merge.moved", "merge.failed"):  # only the run that reached approved
        d.summarize(t, trace)
    if t["hooks"]:
        t = d.hooks(t)
        vars_ = {**tracker.read_vars(nn), **sm.persisted(t)}  # keeps `_resume`
        if d.m["states"][t["state"]].get("resume_on"):  # comments up to now (diagnose's too) are not new
            vars_["checked_comment"] = max((c["id"] for c in tracker.comments(nn)), default=0)
        tracker.write_vars(nn, vars_)
        d.save(t)
    return t, trace


# ---- Event detectors: factory.yaml `events` maps an external event to one ----


def pr_merged(root, nn, vars_):
    """Every PR in `pr_urls` is merged (none recorded: not detected)."""
    prs = [json.loads(sh([gh(), "pr", "view", u, "--json", "state,mergedAt"], root)) for u in vars_.get("pr_urls") or []]
    return {"open_pr_urls": []} if prs and all(p.get("state") == "MERGED" for p in prs) else None


DETECTORS = {"github-pr-merged": pr_merged}


def detect(root, nn, vars_, spec):
    """The event's payload when its detector reports it, else None. `{run: <bash>}`: exit 0, stdout an
    optional JSON payload; a string: a built-in detector."""
    if isinstance(spec, dict) and isinstance(spec.get("run"), str):
        r = subprocess.run(["bash", "-c", spec["run"]], cwd=root, capture_output=True, text=True,
                           env={**os.environ, "FACTORY_TICKET": str(nn)})
        return None if r.returncode else (json.loads(r.stdout) if r.stdout.strip() else {})
    if spec not in DETECTORS:
        raise ValueError(f"unknown event detector {spec!r}; use {', '.join(DETECTORS)} or {{run: <bash>}}")
    return DETECTORS[spec](root, nn, vars_)


COMMENTS = "comment.added"  # fired for a state with `resume_on: comment`; no factory.yaml detector


TRUSTED = ("OWNER", "MEMBER", "COLLABORATOR")  # GitHub author_association of comments that may resume


def comment_added(root, nn, tracker, config, m, state, vars_):
    """Recheck the comments added since the last checked one (not `[factory]`, not a bot login) in one
    cheap router call; record the newest id first, so each comment is checked once. None: nothing new."""
    bots = (config["tracker"].get("bot_logins") or [])
    if (vars_.get("resumes") or 0) >= sm.lookup(config, "limits.resume_cap"):  # no resume possible: skip the reads and the call
        return None
    every = tracker.comments(nn)
    if "checked_comment" not in vars_:  # a ticket that waited before this existed: nothing is new yet
        tracker.write_vars(nn, {**vars_, "checked_comment": max((c["id"] for c in every), default=0)})
        return None
    new = [c for c in every if c["id"] > (vars_.get("checked_comment") or 0)
           and not c["body"].lstrip().startswith("[factory]") and c["author"] not in bots
           and c.get("association") in TRUSTED]  # outsiders cannot resume a ticket (and spend runs)
    if not new:
        return None
    d = Delivery(root, nn, tracker, f"local:{time.strftime('%Y%m%dT%H%M%S')}-{nn}")
    d.comments = new
    t = sm.new_ticket(m, {"state": state, "vars": vars_})
    d.save(t)
    h = d.attempt(t, "recheck")
    if h["result"] != "done":  # not judged: leave the comments unchecked, the next sweep retries
        return None
    tracker.write_vars(nn, {**vars_, "checked_comment": max(c["id"] for c in new)})
    return {"actionable": "yes" if str(h.get("actionable")).lower() == "yes" else "no",
            "reason": h.get("reason", ""), "comments": new}


def load(root):
    root = Path(root)
    return (yaml.safe_load((root / "factory.yaml").read_text()),
            sm.load_machine(root / "workflows/delivery.v1.sm.yaml", root / "factory.yaml"))


def watched(m, config, state):
    """The declared external events on `state`'s transitions that have a detector, in file order."""
    det = config.get("events") or {}
    found = list(dict.fromkeys(tr["on"] for tr in m["transitions"]
                               if state in tr["from"] and tr["on"] in m["events"] and tr["on"] in det))
    return found + ([COMMENTS] if (m["states"].get(state) or {}).get("resume_on") == "comment" else [])


def poll(root, nn, tracker, executor):
    """Fire the first watched event of the ticket's state that its detector reports: (ticket, trace).
    None, without any write, when none is detected."""
    config, m = load(root)
    state = tracker.get_state(nn)
    events = watched(m, config, state)
    vars_ = tracker.read_vars(nn) if events else {}
    for ev in events:
        payload = (comment_added(root, nn, tracker, config, m, state, vars_) if ev == COMMENTS
                   else detect(root, nn, vars_, config["events"][ev]))
        if payload is not None:
            return run_ticket(root, nn, tracker, executor, results=[{ev: payload}])
    return None


def runs_left(ledger, per_day):
    """Runs still allowed in the last 24 hours (`ledger`: a JSON list of start times)."""
    if per_day is None:
        return float("inf")
    now = time.time()
    starts = [x for x in (json.loads(ledger.read_text()) if ledger.exists() else []) if x > now - 86400]
    return max(0, per_day - len(starts))


def record_runs(ledger, n):
    now = time.time()
    starts = [x for x in (json.loads(ledger.read_text()) if ledger.exists() else []) if x > now - 86400]
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps(starts + [now] * n))


ERRORS = (ClaimError, ResumeError, RuntimeError, ValueError, KeyError, OSError, sm.SMError)


def loop(root, tracker, executor, once=False):
    """Move every ticket the machine can move, one line each, until a cycle changes nothing
    (`once`: one cycle). The loop itself never sets a stage. Return 2 if any ticket errored, else 0."""
    root = Path(root)
    cycle = time.strftime("%Y%m%dT%H%M%S")  # one id per loop call, on every line it prints

    def say(msg, err=False):
        print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} cycle={cycle} {msg}",
              file=sys.stderr if err else sys.stdout)
    config, m = load(root)
    limits = config.get("limits") or {}
    width = max(1, limits.get("concurrent_deliveries") or 1)
    ledger = root / config["targets"]["local"]["run_dir"] / "loop-runs.json"
    ran, code = set(), 0
    if distill_due(root, config):  # the weekly distill rides on the loop's schedule
        try:
            if url := distill(root):
                say(f"distill: opened {url}")
        except ERRORS as e:
            say(f"distill: error: {e}", err=True)
            code = 2
    while True:
        moved, agent = False, []
        for nn in tickets(m, config, tracker):
            try:
                state = tracker.get_state(nn)
                if (m["states"].get(state) or {}).get("actor") == "agent":
                    agent += [] if nn in ran or nn in agent else [nn]
                    continue
                done = poll(root, nn, tracker, executor)
            except ERRORS as e:
                say(f"ticket {nn}: error: {e}", err=True)
                code = 2
                continue
            if done is not None:
                say(f"ticket {nn}: final: {done[0]['state']}")
                moved |= done[0]["state"] != state
        batch = agent[:int(min(width, runs_left(ledger, limits.get("runs_per_day"))))]
        if batch:
            record_runs(ledger, len(batch))
            ran.update(batch)

        def go(nn):
            try:
                return run_ticket(root, nn, tracker, executor)[0]["state"], None
            except ERRORS as e:
                return None, e
        with ThreadPoolExecutor(max(1, len(batch))) as ex:
            for nn, (state, err) in zip(batch, ex.map(go, batch)):
                if isinstance(err, ClaimError):
                    say(f"ticket {nn}: skipped: {err}")
                elif err:
                    say(f"ticket {nn}: error: {err}", err=True)
                    code = 2
                else:
                    say(f"ticket {nn}: final: {state}")
        if once or not (moved or batch):
            return code


def tickets(m, config, tracker):
    """Tickets (open and closed) in a state the loop can move: an agent state (the initial state: the
    tracker's frontier) or a state with a watched event."""
    out = []
    for state, spec in m["states"].items():
        if spec["actor"] == "agent" and state == m["initial"]:
            found = tracker.frontier()
        elif spec["actor"] == "agent" or watched(m, config, state):
            found = tracker.in_stage(state, closed=True)
        else:
            continue
        out += [str(n) for n in found if str(n) not in out]
    return out


def report_run(t, trace, run_dir):
    for rep in trace:
        print(event_line(rep))
    table = cost_table(run_dir)
    if table:
        print(table)
    print(f"final: {t['state']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="drive.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="deliver a ticket until it waits on a human or external actor")
    r.add_argument("ticket")
    r.add_argument("--resume", action="store_true", help="restart a failed ticket at the step that failed")
    y = sub.add_parser("loop", help="run every ticket the state machine can move until nothing moves")
    y.add_argument("--once", action="store_true", help="one cycle (was `sync`)")
    for sp in (r, y):
        sp.add_argument("--tracker", default="github", help="local:<effort dir> or github (default)")
        sp.add_argument("--executor", help="claim name (default: the gh login, or the OS user for local)")
    sub.add_parser("distill", help="open one PR that merges the knowledge-inbox entries into the knowledge base")
    rp = sub.add_parser("report", help="per ticket: state, wall, agent and wait time, cost, fix rounds, retries, fallbacks")
    rp.add_argument("--since", help="only tickets whose log starts on or after this date (ISO, e.g. 2026-10-29)")
    rp.add_argument("--ticket", help="one ticket")
    rp.add_argument("--json", action="store_true", help="JSON instead of the table")
    b = sub.add_parser("branch", help="print the ticket's branch name")
    b.add_argument("ticket")
    b.add_argument("ticket_json")
    args = p.parse_args(argv)
    if args.cmd == "distill":
        print(distill(sm.ROOT) or "distill: no new entry, or a distill PR is open")
        return 0
    if args.cmd == "report":
        rows = runlog.collect(sm.ROOT / yaml.safe_load((sm.ROOT / "factory.yaml").read_text())["targets"]["local"]["run_dir"],
                              args.since, args.ticket)
        print(json.dumps(rows, indent=2) if args.json else runlog.table(rows))
        return 0
    if args.cmd not in ("loop", "report") and not args.ticket.isdigit():
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
    run_dir = sm.ROOT / yaml.safe_load((sm.ROOT / "factory.yaml").read_text())["targets"]["local"]["run_dir"]
    if args.cmd == "loop":
        return loop(sm.ROOT, tracker, executor, once=args.once)
    try:
        config, m = load(sm.ROOT)
        events = [] if args.resume else watched(m, config, tracker.get_state(args.ticket))
        if events:
            done = poll(sm.ROOT, args.ticket, tracker, executor)
            if done is None:
                print(f"waiting for {', '.join(events)}")
                return 0
            t, trace = done
        else:
            t, trace = run_ticket(sm.ROOT, args.ticket, tracker, executor, resume=args.resume)
    except (ClaimError, ResumeError) as e:  # a claim held elsewhere, or no resume point
        print(f"drive.py: error: {e}", file=sys.stderr)
        return 2
    report_run(t, trace, run_dir / args.ticket)
    return 0 if t["state"] in ("approved", "done") else 1


if __name__ == "__main__":
    sys.exit(main())

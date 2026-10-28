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
the failing job log (`ci-<round>.md`). Passed or pending CI runs the retro: the retro role's lessons and
the run's numbers go into one entry under `knowledge/<repo>/`, pushed to factory.yaml `knowledge.branch`
(never a default branch), and the stage moves to `approved`. The reviewers of a fork run in parallel. Once
the retro moves the ticket to `approved`, the driver runs the router summary (input: final stage, CI result, commits, event
log), posts its comment on the ticket, edits the PR body from `Refs #N` to `Closes #N` when the summary
says `complete: yes`, and removes the worktree once CI passed. `pi` and `gh` can be replaced with
$FACTORY_AGENT and $FACTORY_GH.

CLI: uv run factory/drive.py run <ticket> [--tracker local:<effort dir>|github] [--executor NAME] [--resume]
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
from pathlib import Path

import yaml

import approver
import preflight
import run_role
import sm
from local_tracker import ClaimError, LocalTracker

class ResumeError(Exception):
    """--resume on a ticket that has no resume point."""


STOPWORDS = {"a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "with", "from", "by"}


def drive(m, tracker, nn, executor, results=(), execute=None, resume=False):
    """Claim ticket `nn`, resume from the stored state, vars and run state (`sm.persisted`),
    apply each result with `sm.step`, write them back after every step, and release the claim
    once the ticket waits on a human or external actor, or when reading the ticket fails.
    A step that ends in `failed` also stores `_resume`: the stage and the actions that were running.
    `resume` restarts a `failed` ticket from it, with fresh retries.
    `results` items use sm.parse_event's forms; once they run out, `execute(ticket)` gives the
    next one (None: stop). Return (ticket, trace)."""
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
        self.m = sm.load_machine(self.root / "workflows/delivery.sm.yaml", self.root / "factory.yaml")
        local = self.config["targets"]["local"]
        self.run_dir = self.root / local["run_dir"] / str(nn)
        self.wt = self.root / local["worktree_dir"] / str(nn)
        self.done = []  # (action, (result, payload)) of fork branches that ran together, not yet reported
        self.trace = []

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
        try:
            return getattr(self, action)(t)
        except (RuntimeError, OSError, ValueError) as e:
            # `ci` failed means a fix round, so its unexpected errors map to `error` (stage failed).
            return ("error" if action == "ci" else "failed"), {"error": str(e)}

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
        failed, rows, report = self.run_checks(self.wt, self.issue()[1])
        path = self.run_dir / f"checks-{t['vars']['fix_round']}.md"
        path.write_text(report)
        return ("failed" if failed else "passed"), {"checks": rows, "report": str(path)}

    def run_checks(self, cwd, base, blocking_only=False):
        """Run the pre-review checks in `cwd`: (a blocking one failed, rows, markdown report)."""
        rows, report, failed = [], [], False
        for c in self.config.get("checks") or []:
            if c.get("stage", "pre-review") != "pre-review" or (blocking_only and c.get("mode") != "blocking"):
                continue
            if c.get("requires") and not shutil.which(c["requires"]):
                status, text = "skipped", f"{c['requires']} is not installed"
            else:
                r = subprocess.run(["bash", "-c", c["run"]], cwd=cwd, capture_output=True, text=True,
                                   env={**os.environ, "FACTORY_BASE": base})
                status, text = ("pass" if r.returncode == 0 else "fail"), r.stdout + r.stderr
            failed |= status == "fail" and c.get("mode") == "blocking"
            rows.append({"name": c["name"], "mode": c.get("mode"), "status": status})
            tail = "\n".join(text.rstrip().splitlines()[-15:])
            report.append(f"## {c['name']} ({c.get('mode')}): {status}\n~~~~\n{tail}\n~~~~\n")
        return failed, rows, "\n".join(report)

    def approve(self, t):
        """The approver (dry-run): record its verdict and reasons on the ticket; publish adds them to the PR."""
        i, base = self.issue()
        out = t["out"]
        ci = "passed" if (out.get("checks") or {}).get("result") == "passed" else "unknown"
        a = approver.decide(self.root, self.config, self.wt, base, {r: out[r] for r in REVIEWERS if r in out}, ci,
                            t["vars"]["risk"], f"{i['title']}\n\n{i.get('body') or ''}")
        path = self.run_dir / f"approve-{t['vars']['fix_round']}.json"
        path.write_text(json.dumps(a["summary"], indent=2))
        self.tracker.comment(self.nn, "\n".join([f"[factory] Approver (dry-run): {a['verdict']}", "",
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
        nits = [f"### {a}\n\n{out[a]['nits'].strip()}" for a in REVIEWERS
                if (out.get(a) or {}).get("nits", "").strip() not in ("", "none")]
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
        return status, {"ci": text.splitlines()[0] if text else status, "report": str(path)}

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
        defaults = {r["default_branch"] for r in self.config["repositories"]} | {base}
        if k["branch"] in defaults or not SAFE_BRANCH.fullmatch(k["branch"]):
            raise ValueError(f"knowledge branch {k['branch']!r} is unsafe or a default branch")
        spec = self.m["actions"]["retro"]
        for _ in range(1 + spec.get("retry", 0)):
            result, h = self.agent(t, "retro", spec)
            if result == "done":
                break
        else:
            h = {}
        repo = next(r for r in self.config["repositories"] if r.get("role") == "primary")["name"]
        run_id = t["vars"]["run_id"] or self.run_id
        rel = f"{k['dir']}/{repo}/retro-{self.nn}-{re.sub(r'[^A-Za-z0-9.-]+', '-', run_id)}.md"
        text = "\n".join([f"# Retro: #{self.nn} {self.issue()[0]['title']}", "", f"run: {run_id}", "",
                          self.run_numbers(t), "", "## Manual steps", "", (h.get("manual_steps") or "unknown").strip(),
                          "", "## Lessons", "", (h.get("lessons") or "unknown").strip()]) + "\n"
        msg = f"{self.config['commit_prefix'].format(ticket=self.nn)}Retro entry for run {run_id}"
        self.push_file(k["branch"], base, rel, text, msg)
        return "done", {"entry": rel, "branch": k["branch"]}

    def run_numbers(self, t):
        """Cost if recorded, wall time (since the run dir's oldest file), fix rounds, blocking findings and
        reviewer agreement from every review handoff, and the approver's dry-run verdict, as markdown lines."""
        files = [p for p in self.run_dir.iterdir() if p.is_file()]
        wall = int(time.time() - min(p.stat().st_mtime for p in files))
        log = self.run_dir / "events.log"
        costs = [r["cost"] for r in map(json.loads, log.read_text().splitlines()) if r.get("event") == "usage"] \
            if log.exists() else []
        rounds, ids = {}, []
        for p in sorted(self.run_dir.glob("review_*.handoff.json")):
            action, _, n = p.name.removesuffix(".handoff.json").partition("-")
            h = json.loads(p.read_text())
            rounds.setdefault(int(n or 0), {})[action] = h.get("verdict")
            ids += [i for i in re.findall(r"^\s*-?\s*id:\s*(\S+)", h.get("blocking") or "", re.M) if i not in ids]
        shared = [set(v.values()) for v in rounds.values() if len(v) > 1]
        a = t["out"].get("approve") or {}
        return "\n".join([
            f"- cost: {'$%.3f' % sum(costs) if costs else 'unknown'}",
            f"- wall time: {wall // 60}m {wall % 60}s",
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
        spec = self.m["actions"]["summary"]
        for _ in range(1 + spec.get("retry", 0)):
            result, h = self.agent(t, "summary", spec)
            if result == "done":
                break
        self.finish(t, {"result": result, **h})

    def finish(self, t, s):
        """After the summary: post its comment on the ticket, switch the PR body to `Closes #N` when the
        summary says `complete: yes`, and remove the worktree once CI passed (the run dir stays)."""
        if s.get("comment"):
            self.tracker.comment(self.nn, s["comment"])
        if s.get("complete") == "yes":
            i, base = self.issue()
            for url in t["vars"]["pr_urls"]:
                sh([gh(), "pr", "edit", url, "--body", self.pr_body(t, i, base, closes=True)], self.root)
        if self.wt.is_dir() and (t["out"].get("ci") or {}).get("result") == "passed":
            sh(["git", "worktree", "remove", str(self.wt)], self.root)  # no --force: keep uncommitted work

    def trailer(self, t):
        return f"Factory-Run: {t['vars']['run_id'] or self.run_id}"

    def commits(self, t, base):
        """The branch's commits since origin/<base>, one `sha subject` per line (the developer's handoff
        lists only its own round)."""
        return sh(["git", "log", "--reverse", "--format=%h %s", f"origin/{base}..{t['vars']['branch']}"],
                  self.root).strip()

    def pr_body(self, t, i, base, closes=False):
        out, v = t["out"], t["vars"]
        lines = [f"[factory] {i['title']}", "", "## What changed", self.commits(t, base) or "See the commits.", "",
                 "## Checks"]
        lines += [f"- {c['name']} ({c['mode']}): {c['status']}" for c in (out.get("checks") or {}).get("checks", [])]
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
        elif action == "document":  # the reviewers' handoffs say what has docs impact
            blocks = {a: out[a] for a in REVIEWERS if a in out}
        elif action == "retro":
            blocks = {a: out[a] for a in ("implement", "fix", "checks", *REVIEWERS, "document", "approve", "ci") if a in out}
            blocks["numbers"] = self.run_numbers(t)
        elif action == "summary":
            lines += [f"stage: {t['state']} (the summary runs after the stage change)",
                      f"ci: {(out.get('ci') or {}).get('ci', 'unknown')}"]
            blocks = {a: out[a] for a in ("implement", "fix", "checks", *REVIEWERS, "document", "approve", "publish", "ci", "retro") if a in out}
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
            h["blocking_ids"] = re.findall(r"^\s*-?\s*id:\s*(\S+)", h.get("blocking") or "", re.M)
        if spec["role"] not in ("developer", "doc-writer"):
            return "done", h
        return (h["status"] if h.get("status") in ("done", "blocked") else "error"), h


def run_ticket(root, nn, tracker, executor, run_id=None, resume=False):
    """Deliver ticket `nn` until the machine waits on a human or external actor. Return (ticket, trace)."""
    d = Delivery(root, nn, tracker, run_id or f"local:{time.strftime('%Y%m%dT%H%M%S')}-{nn}")
    t, trace = drive(d.m, tracker, nn, executor, execute=d, resume=resume)
    d.save(t)  # sm.json shows the final stage
    if trace and trace[-1]["event"] in ("retro.done", "retro.failed"):  # only the run that reached approved
        d.summarize(t, trace)
    return t, trace


def main(argv=None):
    p = argparse.ArgumentParser(prog="drive.py", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="deliver a ticket until it waits on a human or external actor")
    r.add_argument("ticket")
    r.add_argument("--tracker", default="github", help="local:<effort dir> or github (default)")
    r.add_argument("--executor", help="claim name (default: the gh login, or the OS user for local)")
    r.add_argument("--resume", action="store_true", help="restart a failed ticket at the step that failed")
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
    try:
        t, trace = run_ticket(sm.ROOT, args.ticket, tracker, executor, resume=args.resume)
    except (ClaimError, ResumeError) as e:  # a claim held elsewhere, or no resume point
        print(f"drive.py: error: {e}", file=sys.stderr)
        return 2
    for rep in trace:
        print(event_line(rep))
    table = cost_table(sm.ROOT / yaml.safe_load((sm.ROOT / "factory.yaml").read_text())["targets"]["local"]["run_dir"] / args.ticket)
    if table:
        print(table)
    print(f"final: {t['state']}")
    return 0 if t["state"] == "approved" else 1


if __name__ == "__main__":
    sys.exit(main())

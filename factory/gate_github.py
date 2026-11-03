"""The GitHub adapter behind the gate's Tracker port (factory/gate_port.py).

Facts are read from GitHub on every call and nothing is stored (ADR-0014): no `seen` list, the stage
label and the marked reviews at the head are the state. The PR is found from the branch `ez/<n>-*`.
A marker `<!-- factory:review focus=<f> round=<n> verdict=approve|changes -->` counts only on a review by `bot_login`
(agent reviews are COMMENT reviews: the author cannot approve its own PR; the verdict is in the marker)
(`targets.air.bot_login`; `gh api user` answers 403 inside an Air run, so it is never called).
"""
import json
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import config
from gate_port import fix_round_count
from github_tracker import gh

COMPARE_FILE_LIMIT = 300  # GitHub compare lists at most this many files
NOTE_FIELDS, NOTE_FOR, NOTE_MAX = ("for", "files", "repro", "hint", "context"), ("fixer", "reviewer", "developer", "any"), 2000
NOTES_PARKED = "[factory] Agent notes for the PR description"  # ticket comment that holds them until the PR exists
NOTES = re.compile(r"<!-- factory:agent-notes\s*(\{.*?\})\s*-->", re.S)
SECRET = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)|\bAIza[\w-]{35}\b|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{20,}"
                    r"|\bgithub_pat_\w{20,}|\bxox[abprs]-[\w-]{10,}|\bsk-[A-Za-z0-9_-]{20,}"
                    r'|(?i:\b(?:api[_-]?key|secret|token|passw(?:or)?d)\b\s*[:=]\s*(?:"[^"]*"|[^\s",]+))', re.S)


def redact(v):
    """`v` with secret-looking text redacted in every string, before any serialisation."""
    if isinstance(v, str):
        return SECRET.sub("[redacted]", v)
    if isinstance(v, list):
        return [redact(x) for x in v]
    if isinstance(v, dict):
        return {k: redact(x) for k, x in v.items()}
    return v


def clean_notes(raw):
    """The notes object of one block: known fields only, secret-looking text redacted; ValueError when it is no
    object, `for` is unknown or it serialises past NOTE_MAX characters after redaction."""
    if not isinstance(raw, dict):
        raise ValueError("agent notes: needs an object")
    notes = {k: raw[k] for k in NOTE_FIELDS if k in raw}
    if notes.get("for") not in NOTE_FOR:
        raise ValueError(f"agent notes: `for` is one of {', '.join(NOTE_FOR)}")
    notes = {k: redact(v) for k, v in notes.items()}
    if len(json.dumps(notes)) > NOTE_MAX:
        raise ValueError(f"agent notes: over {NOTE_MAX} characters")
    return notes


def notes_block(notes):
    """The hidden block of cleaned notes; `<` and `>` are JSON-escaped so no text can close the comment early."""
    body = json.dumps(notes).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<!-- factory:agent-notes\n{body}\n-->"


def parse_notes(body, login="", kind="User"):
    """The valid notes blocks of `body`, only from a bot author: a person's text is no source of notes."""
    if kind != "Bot" and not login.endswith("[bot]"):
        return []
    out = []
    for m in NOTES.finditer(body or ""):
        try:
            out.append(clean_notes(json.loads(m[1])))
        except ValueError:
            pass
    return out


def gate_clear(n):
    from gate import clear_commands
    return clear_commands(n)


def gate_stop(f):
    """The reason a new fix round must not start, else None (the gate's own rule, shared with gate.apply)."""
    from gate import stop_reason
    return stop_reason(f, fix=True)


def deploy_plan_at(revision):
    """Render the plan with the code and config at `revision`, not today's generator."""
    with tempfile.TemporaryDirectory(prefix="factory-plan-") as path:
        subprocess.run(["git", "worktree", "add", "--detach", "--quiet", path, revision], check=True,
                       capture_output=True, text=True)
        try:
            output = subprocess.run(["uv", "run", "--quiet", "factory/deploy.py", "plan", "--target", "air"],
                                    cwd=path, check=True, capture_output=True, text=True).stdout
            return json.loads(output)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", path], check=True,
                           capture_output=True, text=True)

MARKER = re.compile(r"<!-- factory:review focus=(\S+) round=(\d+)(?: verdict=(approve|changes))?(?: rereview=(1))? -->")
FOCUSES = {"low": ["full"], "high": ["behaviour", "structure-security"]}
BAD_CI = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
GOOD_CI = {"success", "neutral", "skipped"}
WRITE = {"admin", "maintain", "write"}  # repository roles with write access


class GitHubGateTracker:
    def __init__(self, repo, stages, bot_login, risk_labels=None, fix_cap=3, run=gh, trusted_bots=(), run_cap=8,
                 ci_workflows=("quality-gates",)):
        self.trusted, self.run_cap, self.ci_workflows = set(trusted_bots), run_cap, set(ci_workflows)
        self.repo, self.stages, self.bot, self.run = repo, stages, bot_login, run
        self.risk_labels, self.fix_cap = risk_labels or {"low": "risk:low", "high": "risk:high"}, fix_cap

    @classmethod
    def from_config(cls, path=None, run=gh):
        c = yaml.safe_load(Path(path).read_text()) if path else config.load()
        bot = ((c.get("targets") or {}).get("air") or {}).get("bot_login")
        if not bot:
            raise ValueError("descriptor: targets.air.bot_login is not set")
        t = c["tracker"]
        return cls(t["repository"], t["stages"], bot, t.get("risk_labels"), c["limits"]["fix_rounds"], run,
                   (c.get("reviews") or {}).get("trusted_bots") or [], c["limits"].get("agent_runs_per_ticket", 8),
                   (c.get("ci") or {}).get("workflows") or ["quality-gates"])

    # --- reads
    def _api(self, path, *flags):
        return json.loads(self.run(["api", f"repos/{self.repo}/{path}", *flags]))

    def _list(self, path):
        out = self.run(["api", "--paginate", f"repos/{self.repo}/{path}", "--jq", ".[]"])
        return [json.loads(x) for x in out.splitlines() if x.strip()]

    def _labels(self, n):
        return [x["name"] for x in self._api(f"issues/{n}")["labels"]]

    def record_node_error(self, n):
        """Leave a durable canary signal when a sweep catches a node exception."""
        if "factory:canary" in self._labels(n):
            self.run(["api", f"repos/{self.repo}/issues/{n}/comments", "-f",
                      "body=[factory] A factory node reported an execution error.\n\n"
                           "<!-- factory-node-error -->\n\nFactory-Run: sweep:node-error"])

    def sweep_tickets(self, stages, open_only=False):
        """Issue numbers (open or closed) whose stage label is one of `stages`; ready, in-review, changes-requested and in-dev
        (every stage with `open_only`) only when open."""
        found = set()
        for st in stages:
            state = "open" if open_only or st in ("triage", "ready", "in-review", "changes-requested", "in-dev") else "all"
            found |= {i["number"] for i in self._list(f"issues?state={state}&labels={self.stages[st]}&per_page=100")
                      if "pull_request" not in i}
        return sorted(found)

    def config_change_merged(self, pr):
        """Create a deployment reminder when this merge changes the rendered Air plan."""
        if not pr:
            return
        import config
        pull = self._api(f"pulls/{pr}")
        merge_sha = pull.get("merge_commit_sha")
        if not merge_sha:
            return
        commit = self._api(f"commits/{merge_sha}")
        parents = commit.get("parents") or []
        if not parents:
            return

        before, after = deploy_plan_at(parents[0]["sha"]), deploy_plan_at(merge_sha)
        if before == after:
            return
        # Ensure labels exist before the new plan can wake CI or an automation.
        known = {x["name"] for x in self._list("labels?per_page=100")}
        for label in after["labels"]:
            if label not in known:
                self.run(["api", f"repos/{self.repo}/labels", "-f", f"name={label}", "-f", "color=ededed"])
        title = "Deploy needed"
        existing = next((i for i in self._list("issues?state=open&per_page=100")
                         if "pull_request" not in i and i.get("title") == title), None)
        items = []
        for key in ("automations", "environments", "labels", "files"):
            old, new = ((before.get(key) or {}, after.get(key) or {}) if key == "files"
                        else (before.get(key) or [], after.get(key) or []))
            if key in ("automations", "environments"):
                old = {x["name"]: x for x in old}
                new = {x["name"]: x for x in new}
            elif key == "labels":
                old, new = {x: x for x in old}, {x: x for x in new}
            else:
                old, new = old, new
            items.extend(f"- {key}: {name}" for name in sorted(old.keys() | new.keys()) if old.get(name) != new.get(name))
        body = ("[factory] The merged commit changes the Air deploy plan. Run `factory deploy --apply` locally.\n\n"
                + "\n".join(items) + f"\n\nFactory-Run: finish:{pr}")
        if existing:
            self.run(["issue", "edit", str(existing["number"]), "-R", self.repo, "--body", body,
                      "--add-label", "factory:needs-human", "--add-assignee", config.load()["notify"]["github_user"]])
            self.run(["issue", "comment", str(existing["number"]), "-R", self.repo, "--body",
                      f"[factory] Deploy plan changed by #{pr}.\n\nFactory-Run: finish:{pr}"])
        else:
            url = self.run(["issue", "create", "-R", self.repo, "--title", title, "--body", body,
                            "--label", "factory:needs-human", "--assignee", config.load()["notify"]["github_user"]]).strip()
            self.run(["issue", "comment", url.rsplit("/", 1)[-1], "-R", self.repo, "--body",
                      f"[factory] Deploy plan changed by #{pr}.\n\nFactory-Run: finish:{pr}"])

    def deploy_applied(self, plan):
        """Close the reminder after an operator confirms the deployed plan is unchanged."""
        issue = next((i for i in self._list("issues?state=open&per_page=100")
                      if "pull_request" not in i and i.get("title") == "Deploy needed"), None)
        if not issue:
            return
        self.run(["issue", "comment", str(issue["number"]), "-R", self.repo, "--body",
                  "[factory] `factory deploy --apply` confirmed the current plan is applied. <!-- factory:deploy-applied -->\n\nFactory-Run: deploy:apply"])
        self.run(["issue", "close", str(issue["number"]), "-R", self.repo])

    def _pr(self, n):
        """The ticket's PR from its branch: the open one, else the newest. None without a PR."""
        prs = [p for p in self._list("pulls?state=all&per_page=100") if p["head"]["ref"].startswith(f"ez/{n}-")
               and ((p["head"].get("repo") or {}).get("full_name") or "").lower() == self.repo.lower()]  # never a fork's PR
        prs.sort(key=lambda p: (p["state"] == "open", p["number"]))
        if not prs:
            return None
        pr = self._api(f"pulls/{prs[-1]['number']}")
        for _ in range(3):  # GitHub computes `mergeable` lazily: null means UNKNOWN, read again
            if pr.get("state") != "open" or pr.get("mergeable") is not None:
                break
            time.sleep(2)
            pr = self._api(f"pulls/{prs[-1]['number']}")
        return pr

    def ticket_for_pr(self, number):
        """Resolve a PR number to its ticket from the live head ref."""
        pr = self._api(f"pulls/{number}")
        ref = ((pr.get("head") or {}).get("ref") or "")
        match = re.match(r"(?:refs/heads/)?ez/(\d+)-", ref)
        return int(match[1]) if match else None

    def _prs(self, n, open_only=False):
        """The ticket's PRs from its branch; cleanup also includes closed PRs."""
        prs = [p for p in self._list("pulls?state=all&per_page=100")
               if p["head"]["ref"].startswith(f"ez/{n}-")
               and ((p["head"].get("repo") or {}).get("full_name") or "").lower() == self.repo.lower()
               and (not open_only or p["state"] == "open")]
        return sorted(prs, key=lambda p: p["number"])

    def _ci(self, sha):
        """CI at `sha` from the Actions workflow runs (the Air GitHub App has Actions: read, not Checks: read, so
        check-runs answers 403 in a run). Per workflow the latest run counts, so a rerun replaces a failure."""
        latest = self._workflow_runs(sha)
        if not latest:
            return "none"
        done = [r.get("conclusion") if r.get("status") == "completed" else None for r in latest.values()]
        if any(c in BAD_CI for c in done):
            return "red"
        return "green" if all(c in GOOD_CI for c in done) else "pending"

    def _workflow_runs(self, sha, timeout=None):
        out = self.run(["api", "--paginate", f"repos/{self.repo}/actions/runs?head_sha={sha}&per_page=100",
                        "--jq", ".workflow_runs[]"], **({"timeout": timeout} if timeout is not None else {}))
        latest = {}
        for r in (json.loads(x) for x in out.splitlines() if x.strip()):
            if r.get("name") not in self.ci_workflows:  # factory service workflows (reconcile, ci-state) are not CI
                continue
            key = (r.get("workflow_id"), r.get("event"))
            if key not in latest or (r.get("run_number"), r.get("run_attempt", 1)) > (
                    latest[key].get("run_number"), latest[key].get("run_attempt", 1)):
                latest[key] = r
        return latest

    def ci_failure(self, sha):
        """Best-effort details for the first failed step at the head."""
        try:
            for run in self._workflow_runs(sha, timeout=10).values():
                if run.get("conclusion") not in BAD_CI:
                    continue
                out = self.run(["api", "--paginate", f"repos/{self.repo}/actions/runs/{run['id']}/jobs?per_page=100",
                                "--jq", ".jobs[]"], timeout=10)
                for line in out.splitlines():
                    if not line.strip():
                        continue
                    job = json.loads(line)
                    steps = [step for step in job.get("steps", []) if step.get("conclusion") in BAD_CI]
                    if not steps:
                        continue
                    for step in steps:
                        failure = {"job": job["name"], "step": step["name"], "log": None}
                        try:
                            logs = self.run(["api", f"repos/{self.repo}/actions/jobs/{job['id']}/logs"], timeout=10)
                            if not logs.strip():
                                raise RuntimeError("empty log response")
                            failure["log"] = "\n".join(logs.splitlines()[-60:])
                        except Exception as e:
                            failure["error"] = " ".join(str(e).splitlines()) or type(e).__name__
                        return failure
        except Exception as e:
            return {"job": None, "step": None, "log": None,
                    "error": " ".join(str(e).splitlines()) or type(e).__name__}
        return None

    def _reviews(self, pr):
        out = []
        for r in self._list(f"pulls/{pr['number']}/reviews"):
            m = next(iter(list(MARKER.finditer(r.get("body") or ""))[-1:]), None)  # the footer marker is last; prose cannot follow it
            agent = bool(m) and r["user"]["login"] == self.bot
            verdict = (m[3] if agent else {"APPROVED": "approve", "CHANGES_REQUESTED": "changes"}.get(r["state"]))
            out.append({"author": r["user"]["login"], "state": r["state"], "commit_id": r["commit_id"],
                        "focus": m[1] if m else None, "round": int(m[2]) if m else None,
                        "rereview": bool(m and m[4]),
                        "verdict": verdict, "agent": agent, "assoc": r.get("author_association"), "submitted_at": r.get("submitted_at"),
                        "bot": r["user"].get("type") == "Bot" or r["user"]["login"].endswith("[bot]")})
            out[-1]["findings"] = re.findall(r"^\*\*([A-Z]\d+)\*\*", r.get("body") or "", re.M)
        return out

    def _pr_patch(self, base, sha):
        """The PR's patch at `sha` as {file: patch with hunk line numbers dropped}, like `git patch-id --stable`.
        None when GitHub omits a patch (binary or huge diff) or the compare list may be cut at its 300-file limit,
        so the caller cannot compare."""
        files = self._api(f"compare/{base}...{sha}")["files"]
        if len(files) >= COMPARE_FILE_LIMIT or any(x.get("patch") is None for x in files):
            return None
        return {x["filename"]: re.sub(r"^@@ .*? @@", "@@", x["patch"], flags=re.M) for x in files}

    def _clean_base_advance(self, old, head, pr):
        """True when walking first parents from `head` reaches `old` within 20 steps, every step is a merge whose
        other parent is already on the base branch, and the PR's patch is identical at both. Who made the merge does
        not matter; what it contains does. (`compare` also lists the base commits a merge brought in, so it is no guide.)"""
        base = pr["base"]["ref"]
        try:
            cur = head
            for _ in range(20):
                if cur == old:
                    break
                parents = [x["sha"] for x in self._api(f"commits/{cur}")["parents"]]
                if len(parents) != 2 or self._api(f"compare/{base}...{parents[1]}")["ahead_by"] != 0:
                    return False
                cur = parents[0]
            patch = self._pr_patch(base, old)
            return cur == old != head and patch is not None and patch == self._pr_patch(base, head)
        except Exception:  # unknown means the new head needs a review
            return False

    def _carry_approvals(self, reviews, pr):
        """An agent approval of an earlier commit counts at `head` after a clean merge of the base (marked `carried`)."""
        head, clean = pr["head"]["sha"], {}
        for r in reviews:
            if r["agent"] and r["verdict"] == "approve" and r["commit_id"] != head:
                if clean.setdefault(r["commit_id"], self._clean_base_advance(r["commit_id"], head, pr)):
                    r.update(commit_id=head, carried=True)
        return reviews

    def _can_write(self, login):
        """Write access from the collaborator permission API (author_association says nothing about the role)."""
        try:
            return self._api(f"collaborators/{login}/permission").get("role_name") in WRITE
        except Exception as e:
            if "404" in str(e):  # a stranger is no collaborator
                return False
            raise  # any other failure must not drop a blocking review: the join fails and is retried

    def _agent_runs(self, n, pr, issue_events=None):
        """Developer runs started for ticket `n` since a person last set `factory:ready`: each `labeled` event
        of a command label (`factory:implement-*`, `fix-*`, `update-*`) on the issue or its PR after it. A
        person's `ready` starts a fresh budget. Reviews follow pushes, so this bounds them too."""
        cmds = ("factory:implement-", "factory:fix-", "factory:update-")
        events = {t: issue_events if t == n and issue_events is not None else self._list(f"issues/{t}/events")
                  for t in [n] + ([pr] if pr else [])}
        name = lambda e: (e.get("label") or {}).get("name", "") if e.get("event") == "labeled" else ""
        since = self._since = max((e.get("created_at") or "" for e in events[n] if name(e) == self.stages.get("ready")),
                                  default="")
        return sum(1 for es in events.values() for e in es if name(e).startswith(cmds) and (e.get("created_at") or "") >= since)

    def parent(self, n):
        """The parent issue number of sub-issue `n`, else None."""
        try:
            return self._api(f"issues/{n}/parent").get("number")
        except Exception as e:
            if "404" in str(e):
                return None
            raise

    def blocking(self, n):
        """Issues whose native or body fallback dependency list names `n` as a blocker."""
        dependents = self._list(f"issues/{n}/dependencies/blocking")
        known = {i["number"] for i in dependents}
        for issue in self._list("issues?state=open&per_page=100"):
            if issue["number"] != n and issue["number"] not in known and re.search(
                    rf"(?im)^Blocked by: #{n}\s*$", issue.get("body") or ""):
                dependents.append(issue)
                known.add(issue["number"])
        return dependents

    def not_planned(self, n):
        """True when issue `n` was closed with state_reason not_planned."""
        issue = self._api(f"issues/{n}")
        return issue.get("state") == "closed" and issue.get("state_reason") == "not_planned"

    def _fix_writes(self, pr):
        """Times the join started a fix round on `pr`: each `labeled factory:fix-*` event (the fix node removes
        the label when it finishes, so every round adds it again)."""
        return [e["created_at"] for e in self._list(f"issues/{pr}/events")
                if e.get("event") == "labeled" and (e.get("label") or {}).get("name", "").startswith("factory:fix-")]

    def _earlier(self, r, head, fix_writes):
        """True when change request `r` was counted by an earlier round: it names an older commit and a join
        started a fix round at or after it, so the fix was given it. A request sent during a fix, or with no
        fix round after it, counts now. Without any join write (a person pushed by hand), the push of the
        current head ends the earlier round."""
        if r["commit_id"] == head:
            return False
        at = r["submitted_at"] or ""
        if fix_writes:
            return any(t >= at for t in fix_writes)
        return at <= self._api(f"commits/{head}")["commit"]["committer"]["date"]

    def _round(self, reviews, required, head, requested, ci, pr=None):
        """(complete, verdict, ignored, waiting) of the round at `head`. `waiting`: people whose change request a
        fix already received and who have not decided again; the round stays open until they do. The round is marked agent reviews of every required focus at
        head, no requested reviewer left (GitHub drops a person on review and adds them back on a re-request), CI
        concluded. A person (with write access) or trusted bot blocks with a latest CHANGES_REQUESTED on head or
        submitted since the round started; COMMENTED never counts."""
        at_head = {r["focus"]: r for r in reviews if r["agent"] and r["commit_id"] == head}
        others = {r["author"]: r for r in reviews if not r["agent"] and r["verdict"]
                  and (r["author"] in self.trusted if r["bot"] else self._can_write(r["author"]))}
        ignored = [r["author"] for r in reviews if r["bot"] and not r["agent"] and r["author"] not in self.trusted]
        if ci == "red":  # a red head needs a fix whatever the reviews say; reviewers only start on green
            return True, "changes", ignored, []
        complete = all(x in at_head for x in required) and not requested and ci == "green"
        if not complete:
            return False, None, ignored, []
        late = [r for r in others.values() if r["verdict"] == "changes"]
        if any(r["commit_id"] != head for r in late):
            writes = self._fix_writes(pr) if pr is not None else []
            # A request a fix already received stays open until its author decides again: the round waits.
            # A trusted bot cannot be asked again, so its handled request counts as resolved instead.
            waiting = [r["author"] for r in late if self._earlier(r, head, writes) and not r["bot"]]
            late = [r for r in late if not (r["bot"] and self._earlier(r, head, writes))]
            if waiting:
                return False, None, ignored, waiting
        changes = ci == "red" or any(at_head[x]["verdict"] != "approve" for x in required) or bool(late)
        return True, "changes" if changes else "approve", ignored, []

    def facts(self, n):
        issue = self._api(f"issues/{n}")
        labels = [x["name"] for x in issue["labels"]]
        by_label = {v: k for k, v in self.stages.items()}
        events = self._list(f"issues/{n}/events")
        actor_events = [e for e in events if e.get("event") in ("labeled", "unlabeled", "closed", "reopened")]
        # The stage is the stage label added last (a person's factory:ready on a running ticket wins over the
        # label it replaces); `apply` of the stage effect removes the others.
        # Order by event position, not timestamp: the events list is chronological and timestamps can tie.
        added = {}
        for i, e in enumerate(events):
            if e.get("event") == "labeled":
                added[e["label"]["name"]] = i
        stage = max((x for x in labels if x in by_label), key=lambda x: added.get(x, -1), default=None)
        stage = by_label.get(stage, "none")
        blockers = [b for b in self._list(f"issues/{n}/dependencies/blocked_by") if b["state"] == "open"]
        body = issue.get("body") or ""
        for number in re.findall(r"(?im)^Blocked by: #(\d+)\s*$", body):
            blocker = self._api(f"issues/{number}")
            if blocker.get("state") == "open":
                blockers.append({"number": int(number)})
        risk = "low" if self.risk_labels["low"] in labels else "high"
        required, pr = FOCUSES[risk], self._pr(n)
        f = {"stage": stage, "attrs": [x for x in labels if x not in by_label],
             "assignees": [a["login"] for a in issue.get("assignees", [])], "blockers_open": len(blockers),
             "head": None, "pr": None, "mergeable": None, "conflict": False, "ci": "none", "reviews": [], "requested_reviewers": [],
             "focus_missing": [], "round_complete": False, "round_verdict": None, "round_head": None,
             "ignored_reviews": [], "waiting_reviewers": [], "fix_rounds": 0,
             "fix_cap": self.fix_cap, "due": False, "seen": [], "pr_state": "none", "pending_rereviews": [],
             "agent_runs": self._agent_runs(n, pr["number"] if pr else None, events), "run_cap": self.run_cap,
             "handed_to_fix": False, "risk": risk, "behind": False,
             "issue_state": issue.get("state", "open"), "updated_at": issue.get("updated_at"),
             "event_actor": ((actor_events[-1].get("actor") or {}).get("login") if actor_events else None),
             "title": issue.get("title", ""), "body": body, "author": (issue.get("user") or {}).get("login"), "branch": None}
        since = getattr(self, "_since", "")  # a person's factory:ready starts fresh
        comments = self._list(f"issues/{n}/comments")
        f["comments"] = comments
        got = self._notes(comments, "created_at", since)
        if stage == "needs-answers":
            waiting = [e for e in events if e.get("event") == "labeled" and e.get("label", {}).get("name") == self.stages["needs-answers"]]
            if waiting and comments:
                started = waiting[-1].get("created_at", "")
                ready = [e for e in events if e.get("event") == "labeled" and e.get("label", {}).get("name") in (self.stages["triage"], self.stages["ready"])]
                setter = (ready[-1].get("actor") or {}).get("login") if ready else None
                f["answered"] = any((c.get("created_at") or "") > started and
                                     (c.get("user") or {}).get("type") != "Bot" and
                                     not (c.get("user") or {}).get("login", "").endswith("[bot]") and
                                     ((c.get("user") or {}).get("login") == setter or
                                      self._can_write((c.get("user") or {}).get("login", "")))
                                     for c in comments)
        if stage == "split":
            kids = self._list(f"issues/{n}/sub_issues")
            f.update(children=[{"number": k.get("number"), "state": k.get("state"), "not_planned": k.get("state") == "closed" and k.get("state_reason") == "not_planned",
                                "blocked": k.get("state") != "closed" and self.stages["blocked"] in [x["name"] for x in k.get("labels") or []]}
                               for k in kids])
        if pr:
            head, reviews = pr["head"]["sha"], self._carry_approvals(self._reviews(pr), pr)
            pr_labels = {x["name"] for x in pr.get("labels", [])}
            pending_rereviews = [focus for focus in required if f"factory:rereview-{focus}" in pr_labels]
            latest = {r["focus"]: r for r in reviews if r["agent"]}  # the later review of a focus wins
            heads = {latest[x]["commit_id"] for x in required if x in latest}
            requested = [u["login"] for u in pr.get("requested_reviewers") or []]
            ci = self._ci(head)
            since = getattr(self, "_since", "")  # a person's factory:ready starts a fresh round count
            writes = [t for t in self._fix_writes(pr["number"]) if t >= since]
            latest_review = max((r.get("submitted_at") or "" for r in reviews if r["commit_id"] == head), default="")
            commit_date = self._api(f"commits/{head}")["commit"]["committer"]["date"]
            f["handed_to_fix"] = bool(writes) and max(writes) > max(latest_review, commit_date)
            complete, verdict, ignored, waiting = self._round(reviews, required, head, requested, ci, pr["number"])
            f.update(branch=pr["head"]["ref"])
            got += (self._notes(self._list(f"issues/{pr['number']}/comments"), "created_at", since)
                    + self._notes(self._list(f"pulls/{pr['number']}/reviews"), "submitted_at", since)
                    + self._notes([pr], "updated_at", since))  # the PR body is edited after creation
            if stage == "approved" and pr.get("state") == "open":  # only the merge gates read it
                f["behind"] = self._api(f"compare/{pr['base']['ref']}...{head}")["behind_by"] > 0
            f.update(pr_state="merged" if pr.get("merged") else "closed" if pr.get("state") == "closed" else "open")
            f.update(pr=pr["number"], head=head, mergeable=pr.get("mergeable"), conflict=pr.get("mergeable") is False, ci=ci, reviews=reviews,
                     requested_reviewers=requested, round_verdict=verdict, ignored_reviews=ignored, waiting_reviewers=waiting,
                     pending_rereviews=pending_rereviews, pr_labels=sorted(pr_labels),
                     focus_missing=[x for x in required if latest.get(x, {}).get("commit_id") != head],
                     round_complete=complete, round_head=heads.pop() if all(x in latest for x in required) and len(heads) == 1 else None,
                     fix_rounds=fix_round_count(reviews, since))  # same-head dispute rereviews count as a new round
        f["agent_notes"] = [n for _, n in sorted(got, key=lambda t: t[0], reverse=True)]
        return f

    def _notes(self, items, stamp, since):
        """(time, notes) of the bot-written items (comments, reviews, a PR) from `since` on."""
        return [(x.get(stamp) or "", n) for x in items if (x.get(stamp) or "") >= since
                for n in parse_notes(x.get("body"), (x.get("user") or {}).get("login", ""), (x.get("user") or {}).get("type", ""))]

    def stale_facts(self, n):
        """facts plus what the stale sweep needs: `since` (last label or commit event, ISO), the command label
        (`factory:implement-*` on the issue, `factory:fix-*` on the PR) and when it was added, the head commit
        date, the PR's CI label, and the `[factory] recover:` comments written since the stage began."""
        f, labeled, events = self.facts(n), {}, []
        pr = f["pr"]
        for t in [n] + ([pr] if pr else []):
            for e in self._list(f"issues/{t}/events"):
                events.append((t, e))
                if e.get("event") == "labeled":
                    labeled[(t, e["label"]["name"])] = max(e["created_at"], labeled.get((t, e["label"]["name"]), ""))
        pr_labels = f.get("pr_labels", [])
        review_labels = [(x, labeled.get((pr, x), "")) for x in pr_labels
                         if x.startswith(("factory:review-", "factory:rereview-")) and labeled.get((pr, x))]
        have = [(n, x) for x in self._labels(n)] + [(pr, x) for x in pr_labels]
        cmd = next(((t, x) for t, x in have if x.startswith(("factory:implement-", "factory:fix-", "factory:update-", "factory:review-"))), None)
        command_labels = ("factory:ready", "factory:triage", "factory:implement-", "factory:fix-",
                          "factory:update-", "factory:ci-green")
        active_commands = {(t, x) for t, x in have if x == "factory:ready" or x == "factory:triage"
                           or x == "factory:ci-green" or x.startswith(command_labels[2:5]) or x.startswith("factory:review-")}
        command_events = [e for t, e in events if e.get("event") == "labeled"
                          and (t, e.get("label", {}).get("name")) in active_commands]
        commit_at = self._api(f"commits/{f['head']}")["commit"]["committer"]["date"] if f["head"] else None
        latest_event = max([e.get("created_at", "") for _, e in events] + [commit_at or ""])
        latest_command = max((e["created_at"] for e in command_events), default="")
        stage_at = labeled.get((n, self.stages[f["stage"]]), "") if f["stage"] in self.stages else ""
        comments = [c["body"] for c in self._list(f"issues/{n}/comments") if c["body"].startswith(("[factory] recover:", "[factory] reconcile:"))
                    and c["created_at"] >= stage_at]
        return {**f, "since": max([*labeled.values(), commit_at or ""]) or None, "command": cmd and cmd[1], "stage_at": stage_at or None,
                "command_pending": bool(latest_command and latest_command == latest_event),
                "command_at": cmd and labeled.get(cmd), "commit_at": commit_at, "recover_comments": comments,
                "review_labels": review_labels,
                "ci_label": next((x[11:] for x in pr_labels if x in ("factory:ci-green", "factory:ci-red")
                                 and x[11:] == f.get("ci")), None)}  # a label left over from an earlier head is not this head's CI

    # --- effects: read the target first, write only when it differs
    def _edit(self, n, add=(), remove=(), on="issue"):
        flags = [x for a in add for x in ("--add-label", a)] + [x for r in remove for x in ("--remove-label", r)]
        self.run([on, "edit", str(n), "-R", self.repo, *flags])
        return True

    def apply(self, e):
        n, kind, value = e["ticket"], e["kind"], e["value"]
        if kind == "seen":
            return False  # no hidden state: conditions catch a duplicate
        if kind == "join":
            return self._join(n, e.get("tier") or "mid")
        if kind == "stage":
            want, have = self.stages[value], [x for x in self._labels(n) if x in self.stages.values()]
            closed = False
            if value == "done" and self._api(f"issues/{n}").get("state", "open") == "open":
                self.run(["issue", "close", str(n), "-R", self.repo])  # before the label: a retry still finds the ticket
                closed = True
            return (have != [want] and self._edit(n, [want] if want not in have else [], [x for x in have if x != want])) or closed
        if kind in ("label", "unlabel"):
            on, target = e.get("on") or "issue", n
            if on == "pr":
                attention = value == "factory:needs-human"
                if attention:
                    prs = self._prs(n, open_only=kind == "label")
                else:
                    pr = self._pr(n)
                    prs = [pr] if pr else []
                if not prs:
                    return False  # ticket-level waits also apply before a PR exists
                changed = False
                for pr in prs:
                    labels = self._labels(pr["number"])
                    if kind == "label":
                        if value not in labels:
                            changed |= self._edit(pr["number"], [value], on=on)
                    else:
                        gone = [x for x in labels if x.startswith(value[:-1])] if value.endswith("*") else [value] if value in labels else []
                        if gone:
                            changed |= self._edit(pr["number"], remove=gone, on=on)
                return changed
            elif on != "issue":
                raise ValueError(f"{kind}: bad target: {on}")
            labels = self._labels(target)
            if kind == "label":
                return value not in labels and self._edit(target, [value], on=on)
            # `prefix-*` removes every label with that prefix (a command label of any tier)
            gone = [x for x in labels if x.startswith(value[:-1])] if value.endswith("*") else [value] if value in labels else []
            return bool(gone) and self._edit(target, remove=gone, on=on)
        if kind == "set-field":  # an attribute label `<field>:<value>`; one value per field
            (k, v), = value.items()
            have = [x for x in self._labels(n) if x.startswith(f"{k}:")]
            return have != [f"{k}:{v}"] and self._edit(n, [f"{k}:{v}"] if f"{k}:{v}" not in have else [],
                                                       [x for x in have if x != f"{k}:{v}"])
        if kind in ("assign", "unassign"):
            on, target = e.get("on") or "issue", n
            if on == "pr":
                prs = self._prs(n, open_only=kind == "assign")
                if not prs:
                    return False
                changed = False
                for pr in prs:
                    current = {a["login"] for a in self._api(f"issues/{pr['number']}").get("assignees", [])}
                    desired = set((value if isinstance(value, list) else [value])) if kind == "assign" else set()
                    if current != desired:
                        flags = [x for a in sorted(desired - current) for x in ("--add-assignee", a)]
                        flags += [x for a in sorted(current - desired) for x in ("--remove-assignee", a)]
                        self.run(["issue", "edit", str(pr["number"]), "-R", self.repo, *flags])
                        changed = True
                return changed
            elif on != "issue":
                raise ValueError(f"{kind}: bad target: {on}")
            current = {a["login"] for a in self._api(f"issues/{target}").get("assignees", [])}
            desired = set((value if isinstance(value, list) else [value])) if kind == "assign" else set()
            if current == desired:
                return False
            flags = [x for a in sorted(desired - current) for x in ("--add-assignee", a)]
            flags += [x for a in sorted(current - desired) for x in ("--remove-assignee", a)]
            self.run(["issue", "edit", str(target), "-R", self.repo, *flags])
            return True
        if kind == "comment":
            body = value if value.startswith("[factory]") else f"[factory] {value}"
            if e.get("run_id") and "Factory-Run:" not in body:
                body += f"\n\nFactory-Run: {e['run_id']}"
            target = e.get("on") or "issue"
            pr = self._pr(n) if target == "pr" else None
            if target == "pr" and pr is None:
                raise RuntimeError(f"comment: no PR for ticket {n}")
            if any(c["body"] == body for c in self._list(f"issues/{pr['number'] if pr else n}/comments")):
                return False
            body += self._block(e)
            self.run(["issue", "comment", str(pr["number"] if pr else n), "-R", self.repo, "--body", body])
            return True
        if kind in ("pr-notes", "pr-notes-sync"):  # a developer's `done` notes for the PR description
            pr = self._pr(n)
            if kind == "pr-notes" and pr is None:  # Air opens the PR after the run: park the notes on the ticket
                body = f"{NOTES_PARKED}\n\n{value}"
                if e.get("run_id"):
                    body += f"\n\nFactory-Run: {e['run_id']}"
                if any(c["body"] == body for c in self._list(f"issues/{n}/comments")):
                    return False
                self.run(["issue", "comment", str(n), "-R", self.repo, "--body", body])
                return True
            if pr is None:
                return False
            if kind == "pr-notes-sync":  # the first gate step that sees the PR copies the parked notes
                parked = [c for c in self._list(f"issues/{n}/comments") if (c.get("body") or "").startswith(NOTES_PARKED)]
                found = parse_notes(parked[-1]["body"], (parked[-1].get("user") or {}).get("login", ""),
                                    (parked[-1].get("user") or {}).get("type", "")) if parked else []
                if not found:
                    return False
                value = notes_block(found[-1])
            if value in (pr.get("body") or ""):
                return False
            self.run(["pr", "edit", str(pr["number"]), "-R", self.repo, "--body", f"{pr.get('body') or ''}\n\n{value}"])
            return True
        if kind == "review":
            return self._review(n, e)
        if kind == "request-reviewers":
            return self._rerequest(n)
        if kind == "create-issues":
            return self._create(n, value)
        if kind == "park-behind-main-red":
            return self._park(n, value)
        if kind == "merge":
            pr = self._pr(n)
            if pr is None:
                raise RuntimeError(f"merge: no PR for ticket {n}")
            if pr["merged"]:
                return False
            return self._locked_merge(pr, e.get("head") or pr["head"]["sha"])
        if kind == "update-branch":  # a merge commit of the base into the PR branch; refused when the head moved
            pr = self._pr(n)
            if pr is None or pr["merged"]:
                return False
            self._update_branch(pr, e.get("head") or pr["head"]["sha"])
            return True
        raise ValueError(f"unknown effect: {kind}")

    def _update_branch(self, pr, head):
        self.run(["api", "-X", "PUT", f"repos/{self.repo}/pulls/{pr['number']}/update-branch",
                  "-f", f"expected_head_sha={head}"])

    MERGE_LOCK, RECENT_MERGE_SECONDS = "factory-merge-lock", 60

    def _locked_merge(self, pr, head):
        """One merge at a time across runs: the lock is a branch, created atomically (GitHub refuses a second create).
        Under it, wait out RECENT_MERGE_SECONDS after another PR's merge, so the base this
        merge integrates is the one the gates judged. False (nothing merged) means the next sweep reads it again.
        A crashed run leaves the branch; a person deletes it."""
        base = pr["base"]["ref"]
        try:
            self.run(["api", "-X", "POST", f"repos/{self.repo}/git/refs", "-f", f"ref=refs/heads/{self.MERGE_LOCK}",
                      "-f", f"sha={self._api(f'git/ref/heads/{base}')['object']['sha']}"])
        except RuntimeError:
            return False  # another run holds the lock
        try:
            now = datetime.now(timezone.utc)
            wait = max((self.RECENT_MERGE_SECONDS - (now - datetime.fromisoformat(o["merged_at"].replace("Z", "+00:00"))).total_seconds()
                        for o in self._api(f"pulls?state=closed&base={base}&sort=updated&direction=desc&per_page=5")
                        if o["number"] != pr["number"] and o.get("merged_at")), default=0)
            if wait > 0:
                time.sleep(wait)  # no event wakes `merge` again after the window, so wait it out under the lock
            if self._api(f"compare/{base}...{head}")["behind_by"] > 0:
                self._update_branch(pr, head)  # the merge that just landed moved the base: CI reruns, ci-green wakes `merge`
                return False
            self.run(["pr", "merge", str(pr["number"]), "-R", self.repo, "--merge", "--match-head-commit", head])
            return True
        finally:
            self.run(["api", "-X", "DELETE", f"repos/{self.repo}/git/refs/heads/{self.MERGE_LOCK}"])

    def _join(self, n, tier):
        """Nothing while the round is open; else changes -> PR label fix-<tier> + stage changes-requested, approve -> approved."""
        f = self.facts(n)
        for who in f["ignored_reviews"]:
            print(f"join: ignored unmarked bot review by {who}", file=sys.stderr)
        rereviewed = any(r["agent"] and r["rereview"] and r["commit_id"] == f["head"] for r in f["reviews"])
        if f["handed_to_fix"] and not rereviewed:
            return False  # this head already went to a fix; its push starts the next round
        if f["pending_rereviews"]:
            return False  # every disputed focus gets its same-head review before the round can move
        if f["stage"] == "in-review" and f["waiting_reviewers"]:
            return self._rerequest(n, f["waiting_reviewers"])  # nothing moves until they decide again
        if not f["round_complete"] or f["stage"] != "in-review":
            return False
        if f["round_verdict"] == "approve":
            return self.apply({"ticket": n, "kind": "stage", "value": "approved"})
        stop = gate_stop(f)
        if stop:  # no further developer run starts: a person decides
            self.apply({"ticket": n, "kind": "comment", "value": stop})
            for e in gate_clear(n):
                self.apply(e)
            return self.apply({"ticket": n, "kind": "stage", "value": "blocked"})
        if rereviewed:
            self.apply({"ticket": n, "kind": "unlabel", "value": "factory:fix-*", "on": "pr"})
        wrote = self.apply({"ticket": n, "kind": "label", "value": f"factory:fix-{tier}", "on": "pr"})
        return self.apply({"ticket": n, "kind": "stage", "value": "changes-requested"}) or wrote

    def _rerequest(self, n, only=None):
        """Ask again every person whose latest non-COMMENTED review requests changes, so the next round waits
        for them (the `fix` node's `done`, after the push: a request at join time would hold the fix itself)."""
        pr = self._pr(n)
        if pr is None:
            return False
        latest = {}
        for r in self._reviews(pr):
            if not r["agent"] and not r["bot"] and r["verdict"]:
                latest[r["author"]] = r["verdict"]
        requested = {u["login"] for u in pr.get("requested_reviewers") or []}
        who = [a for a, v in latest.items() if v == "changes" and a not in requested and (only is None or a in only)
               and self._can_write(a)]
        if not who:
            return False
        self.run(["api", "-X", "POST", f"repos/{self.repo}/pulls/{pr['number']}/requested_reviewers",
                  *[x for a in who for x in ("-f", f"reviewers[]={a}")]])
        return True

    @staticmethod
    def _block(e):
        """The hidden notes block an effect carries (`notes`: the gate's cleaned object), after the visible text."""
        return f"\n\n{notes_block(e['notes'])}" if e.get("notes") else ""

    def _review(self, n, e):
        pr, focus = self._pr(n), e.get("focus")
        if pr is None or not focus:
            raise RuntimeError("review: needs a PR and a payload `focus`")
        head = pr["head"]["sha"]
        existing = [r for r in self._reviews(pr) if r["agent"] and r["focus"] == focus and r["commit_id"] == head]
        labels = self._labels(pr["number"])
        rereview = f"factory:rereview-{focus}" in labels
        if existing and (not rereview or any(r["rereview"] for r in existing)):
            return False
        facts = self.facts(n)
        earlier_change_heads = {r["commit_id"] for r in facts["reviews"]
                                if r["agent"] and r["verdict"] == "changes"
                                and r["commit_id"] != head
                                and (r.get("submitted_at") or "") >= self._since}
        rnd = e.get("round") or len(earlier_change_heads) + 1
        verdict = "approve" if e["value"].startswith("approve") else "changes"
        marker = f"<!-- factory:review focus={focus} round={rnd} verdict={verdict}{' rereview=1' if rereview else ''} -->"
        found = sorted(e.get("findings") or [], key=lambda x: x.get("severity") != "blocking")  # blocking first
        defang = lambda t: str(t).replace("<!--", "<!\u200b--")  # noqa: E731  # prose never carries a marker
        lines = [defang(f"**{x['id']}** {x['file']} — {x['finding']}") for x in found]
        summary = f"[factory] {defang(e['summary'])}" if e.get("summary") else "[factory]"
        body = "\n\n".join(x for x in (summary, "\n".join(lines), marker) if x)
        if e.get("run_id"):
            body += f"\n\nFactory-Run: {e['run_id']}"
        self.run(["api", "-X", "POST", f"repos/{self.repo}/pulls/{pr['number']}/reviews", "-f", f"commit_id={head}",
                  "-f", "event=COMMENT", "-f", f"body={body}{self._block(e)}"])
        if rereview:
            self._edit(pr["number"], remove=[f"factory:rereview-{focus}"], on="pr")
        return True

    def _create(self, n, children):
        """Per child: reuse an open issue of that title, then add each link that is missing (a retry converges)."""
        subs = {c["title"]: c for c in self._list(f"issues/{n}/sub_issues")}
        blockers = {b["id"] for b in self._list(f"issues/{n}/dependencies/blocked_by")}
        mark = f"<!-- factory:child of={n} -->"  # only an issue this ticket created is reused
        open_issues = {i["title"]: i for i in reversed(self._list("issues?state=open&per_page=100"))
                       if "pull_request" not in i and mark in (i.get("body") or "")}
        risk = [x for x in self._labels(n) if x.startswith("risk:")]  # a slice keeps its parent's risk path
        wrote, prev, prev_number = False, None, None  # prior slice blocks the next
        parent_body = self._api(f"issues/{n}").get("body") or ""
        for c in children:
            c = c if isinstance(c, dict) else {"title": str(c), "body": ""}
            new = open_issues.get(c["title"])
            if c["title"] in subs and new is None:  # not created by this ticket: leave it alone
                prev = subs[c["title"]].get("id")
                prev_number = subs[c["title"]].get("number")
                continue
            want = f"{c.get('body', '')}\n\n{mark}"
            if new is None:
                new = json.loads(self.run(["api", "-X", "POST", f"repos/{self.repo}/issues", "-f", f"title={c['title']}",
                                           "-f", f"body={want}",
                                           *[x for r in risk for x in ("-f", f"labels[]={r}")]]))  # no stage yet: not runnable
                chained, have = False, set()
            else:  # a reused child converges too: body, risk labels, chain
                path = f"repos/{self.repo}/issues/{new['number']}"
                if (new.get("body") or "") != want:
                    self.run(["api", "-X", "PATCH", path, "-f", f"body={want}"])
                    wrote = True
                have = {x["name"] for x in new.get("labels") or []}
                stale = [x for x in have if x.startswith("risk:") and x not in risk] if risk else []
                for x in stale:  # an opposing risk label would pick the wrong review path
                    self.run(["api", "-X", "DELETE", f"{path}/labels/{x}"])
                missing = [r for r in risk if r not in have]
                if missing:
                    self.run(["api", "-X", "POST", f"{path}/labels", *[x for r in risk for x in ("-f", f"labels[]={r}")]])
                wrote = wrote or bool(stale or missing)
                chained = bool(prev) and prev in {b["id"] for b in self._list(f"issues/{new['number']}/dependencies/blocked_by")}
            if prev and not chained:
                try:
                    self.run(["api", "-X", "POST", f"repos/{self.repo}/issues/{new['number']}/dependencies/blocked_by", "-F", f"issue_id={prev}"])
                except RuntimeError as e:
                    if "403" not in str(e) and "Resource not accessible" not in str(e):
                        raise
                    want = self._blocked_body(want, prev_number) if prev_number else want
                    self.run(["api", "-X", "PATCH", f"repos/{self.repo}/issues/{new['number']}", "-f", f"body={want}"])
                wrote = True
            if c["title"] not in subs:
                self.run(["api", "-X", "POST", f"repos/{self.repo}/issues/{n}/sub_issues", "-F", f"sub_issue_id={new['id']}"])
            if new["id"] not in blockers:
                try:
                    self.run(["api", "-X", "POST", f"repos/{self.repo}/issues/{n}/dependencies/blocked_by", "-F", f"issue_id={new['id']}"])
                except RuntimeError as e:
                    if "403" not in str(e) and "Resource not accessible" not in str(e):
                        raise
                    parent_body = self._blocked_body(parent_body, new["number"])
                    self.run(["api", "-X", "PATCH", f"repos/{self.repo}/issues/{n}", "-f",
                              f"body={parent_body}"])
                wrote = True
            if self.stages["ready"] not in have:  # publish `ready` only once the links exist
                for x in have:  # a reused draft child leaves its old stage
                    if x.startswith("factory:") and x != self.stages["ready"]:
                        self.run(["api", "-X", "DELETE", f"repos/{self.repo}/issues/{new['number']}/labels/{x}"])
                self.run(["api", "-X", "POST", f"repos/{self.repo}/issues/{new['number']}/labels", "-f", f"labels[]={self.stages['ready']}"])
                wrote = True
            wrote = wrote or c["title"] not in subs or new["id"] not in blockers
            prev = new["id"]
            prev_number = new["number"]
        return wrote

    def _park(self, n, check):
        """Open or reuse the one `Main is red in Air: <check>` ticket, then block ticket `n` by it."""
        title = f"Main is red in Air: {check}"
        red = next((i for i in self._list("issues?state=open&per_page=100") if "pull_request" not in i and i["title"] == title), None)
        if red is None:
            body = (f"[factory] The required check `{check}` fails identically on unchanged `main` in Air (found while working on #{n}). "
                    "Fix main; the tickets blocked by this one restart when it closes.")
            red = json.loads(self.run(["api", "-X", "POST", f"repos/{self.repo}/issues", "-f", f"title={title}", "-f", f"body={body}",
                                       "-f", f"labels[]={self.stages['triage']}"]))
        if red["id"] in {b["id"] for b in self._list(f"issues/{n}/dependencies/blocked_by")}:
            return False
        self.run(["api", "-X", "POST", f"repos/{self.repo}/issues/{n}/dependencies/blocked_by", "-F", f"issue_id={red['id']}"])
        return True

    @staticmethod
    def _blocked_body(body, number):
        line = f"Blocked by: #{number}"
        return body if re.search(rf"(?m)^{re.escape(line)}$", body) else f"{line}\n\n{body}"

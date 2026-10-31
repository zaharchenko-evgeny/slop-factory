"""The GitHub adapter behind the gate's Tracker port (factory/gate_port.py).

Facts are read from GitHub on every call and nothing is stored (ADR-0014): no `seen` list, the stage
label and the marked reviews at the head are the state. The PR is found from the branch `ez/<n>-*`.
A marker `<!-- factory:review focus=<f> round=<n> verdict=approve|changes -->` counts only on a review by `bot_login`
(agent reviews are COMMENT reviews: the author cannot approve its own PR; the verdict is in the marker)
(`targets.air.bot_login`; `gh api user` answers 403 inside an Air run, so it is never called).
"""
import json
import re
import sys
from pathlib import Path

import yaml

from github_tracker import ROOT, gh

MARKER = re.compile(r"<!-- factory:review focus=(\S+) round=(\d+)(?: verdict=(approve|changes))? -->")
FOCUSES = {"low": ["full"], "high": ["behaviour", "structure-security"]}
BAD_CI = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
GOOD_CI = {"success", "neutral", "skipped"}
WRITE = {"admin", "maintain", "write"}  # repository roles with write access


class GitHubGateTracker:
    def __init__(self, repo, stages, bot_login, risk_labels=None, fix_cap=3, run=gh, trusted_bots=()):
        self.trusted = set(trusted_bots)
        self.repo, self.stages, self.bot, self.run = repo, stages, bot_login, run
        self.risk_labels, self.fix_cap = risk_labels or {"low": "risk:low", "high": "risk:high"}, fix_cap

    @classmethod
    def from_config(cls, path=ROOT / "factory.yaml", run=gh):
        c = yaml.safe_load(Path(path).read_text())
        bot = ((c.get("targets") or {}).get("air") or {}).get("bot_login")
        if not bot:
            raise ValueError("factory.yaml: targets.air.bot_login is not set")
        t = c["tracker"]
        return cls(t["repository"], t["stages"], bot, t.get("risk_labels"), c["limits"]["fix_rounds"], run,
                   (c.get("reviews") or {}).get("trusted_bots") or [])

    # --- reads
    def _api(self, path, *flags):
        return json.loads(self.run(["api", f"repos/{self.repo}/{path}", *flags]))

    def _list(self, path):
        out = self.run(["api", "--paginate", f"repos/{self.repo}/{path}", "--jq", ".[]"])
        return [json.loads(x) for x in out.splitlines() if x.strip()]

    def _labels(self, n):
        return [x["name"] for x in self._api(f"issues/{n}")["labels"]]

    def sweep_tickets(self, stages):
        """Issue numbers (open or closed) whose stage label is one of `stages`; in-review and changes-requested only when open."""
        found = set()
        for st in stages:
            state = "open" if st in ("in-review", "changes-requested") else "all"
            found |= {i["number"] for i in self._list(f"issues?state={state}&labels={self.stages[st]}&per_page=100")
                      if "pull_request" not in i}
        return sorted(found)

    def _pr(self, n):
        """The ticket's PR from its branch: the open one, else the newest. None without a PR."""
        prs = [p for p in self._list("pulls?state=all&per_page=100") if p["head"]["ref"].startswith(f"ez/{n}-")
               and ((p["head"].get("repo") or {}).get("full_name") or "").lower() == self.repo.lower()]  # never a fork's PR
        prs.sort(key=lambda p: (p["state"] == "open", p["number"]))
        return self._api(f"pulls/{prs[-1]['number']}") if prs else None

    def _ci(self, sha):
        """CI at `sha` from the Actions workflow runs (the Air GitHub App has Actions: read, not Checks: read, so
        check-runs answers 403 in a run). Per workflow the latest run counts, so a rerun replaces a failure."""
        out = self.run(["api", "--paginate", f"repos/{self.repo}/actions/runs?head_sha={sha}&per_page=100",
                        "--jq", ".workflow_runs[]"])
        latest = {}
        for r in (json.loads(x) for x in out.splitlines() if x.strip()):
            key = (r.get("workflow_id"), r.get("event"))
            if key not in latest or (r.get("run_number"), r.get("run_attempt", 1)) > (
                    latest[key].get("run_number"), latest[key].get("run_attempt", 1)):
                latest[key] = r
        if not latest:
            return "none"
        done = [r.get("conclusion") if r.get("status") == "completed" else None for r in latest.values()]
        if any(c in BAD_CI for c in done):
            return "red"
        return "green" if all(c in GOOD_CI for c in done) else "pending"

    def _reviews(self, pr):
        out = []
        for r in self._list(f"pulls/{pr['number']}/reviews"):
            m = MARKER.search(r.get("body") or "")
            agent = bool(m) and r["user"]["login"] == self.bot
            verdict = (m[3] if agent else {"APPROVED": "approve", "CHANGES_REQUESTED": "changes"}.get(r["state"]))
            out.append({"author": r["user"]["login"], "state": r["state"], "commit_id": r["commit_id"],
                        "focus": m[1] if m else None, "round": int(m[2]) if m else None,
                        "verdict": verdict, "agent": agent, "assoc": r.get("author_association"), "submitted_at": r.get("submitted_at"),
                        "bot": r["user"].get("type") == "Bot" or r["user"]["login"].endswith("[bot]")})
        return out

    def _can_write(self, login):
        """Write access from the collaborator permission API (author_association says nothing about the role)."""
        try:
            return self._api(f"collaborators/{login}/permission").get("role_name") in WRITE
        except Exception as e:
            if "404" in str(e):  # a stranger is no collaborator
                return False
            raise  # any other failure must not drop a blocking review: the join fails and is retried

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
        complete = all(x in at_head for x in required) and not requested and ci in ("green", "red")
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
        stage = next((by_label[x] for x in labels if x in by_label), "none")
        blockers = [b for b in self._list(f"issues/{n}/dependencies/blocked_by") if b["state"] == "open"]
        risk = "low" if self.risk_labels["low"] in labels else "high"
        required, pr = FOCUSES[risk], self._pr(n)
        f = {"stage": stage, "attrs": [x for x in labels if x not in by_label], "blockers_open": len(blockers),
             "head": None, "pr": None, "mergeable": None, "ci": "none", "reviews": [], "requested_reviewers": [],
             "focus_missing": [], "round_complete": False, "round_verdict": None, "round_head": None,
             "ignored_reviews": [], "waiting_reviewers": [], "fix_rounds": 0,
             "fix_cap": self.fix_cap, "due": False, "seen": [], "pr_state": "none",
             "issue_state": issue.get("state", "open"),
             "title": issue.get("title", ""), "body": issue.get("body") or "", "branch": None}
        if stage == "split":
            kids = self._list(f"issues/{n}/sub_issues")
            f.update(children_total=len(kids), children_open=sum(k.get("state") == "open" for k in kids))
        if pr:
            head, reviews = pr["head"]["sha"], self._reviews(pr)
            latest = {r["focus"]: r for r in reviews if r["agent"]}  # the later review of a focus wins
            heads = {latest[x]["commit_id"] for x in required if x in latest}
            requested = [u["login"] for u in pr.get("requested_reviewers") or []]
            ci = self._ci(head)
            complete, verdict, ignored, waiting = self._round(reviews, required, head, requested, ci, pr["number"])
            f.update(branch=pr["head"]["ref"])
            f.update(pr_state="merged" if pr.get("merged") else "closed" if pr.get("state") == "closed" else "open")
            f.update(pr=pr["number"], head=head, mergeable=pr.get("mergeable"), ci=ci, reviews=reviews,
                     requested_reviewers=requested, round_verdict=verdict, ignored_reviews=ignored, waiting_reviewers=waiting,
                     focus_missing=[x for x in required if latest.get(x, {}).get("commit_id") != head],
                     round_complete=complete, round_head=heads.pop() if all(x in latest for x in required) and len(heads) == 1 else None,
                     fix_rounds=sum(1 for r in reviews if r["agent"] and r["verdict"] == "changes"))  # bot change requests
        return f

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
                pr = self._pr(n)
                if pr is None:
                    raise RuntimeError(f"{kind}: no PR for ticket {n}")
                target = pr["number"]
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
        if kind == "comment":
            body = value if value.startswith("[factory]") else f"[factory] {value}"
            if e.get("run_id") and "Factory-Run:" not in body:
                body += f"\n\nFactory-Run: {e['run_id']}"
            if any(c["body"] == body for c in self._list(f"issues/{n}/comments")):
                return False
            self.run(["issue", "comment", str(n), "-R", self.repo, "--body", body])
            return True
        if kind == "review":
            return self._review(n, e)
        if kind == "request-reviewers":
            return self._rerequest(n)
        if kind == "create-issues":
            return self._create(n, value)
        if kind == "merge":
            pr = self._pr(n)
            if pr is None:
                raise RuntimeError(f"merge: no PR for ticket {n}")
            if pr["merged"]:
                return False
            self.run(["pr", "merge", str(pr["number"]), "-R", self.repo, "--merge",
                      "--match-head-commit", pr["head"]["sha"]])
            return True
        raise ValueError(f"unknown effect: {kind}")

    def _join(self, n, tier):
        """Nothing while the round is open; else changes -> PR label fix-<tier> + stage changes-requested, approve -> approved."""
        f = self.facts(n)
        for who in f["ignored_reviews"]:
            print(f"join: ignored unmarked bot review by {who}", file=sys.stderr)
        if f["stage"] == "in-review" and f["waiting_reviewers"]:
            return self._rerequest(n, f["waiting_reviewers"])  # nothing moves until they decide again
        if not f["round_complete"] or f["stage"] != "in-review":
            return False
        if f["round_verdict"] == "approve":
            return self.apply({"ticket": n, "kind": "stage", "value": "approved"})
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

    def _review(self, n, e):
        pr, focus = self._pr(n), e.get("focus")
        if pr is None or not focus:
            raise RuntimeError("review: needs a PR and a payload `focus`")
        head = pr["head"]["sha"]
        if any(r["agent"] and r["focus"] == focus and r["commit_id"] == head for r in self._reviews(pr)):
            return False
        rnd = e.get("round") or self.facts(n)["fix_rounds"] + 1
        verdict = "approve" if e["value"].startswith("approve") else "changes"
        self.run(["api", "-X", "POST", f"repos/{self.repo}/pulls/{pr['number']}/reviews", "-f", f"commit_id={head}",
                  "-f", "event=COMMENT",
                  "-f", f"body=[factory] <!-- factory:review focus={focus} round={rnd} verdict={verdict} -->"])
        return True

    def _create(self, n, children):
        """Per child: reuse an open issue of that title, then add each link that is missing (a retry converges)."""
        subs = {c["title"] for c in self._list(f"issues/{n}/sub_issues")}
        blockers = {b["id"] for b in self._list(f"issues/{n}/dependencies/blocked_by")}
        mark = f"<!-- factory:child of={n} -->"  # only an issue this ticket created is reused
        open_issues = {i["title"]: i for i in reversed(self._list("issues?state=open&per_page=100"))
                       if "pull_request" not in i and mark in (i.get("body") or "")}
        wrote = False
        for c in children:
            c = c if isinstance(c, dict) else {"title": str(c), "body": ""}
            new = open_issues.get(c["title"])
            if c["title"] in subs and (new is None or new["id"] in blockers):
                continue
            if new is None:
                new = json.loads(self.run(["api", "-X", "POST", f"repos/{self.repo}/issues", "-f", f"title={c['title']}",
                                           "-f", f"body={c.get('body', '')}\n\n{mark}",
                                           "-f", f"labels[]={self.stages['draft']}"]))
            if c["title"] not in subs:
                self.run(["api", "-X", "POST", f"repos/{self.repo}/issues/{n}/sub_issues", "-F", f"sub_issue_id={new['id']}"])
            if new["id"] not in blockers:
                self.run(["api", "-X", "POST", f"repos/{self.repo}/issues/{n}/dependencies/blocked_by", "-F", f"issue_id={new['id']}"])
            wrote = True
        return wrote

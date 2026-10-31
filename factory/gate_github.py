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

    def _pr(self, n):
        """The ticket's PR from its branch: the open one, else the newest. None without a PR."""
        prs = [p for p in self._list("pulls?state=all&per_page=100") if p["head"]["ref"].startswith(f"ez/{n}-")
               and ((p["head"].get("repo") or {}).get("full_name") or "").lower() == self.repo.lower()]  # never a fork's PR
        prs.sort(key=lambda p: (p["state"] == "open", p["number"]))
        return self._api(f"pulls/{prs[-1]['number']}") if prs else None

    def _ci(self, sha):
        out = self.run(["api", "--paginate", f"repos/{self.repo}/commits/{sha}/check-runs", "--jq", ".check_runs[]"])
        runs = [json.loads(x) for x in out.splitlines() if x.strip()]
        done = [r["conclusion"] for r in runs]
        if not runs:
            return "none"
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

    def _start(self, head, at_head):
        """The round's start: the first agent review at head, else the head commit's date."""
        return min((r["submitted_at"] for r in at_head.values() if r["submitted_at"]), default=None) \
            or self._api(f"commits/{head}")["commit"]["committer"]["date"]

    def _round(self, reviews, required, head, requested, ci):
        """(complete, verdict, ignored) of the round at `head`: marked agent reviews of every required focus at
        head, no requested reviewer left (GitHub drops a person on review and adds them back on a re-request), CI
        concluded. A person (with write access) or trusted bot blocks with a latest CHANGES_REQUESTED on head or
        submitted since the round started; COMMENTED never counts."""
        at_head = {r["focus"]: r for r in reviews if r["agent"] and r["commit_id"] == head}
        others = {r["author"]: r for r in reviews if not r["agent"] and r["verdict"]
                  and (r["author"] in self.trusted if r["bot"] else self._can_write(r["author"]))}
        ignored = [r["author"] for r in reviews if r["bot"] and not r["agent"] and r["author"] not in self.trusted]
        complete = all(x in at_head for x in required) and not requested and ci in ("green", "red")
        if not complete:
            return False, None, ignored
        late = [r for r in others.values() if r["verdict"] == "changes"]
        if any(r["commit_id"] != head for r in late):  # a change request of an earlier round does not block this one
            start = self._start(head, at_head)
            late = [r for r in late if r["commit_id"] == head or (r["submitted_at"] or "") >= start]
        changes = ci == "red" or any(at_head[x]["verdict"] != "approve" for x in required) or bool(late)
        return True, "changes" if changes else "approve", ignored

    def facts(self, n):
        labels = self._labels(n)
        by_label = {v: k for k, v in self.stages.items()}
        stage = next((by_label[x] for x in labels if x in by_label), "none")
        blockers = [b for b in self._list(f"issues/{n}/dependencies/blocked_by") if b["state"] == "open"]
        risk = "low" if self.risk_labels["low"] in labels else "high"
        required, pr = FOCUSES[risk], self._pr(n)
        f = {"stage": stage, "attrs": [x for x in labels if x not in by_label], "blockers_open": len(blockers),
             "head": None, "pr": None, "mergeable": None, "ci": "none", "reviews": [], "requested_reviewers": [],
             "focus_missing": [], "round_complete": False, "round_verdict": None, "round_head": None,
             "ignored_reviews": [], "fix_rounds": 0,
             "fix_cap": self.fix_cap, "due": False, "seen": []}
        if pr:
            head, reviews = pr["head"]["sha"], self._reviews(pr)
            latest = {r["focus"]: r for r in reviews if r["agent"]}  # the later review of a focus wins
            heads = {latest[x]["commit_id"] for x in required if x in latest}
            requested = [u["login"] for u in pr.get("requested_reviewers") or []]
            ci = self._ci(head)
            complete, verdict, ignored = self._round(reviews, required, head, requested, ci)
            f.update(pr=pr["number"], head=head, mergeable=pr.get("mergeable"), ci=ci, reviews=reviews,
                     requested_reviewers=requested, round_verdict=verdict, ignored_reviews=ignored,
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
            return have != [want] and self._edit(n, [want] if want not in have else [], [x for x in have if x != want])
        if kind in ("label", "unlabel"):
            on, target = e.get("on") or "issue", n
            if on == "pr":
                pr = self._pr(n)
                if pr is None:
                    raise RuntimeError(f"{kind}: no PR for ticket {n}")
                target = pr["number"]
            elif on != "issue":
                raise ValueError(f"{kind}: bad target: {on}")
            has = value in self._labels(target)
            if kind == "label":
                return not has and self._edit(target, [value], on=on)
            return has and self._edit(target, remove=[value], on=on)
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
        if not f["round_complete"] or f["stage"] != "in-review":
            return False
        if f["round_verdict"] == "approve":
            return self.apply({"ticket": n, "kind": "stage", "value": "approved"})
        wrote = self.apply({"ticket": n, "kind": "label", "value": f"factory:fix-{tier}", "on": "pr"})
        return self.apply({"ticket": n, "kind": "stage", "value": "changes-requested"}) or wrote

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

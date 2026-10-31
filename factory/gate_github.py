"""The GitHub adapter behind the gate's Tracker port (factory/gate_port.py).

Facts are read from GitHub on every call and nothing is stored (ADR-0014): no `seen` list, the stage
label and the marked reviews at the head are the state. The PR is found from the branch `ez/<n>-*`.
A marker `<!-- factory:review focus=<f> round=<n> -->` counts only on a review by `bot_login`
(`targets.air.bot_login`; `gh api user` answers 403 inside an Air run, so it is never called).
"""
import json
import re
from pathlib import Path

import yaml

from github_tracker import ROOT, gh

MARKER = re.compile(r"<!-- factory:review focus=(\S+) round=(\d+) -->")
FOCUSES = {"low": ["full"], "high": ["behaviour", "structure-security"]}
BAD_CI = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
GOOD_CI = {"success", "neutral", "skipped"}


class GitHubGateTracker:
    def __init__(self, repo, stages, bot_login, risk_labels=None, fix_cap=3, run=gh):
        self.repo, self.stages, self.bot, self.run = repo, stages, bot_login, run
        self.risk_labels, self.fix_cap = risk_labels or {"low": "risk:low", "high": "risk:high"}, fix_cap

    @classmethod
    def from_config(cls, path=ROOT / "factory.yaml", run=gh):
        c = yaml.safe_load(Path(path).read_text())
        bot = ((c.get("targets") or {}).get("air") or {}).get("bot_login")
        if not bot:
            raise ValueError("factory.yaml: targets.air.bot_login is not set")
        t = c["tracker"]
        return cls(t["repository"], t["stages"], bot, t.get("risk_labels"), c["limits"]["fix_rounds"], run)

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
        prs = [p for p in self._list("pulls?state=all&per_page=100") if p["head"]["ref"].startswith(f"ez/{n}-")]
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
            out.append({"author": r["user"]["login"], "state": r["state"], "commit_id": r["commit_id"],
                        "focus": m[1] if m else None, "round": int(m[2]) if m else None,
                        "agent": bool(m) and r["user"]["login"] == self.bot})
        return out

    def facts(self, n):
        labels = self._labels(n)
        by_label = {v: k for k, v in self.stages.items()}
        stage = next((by_label[x] for x in labels if x in by_label), "none")
        blockers = [b for b in self._list(f"issues/{n}/dependencies/blocked_by") if b["state"] == "open"]
        risk = "low" if self.risk_labels["low"] in labels else "high"
        required, pr = FOCUSES[risk], self._pr(n)
        f = {"stage": stage, "attrs": [x for x in labels if x not in by_label], "blockers_open": len(blockers),
             "head": None, "pr": None, "mergeable": None, "ci": "none", "reviews": [], "requested_reviewers": [],
             "focus_missing": [], "round_complete": False, "round_head": None, "fix_rounds": 0,
             "fix_cap": self.fix_cap, "due": False, "seen": []}
        if pr:
            head, reviews = pr["head"]["sha"], self._reviews(pr)
            latest = {r["focus"]: r for r in reviews if r["agent"]}  # the later review of a focus wins
            heads = {latest[x]["commit_id"] for x in required if x in latest}
            complete = all(x in latest for x in required)
            f.update(pr=pr["number"], head=head, mergeable=pr.get("mergeable"), ci=self._ci(head), reviews=reviews,
                     requested_reviewers=[u["login"] for u in pr.get("requested_reviewers") or []],
                     focus_missing=[x for x in required if latest.get(x, {}).get("commit_id") != head],
                     round_complete=complete, round_head=heads.pop() if complete and len(heads) == 1 else None,
                     fix_rounds=max([r["round"] for r in latest.values()] + [1]) - 1)
        return f

    # --- effects: read the target first, write only when it differs
    def _edit(self, n, add=(), remove=()):
        flags = [x for a in add for x in ("--add-label", a)] + [x for r in remove for x in ("--remove-label", r)]
        self.run(["issue", "edit", str(n), "-R", self.repo, *flags])
        return True

    def apply(self, e):
        n, kind, value = e["ticket"], e["kind"], e["value"]
        if kind in ("seen", "join"):
            return False  # no hidden state: conditions catch a duplicate
        if kind == "stage":
            want, have = self.stages[value], [x for x in self._labels(n) if x in self.stages.values()]
            return have != [want] and self._edit(n, [want] if want not in have else [], [x for x in have if x != want])
        if kind == "label":
            return value not in self._labels(n) and self._edit(n, [value])
        if kind == "unlabel":
            return value in self._labels(n) and self._edit(n, remove=[value])
        if kind == "set-field":  # an attribute label `<field>:<value>`; one value per field
            (k, v), = value.items()
            have = [x for x in self._labels(n) if x.startswith(f"{k}:")]
            return have != [f"{k}:{v}"] and self._edit(n, [f"{k}:{v}"] if f"{k}:{v}" not in have else [],
                                                       [x for x in have if x != f"{k}:{v}"])
        if kind == "comment":
            body = value if value.startswith("[factory]") else f"[factory] {value}"
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

    def _review(self, n, e):
        pr, focus = self._pr(n), e.get("focus")
        if pr is None or not focus:
            raise RuntimeError("review: needs a PR and a payload `focus`")
        head = pr["head"]["sha"]
        if any(r["agent"] and r["focus"] == focus and r["commit_id"] == head for r in self._reviews(pr)):
            return False
        rnd = e.get("round") or self.facts(n)["fix_rounds"] + 1
        event = "APPROVE" if e["value"].startswith("approve") else "REQUEST_CHANGES"
        self.run(["api", "-X", "POST", f"repos/{self.repo}/pulls/{pr['number']}/reviews", "-f", f"commit_id={head}",
                  "-f", f"event={event}", "-f", f"body=[factory] <!-- factory:review focus={focus} round={rnd} -->"])
        return True

    def _create(self, n, children):
        have = {c["title"] for c in self._list(f"issues/{n}/sub_issues")}
        wrote = False
        for c in children:
            c = c if isinstance(c, dict) else {"title": str(c), "body": ""}
            if c["title"] in have:
                continue
            new = json.loads(self.run(["api", "-X", "POST", f"repos/{self.repo}/issues", "-f", f"title={c['title']}",
                                       "-f", f"body={c.get('body', '')}"]))
            for path in (f"issues/{n}/sub_issues", f"issues/{n}/dependencies/blocked_by"):
                self.run(["api", "-X", "POST", f"repos/{self.repo}/{path}", "-F", f"sub_issue_id={new['id']}"
                          if path.endswith("sub_issues") else f"issue_id={new['id']}"])
            wrote = True
        return wrote

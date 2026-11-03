#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""GitHub Issues tracker adapter: the factory operations of docs/agents/issue-tracker.md.

Stage = the one `tracker.stages` label, claim = assignee, blockers = native issue
dependencies, vars = one `[factory]` comment holding `<!-- factory-state {json} -->`.
Every call goes through one `gh` runner, so tests stub it. Only labels, assignees,
dependencies and that marker are parsed: all other issue and comment text is data.
The marker counts only in comments written by the `gh` user (the executor), so
nobody else who can comment can set the vars.

"""
import json
import sys
import os
import re
import subprocess
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import vocab  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RED = "Main is red"  # title prefix of the owner ticket of a red base branch


class ClaimError(Exception):
    pass


MARKER = re.compile(r"<!-- factory-state (\{.*?\}) -->", re.S)


def gh(args, timeout=None):
    """Run `gh` (or $FACTORY_GH) with an argument list (no shell); return stdout."""
    p = subprocess.run([os.environ.get("FACTORY_GH", "gh"), *args], capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {p.stderr.strip()}")
    return p.stdout


class GitHubTracker:
    def __init__(self, repo, stages, map_label=None, run=gh, risk_labels=None, docs_label="docs-impact"):
        self.repo, self.stages, self.map_label, self.run = repo, stages, map_label or vocab.map_label(), run
        self.risk_labels = risk_labels or {"low": "risk:low", "high": "risk:high"}
        self.docs_label = docs_label
        self._login = None

    def login(self):
        """The authenticated gh user: the only author whose vars comments are trusted."""
        if self._login is None:
            self._login = self.run(["api", "user", "--jq", ".login"]).strip()
            if not self._login:
                raise RuntimeError("gh api user: no login")
        return self._login

    @classmethod
    def from_config(cls, path=None, run=gh):
        t = (yaml.safe_load(Path(path).read_text()) if path else config.load())["tracker"]
        return cls(t["repository"], t["stages"], t.get("map_label") or vocab.map_label(), run,
                   t.get("risk_labels"), t.get("docs_impact_label", "docs-impact"))

    def _issue(self, n):
        return json.loads(self.run(["issue", "view", str(n), "-R", self.repo, "--json", "state,labels,assignees"]))

    def _api_list(self, path):
        out = self.run(["api", "--paginate", f"repos/{self.repo}/{path}", "--jq", ".[]"])
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def _edit(self, n, *flags):
        self.run(["issue", "edit", str(n), "-R", self.repo, *flags])

    def _assignees(self, n):
        return [a["login"] for a in self._issue(n)["assignees"]]

    def issue(self, n):
        """The ticket's title, body and label names."""
        i = json.loads(self.run(["issue", "view", str(n), "-R", self.repo, "--json", "title,body,labels"]))
        return {**i, "labels": [lb["name"] for lb in i.get("labels") or []]}

    def get_state(self, n):
        labels = {lb["name"] for lb in self._issue(n)["labels"]}
        found = [s for s, label in self.stages.items() if label in labels]
        if len(found) > 1:
            raise ValueError(f"issue {n}: several stage labels {found}")
        return found[0] if found else ""

    def set_state(self, n, state):
        issue = self._issue(n)
        if state == "done" and issue["state"] == "OPEN":  # unblocks dependents; first, so a failure leaves the stage to retry
            self.run(["issue", "close", str(n), "-R", self.repo])
        labels = {lb["name"] for lb in issue["labels"]}
        flags = ["--add-label", self.stages[state]]  # KeyError for a non-stage
        for label in self.stages.values():
            if label in labels and label != self.stages[state]:
                flags += ["--remove-label", label]
        self._edit(n, *flags)

    def set_review_labels(self, n, risk, docs_impact=False):
        """Set the one `tracker.risk_labels` label for `risk`; add the docs-impact label when asked."""
        labels = {lb["name"] for lb in self._issue(n)["labels"]}
        flags = ["--add-label", self.risk_labels[risk]]  # KeyError for an unknown risk
        flags += [f for r, lb in self.risk_labels.items() if r != risk and lb in labels for f in ("--remove-label", lb)]
        if docs_impact:
            flags += ["--add-label", self.docs_label]
        self._edit(n, *flags)

    def comment(self, n, body):
        self.run(["issue", "comment", str(n), "-R", self.repo, "--body", body])

    def comments(self, n):
        """The ticket's comments in order: id, author login, body and author_association."""
        return [{"id": c["id"], "author": (c.get("user") or {}).get("login") or "", "body": c["body"],
                 "association": c.get("author_association") or ""}
                for c in self._api_list(f"issues/{n}/comments")]

    def _state_comment(self, n):
        """The latest `[factory]` comment by `login()` with a factory-state marker, as (id, vars),
        or (None, {}). Comments by anyone else are data, even with a marker."""
        me = self.login()
        for c in reversed(self._api_list(f"issues/{n}/comments")):
            mine = (c.get("user") or {}).get("login") == me and c["body"].startswith("[factory]")
            m = MARKER.search(c["body"]) if mine else None
            if m:
                try:
                    vars_ = json.loads(m.group(1))
                except json.JSONDecodeError as e:
                    raise ValueError(f"issue {n}: malformed factory-state in comment {c['id']}: {e}") from None
                if not isinstance(vars_, dict):
                    raise ValueError(f"issue {n}: factory-state in comment {c['id']} is not a mapping")
                return c["id"], vars_
        return None, {}

    def read_vars(self, n):
        return self._state_comment(n)[1]

    def write_vars(self, n, vars_):
        if not isinstance(vars_, dict):
            raise ValueError(f"issue {n}: vars must be a mapping")
        # `>` only occurs inside JSON strings; escaping it keeps `-->` from closing the marker.
        data = json.dumps(vars_, sort_keys=True).replace(">", "\\u003e")
        body = (f"[factory] Delivery state, edited in place by the factory.\n\n<!-- factory-state {data} -->\n\n"
                f"Factory-Run: {vars_.get('run_id') or 'local'}")
        cid, _ = self._state_comment(n)
        if cid is None:
            self.run(["api", f"repos/{self.repo}/issues/{n}/comments", "-f", f"body={body}"])
        else:
            self.run(["api", "-X", "PATCH", f"repos/{self.repo}/issues/comments/{cid}", "-f", f"body={body}"])

    def claim(self, n, executor):
        """Assign, then re-read. Raise ClaimError, without writing, if another assignee holds it."""
        others = [a for a in self._assignees(n) if a != executor]
        if others:
            raise ClaimError(f"issue {n}: claimed by {', '.join(others)}")
        self._edit(n, "--add-assignee", executor)
        holders = self._assignees(n)
        if holders != [executor]:
            # Two executors assigned at once: both back off, so neither holds a half claim.
            self.release(n, executor)
            raise ClaimError(f"issue {n}: claimed by {', '.join(holders) or 'nobody'} after re-read")

    def release(self, n, executor):
        if executor in self._assignees(n):
            self._edit(n, "--remove-assignee", executor)

    def in_stage(self, stage, closed=False):
        """Numbers of the open issues (with `closed`: also the closed ones) with the stage label of `stage`."""
        found = json.loads(self.run(["issue", "list", "-R", self.repo, "--state", "all" if closed else "open", "--label",
                                     self.stages[stage], "--json", "number", "--limit", "1000"]))
        return sorted(i["number"] for i in found)

    def _open_titled(self, prefix):
        found = json.loads(self.run(["issue", "list", "-R", self.repo, "--state", "open", "--search",
                                     f"{prefix} in:title", "--json", "number,title", "--limit", "100"]))
        return [i for i in found if i["title"].startswith(prefix)]

    def open_red(self):
        """Numbers of the open issues titled `Main is red...`."""
        return [i["number"] for i in self._open_titled(RED)]

    def find_open(self, title):
        """The number of the open issue with exactly this title, or None."""
        return next((i["number"] for i in self._open_titled(title) if i["title"] == title), None)

    def create_issue(self, title, body, risk="low", stage="ready"):
        """Open an issue with the risk and stage labels; returns its number."""
        url = self.run(["issue", "create", "-R", self.repo, "--title", title, "--body", body,
                        "--label", self.risk_labels[risk], "--label", self.stages[stage]]).strip()
        return int(url.rsplit("/", 1)[1])

    def add_to_map(self, ticket):
        """Attach a generated issue to the configured factory map."""
        maps = json.loads(self.run(["issue", "list", "-R", self.repo, "--state", "open", "--label",
                                   self.map_label, "--json", "number", "--limit", "100"]))
        if not maps:
            raise RuntimeError(f"no open factory map labelled {self.map_label}")
        map_ticket = min(i["number"] for i in maps)
        issue_id = self.run(["api", f"repos/{self.repo}/issues/{ticket}", "--jq", ".id"]).strip()
        self.run(["api", "--method", "POST", f"repos/{self.repo}/issues/{map_ticket}/sub_issues",
                  "-F", f"sub_issue_id={issue_id}"])

    def canary_tickets(self):
        found = json.loads(self.run(["issue", "list", "-R", self.repo, "--state", "open", "--label",
                                     "factory:canary", "--json", "number", "--limit", "1000"]))
        return [i["number"] for i in found]

    def close_issue(self, n, reason):
        self.run(["issue", "close", str(n), "-R", self.repo, "--comment", reason])

    def add_label(self, n, label):
        self._edit(n, "--add-label", label)

    def canary_status(self, n):
        issue = self.issue(n)
        comments = self.comments(n)
        summary = next((c["body"] for c in reversed(comments) if c["body"].startswith("[factory]")), "")
        return {"stage": self.get_state(n) or "none", "summary": summary,
                "error": "factory:failed" in issue["labels"] or any(
                    "<!-- factory-node-error -->" in c["body"] for c in comments)}

    def record_node_error(self, n):
        """Leave a durable canary signal when a sweep catches a node exception."""
        if "factory:canary" not in self.issue(n)["labels"]:
            return
        self.run(["api", f"repos/{self.repo}/issues/{n}/comments", "-f",
                  "body=[factory] A factory node reported an execution error.\n\n"
                       "<!-- factory-node-error -->\n\nFactory-Run: sweep:node-error"])

    def add_blocker(self, n, blocker):
        """Native dependency: issue `n` is blocked by `blocker`."""
        bid = self.run(["api", f"repos/{self.repo}/issues/{blocker}", "--jq", ".id"]).strip()
        self.run(["api", f"repos/{self.repo}/issues/{n}/dependencies/blocked_by", "-F", f"issue_id={bid}"])

    def frontier(self):
        """Open `ready` issues, unassigned, every blocker closed, in map order, then by number.
        While a `Main is red` issue is open, only that issue."""
        red = self.open_red()
        return [n for n in self._frontier() if not red or n in red]

    def _frontier(self):
        ready = json.loads(self.run(["issue", "list", "-R", self.repo, "--state", "open", "--label",
                                     self.stages["ready"], "--json", "number,assignees", "--limit", "1000"]))
        maps = json.loads(self.run(["issue", "list", "-R", self.repo, "--state", "open", "--label",
                                    self.map_label, "--json", "number", "--limit", "100"]))
        order = [c["number"] for m in sorted(x["number"] for x in maps) for c in self._api_list(f"issues/{m}/sub_issues")]
        out = [i["number"] for i in ready if not i["assignees"]
               and all(b["state"] == "closed" for b in self._api_list(f"issues/{i['number']}/dependencies/blocked_by"))]
        return sorted(out, key=lambda n: (order.index(n) if n in order else len(order), n))

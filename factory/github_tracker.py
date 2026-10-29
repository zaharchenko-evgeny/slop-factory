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

CLI: uv run factory/github_tracker.py get-state 23 | set-state 23 in-review | read-vars 23
     | write-vars 23 '<json>' | claim 23 <login> | release 23 <login> | frontier
"""
import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

from local_tracker import ClaimError

ROOT = Path(__file__).resolve().parent.parent
MARKER = re.compile(r"<!-- factory-state (\{.*?\}) -->", re.S)


def gh(args):
    """Run `gh` (or $FACTORY_GH) with an argument list (no shell); return stdout."""
    p = subprocess.run([os.environ.get("FACTORY_GH", "gh"), *args], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(f"gh {' '.join(args[:3])}: {p.stderr.strip()}")
    return p.stdout


class GitHubTracker:
    def __init__(self, repo, stages, map_label="factory:map", run=gh, risk_labels=None, docs_label="docs-impact"):
        self.repo, self.stages, self.map_label, self.run = repo, stages, map_label, run
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
    def from_config(cls, path=ROOT / "factory.yaml", run=gh):
        t = yaml.safe_load(Path(path).read_text())["tracker"]
        return cls(t["repository"], t["stages"], t.get("map_label", "factory:map"), run,
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
        labels = {lb["name"] for lb in self._issue(n)["labels"]}
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

    def frontier(self):
        """Open `ready` issues, unassigned, every blocker closed, in map order, then by number."""
        ready = json.loads(self.run(["issue", "list", "-R", self.repo, "--state", "open", "--label",
                                     self.stages["ready"], "--json", "number,assignees", "--limit", "1000"]))
        maps = json.loads(self.run(["issue", "list", "-R", self.repo, "--state", "open", "--label",
                                    self.map_label, "--json", "number", "--limit", "100"]))
        order = [c["number"] for m in sorted(x["number"] for x in maps) for c in self._api_list(f"issues/{m}/sub_issues")]
        out = [i["number"] for i in ready if not i["assignees"]
               and all(b["state"] == "closed" for b in self._api_list(f"issues/{i['number']}/dependencies/blocked_by"))]
        return sorted(out, key=lambda n: (order.index(n) if n in order else len(order), n))


def main(argv=None):
    p = argparse.ArgumentParser(prog="github_tracker.py", description=__doc__.splitlines()[0])
    p.add_argument("--config", default=ROOT / "factory.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, extra in [("get-state", []), ("set-state", ["state"]), ("read-vars", []),
                        ("write-vars", ["json"]), ("claim", ["executor"]), ("release", ["executor"])]:
        sp = sub.add_parser(name)
        for a in ["issue", *extra]:
            sp.add_argument(a)
    sub.add_parser("frontier")
    args = p.parse_args(argv)
    tr = GitHubTracker.from_config(args.config)
    try:
        if args.cmd == "get-state":
            print(tr.get_state(args.issue))
        elif args.cmd == "set-state":
            tr.set_state(args.issue, args.state)
        elif args.cmd == "read-vars":
            print(json.dumps(tr.read_vars(args.issue), indent=2, sort_keys=True))
        elif args.cmd == "write-vars":
            tr.write_vars(args.issue, json.loads(args.json))
        elif args.cmd == "claim":
            tr.claim(args.issue, args.executor)
        elif args.cmd == "release":
            tr.release(args.issue, args.executor)
        else:
            print(" ".join(map(str, tr.frontier())))
    except (ClaimError, ValueError, KeyError, RuntimeError) as e:
        print(f"github_tracker.py: error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

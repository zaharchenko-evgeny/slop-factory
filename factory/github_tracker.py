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
import random
import re
import subprocess
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import vocab  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RED = "Main is red"  # title prefix of the owner ticket of a red base branch


def random_color():
    """A random GitHub label color: the color of a label that has no mapped one."""
    return f"{random.randrange(0x1000000):06x}"


class ClaimError(Exception):
    pass


MARKER = re.compile(r"<!-- factory-state (\{.*?\}) -->", re.S)


class RateLimited(RuntimeError):
    kind = "rate-limited"  # `reconcile` re-fires a step that failed this way (#577)


RATE_LIMIT = re.compile(r"rate limit|x-ratelimit-remaining:\s*0", re.I)
RESET = re.compile(r"x-ratelimit-reset:\s*(\d+)", re.I)
MAX_WAIT, DEFAULT_WAIT = 300, 60  # seconds: the longest wait for a reset, and the wait when none is given


def gh(args, timeout=None, token=None):
    """Run `gh` (or $FACTORY_GH) with an argument list (no shell); return stdout. On a rate-limit answer
    (HTTP 403 or 429), wait until `x-ratelimit-reset` (at most MAX_WAIT), retry once, then raise RateLimited.
    `token`: GH_TOKEN of this call only."""
    env = {**os.environ, "GH_TOKEN": token} if token else None
    for attempt in (1, 2):
        p = subprocess.run([os.environ.get("FACTORY_GH", "gh"), *args], capture_output=True, text=True, timeout=timeout,
                           env=env)
        if not p.returncode:
            return p.stdout
        text = f"{p.stderr}\n{p.stdout}"
        msg = f"gh {' '.join(args[:3])}: {p.stderr.strip()}"
        if not (re.search(r"HTTP (403|429)", text) and RATE_LIMIT.search(text)):
            raise RuntimeError(msg)
        if attempt == 2:
            raise RateLimited(msg)
        m = RESET.search(text)
        time.sleep(min(max(int(m[1]) - time.time(), 0), MAX_WAIT) if m else DEFAULT_WAIT)


class CountedRun:
    """Wrap a `gh` runner: count the calls that reach it and answer a repeated read of one issue's record,
    timeline, labels or blockers from memory. `cache` holds REST bodies a tracker prefetched with GraphQL
    (`GitHubGateTracker.prefetch`). Any write may change them, so it drops both."""
    # Issue reads; a commit or a comparison by SHA (fixed); the PR list (changed only by a write, which drops the memo).
    # Not `pulls/{n}`: a merge re-reads it to check the head.
    READ = re.compile(r"repos/[^/]+/[^/]+/(issues/\d+(/timeline|/events|/labels|/dependencies/blocked_by)?"
                      r"|commits/[0-9a-f]{40}|compare/[^/?]+\.\.\.[0-9a-f]{40}|pulls\?state=all&per_page=100)")

    def __init__(self, run):
        self.run, self.calls, self.memo, self.cache = run, 0, {}, {}

    def __call__(self, args, **kw):
        read = args[:2] in (["issue", "view"], ["issue", "list"], ["pr", "view"], ["pr", "list"], ["run", "list"]) or (
            args[0] == "api" and not {"-X", "--method", "-f", "-F"} & set(args)) or (
            args[:2] == ["api", "graphql"] and not any(str(a).startswith("query=mutation") for a in args))
        path = next((x for x in args if x.startswith("repos/")), "")
        key = tuple(args) if read and self.READ.fullmatch(path) else None
        if not read:
            self.memo.clear()
            self.cache.clear()
        elif key in self.memo:
            return self.memo[key]
        self.calls += 1
        out = self.run(args, **kw)
        if key:
            self.memo[key] = out
        return out


# `write_mode: token`: Air's GitHub token writes only the factory repository, so writes to a `role: project`
# repository use this secret (deploy.py adds it to WRITE_TOKEN_ENVS). It is read from the environment, never printed.
WRITE_TOKEN = "FACTORY_WRITE_TOKEN"
WRITE_TOKEN_ENVS = ("write", "ops")


def token_env(env, config):
    """Whether `env` writes project repositories with WRITE_TOKEN: it declares the secret (a definition team), or it
    is one of WRITE_TOKEN_ENVS (the old layout)."""
    declared = ((config.get("environments") or {}).get(env) or {}).get("secrets") or []
    return WRITE_TOKEN in declared or env in WRITE_TOKEN_ENVS
WRITE_VERBS = {"create", "edit", "merge", "review", "comment", "close", "reopen", "delete", "ready", "update-branch"}


def project_repositories(config):
    """owner/repo of each `role: project` repository of the descriptor: the CI label sync's repositories."""
    return ["/".join(r["url"].rstrip("/").split("/")[-2:]) for r in config.get("repositories") or []
            if r.get("role") == "project"]


def gh_target(args):
    """owner/repo a `gh` call names with `-R`/`--repo` or a `repos/<owner>/<repo>/` path, else None."""
    for flag, value in zip(args, args[1:]):
        if flag in ("-R", "--repo"):
            return value
    path = next((str(x) for x in args if str(x).startswith("repos/")), "")
    return "/".join(path.split("?")[0].split("/")[1:3]) or None


def gh_writes(args):
    """True when a `gh` call writes: `gh api` with a method other than GET or a field (gh then POSTs), or a
    WRITE_VERBS subcommand such as `pr edit` or `label create`."""
    if args[0] == "api":
        method = next((v for f, v in zip(args, args[1:]) if f in ("-X", "--method")), None)
        return method.upper() != "GET" if method else bool({"-f", "-F", "--field", "--raw-field"} & set(args))
    return len(args) > 1 and args[1] in WRITE_VERBS


class ProjectWriteRun:
    """Wrap a `gh` runner: a write to one of `repositories` runs with $FACTORY_WRITE_TOKEN as its GH_TOKEN, when
    that is set; every other call, and every call without it, runs as before."""

    def __init__(self, run, repositories, environ=os.environ):
        self.run, self.repositories, self.environ = run, {r.lower() for r in repositories}, environ

    def __call__(self, args, **kw):
        token = self.environ.get(WRITE_TOKEN)
        if token and gh_writes(args) and (gh_target(args) or "").lower() in self.repositories:
            kw["token"] = token
        return self.run(args, **kw)


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

    def list_labels(self, repo=None):
        """{name: {"color", "description"}} of the repository's labels (default: the tracker's)."""
        out = self.run(["label", "list", "-R", repo or self.repo, "--limit", "1000", "--json", "name,color,description"])
        return {x["name"]: {"color": x["color"].lower(), "description": x.get("description") or ""} for x in json.loads(out)}

    def create_label(self, name, color, description="", repo=None):
        self.run(["label", "create", name, "-R", repo or self.repo, "--color", color, "--description", description])

    def update_label(self, name, color, description="", repo=None):
        self.run(["label", "edit", name, "-R", repo or self.repo, "--color", color, "--description", description])

    def delete_label(self, name, owned_prefixes, repo=None):
        """Delete a label only inside the factory's namespaces; anything else is someone else's and never removed."""
        if not name.startswith(tuple(owned_prefixes)):
            raise ValueError(f"label {name!r} is not factory-owned")
        self.run(["label", "delete", name, "-R", repo or self.repo, "--yes"])

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

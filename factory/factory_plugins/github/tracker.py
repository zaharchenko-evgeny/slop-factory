"""GitHub Issues tracker adapter: the factory operations of docs/agents/issue-tracker.md.

Stage = the one `tracker.stages` label, claim = assignee, blockers = native issue
dependencies, vars = one `[factory]` comment holding `<!-- factory-state {json} -->`.
Every call goes through one `gh` runner, so tests stub it. Only labels, assignees,
dependencies and that marker are parsed: all other issue and comment text is data.
The marker counts only in comments written by the `gh` user (the executor), so
nobody else who can comment can set the vars.

"""
import copy
import json
import os
import random
import re
import subprocess
import time
from pathlib import Path

import yaml

import config
import vocab
from gate_port import in_ticket_repository, ticket_ref, tracker_repositories

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


def gh(args, timeout=None, token=None, cwd=None):
    """Run `gh` (or $FACTORY_GH) with an argument list (no shell); return stdout. On a rate-limit answer
    (HTTP 403 or 429), wait until `x-ratelimit-reset` (at most MAX_WAIT), retry once, then raise RateLimited.
    `token`: GH_TOKEN of this call only. `cwd`: the directory it runs in (Air's token follows the checkout, #1246)."""
    env = {**os.environ, "GH_TOKEN": token} if token else None
    for attempt in (1, 2):
        p = subprocess.run([os.environ.get("FACTORY_GH", "gh"), *args], capture_output=True, text=True, timeout=timeout,
                           env=env, cwd=cwd)
        if not p.returncode:
            return p.stdout
        text = f"{p.stderr}\n{p.stdout}"
        target = next((str(x) for x in args if str(x).startswith("repos/")), None) or gh_target(args) or ""
        msg = f"gh {' '.join(str(x) for x in args[:3])} {target}".rstrip() + f": {p.stderr.strip()}"  # which call failed
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

    def for_repository(self, repo):
        """The wrapped runner's `for_repository` (ProjectWriteRun: a link to another repository's issue takes the
        issues token), counted like any call; this runner itself when it has none."""
        inner = getattr(self.run, "for_repository", None)
        if inner is None:
            return self
        forward = inner(repo)

        def run(args, **kw):
            self.calls += 1
            self.memo.clear()
            self.cache.clear()
            return forward(args, **kw)
        return run

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


# Air's GitHub token writes only the automation's own repository, so writes to a `role: project` repository use this
# secret when a person sets it (ADR-0024: deploy no longer adds it). It is read from the environment, never printed.
WRITE_TOKEN = "FACTORY_WRITE_TOKEN"
WRITE_TOKEN_ENVS = ("write", "ops")
ENV_REPOSITORY = "FACTORY_ENV_REPOSITORY"  # owner/repo of the environment's Air repository, set by deploy (#1126)
WORKSPACES = "FACTORY_WORKSPACES"  # where Air checks out a run's repositories (default /workspaces)


def token_env(env, config):
    """Whether `env` writes project repositories with WRITE_TOKEN: it declares the secret (a definition team), or it
    is one of WRITE_TOKEN_ENVS (the old layout)."""
    declared = ((config.get("environments") or {}).get(env) or {}).get("secrets") or []
    return WRITE_TOKEN in declared or env in WRITE_TOKEN_ENVS
WRITE_VERBS = {"create", "edit", "merge", "review", "comment", "close", "reopen", "delete", "ready", "update-branch"}


def repository_tokens(config, environ=os.environ):
    """{owner/repo (lower case): secret name} of the repositories' `token` (ADR-0027) in the run's environment
    (FACTORY_ENV); every environment's when none is set (deploy, doctor)."""
    envs = config.get("environments") or {}
    names = [environ.get("FACTORY_ENV")] if environ.get("FACTORY_ENV") else list(envs)
    urls = {r.get("name"): r.get("url") for r in config.get("repositories") or [] if isinstance(r, dict)}
    out = {}
    for env in names:
        for name, secret in ((envs.get(env) or {}).get("tokens") or {}).items():
            repo = "/".join(str(urls.get(name, name)).rstrip("/").removesuffix(".git").split("/")[-2:]).lower()
            out.setdefault(repo, secret)
    return out


def checked_out(config, environ=os.environ):
    """owner/repo of the repositories of the run's environment (FACTORY_ENV): each copy checks them all out (its main
    and its additional repositories, ADR-0027). Air's token writes a checkout from that checkout's directory (#1246)."""
    env = ((config.get("environments") or {}).get(environ.get("FACTORY_ENV") or "") or {})
    urls = {r.get("name"): r.get("url") for r in config.get("repositories") or [] if isinstance(r, dict)}
    out = []
    for r in env.get("repositories") or []:
        url = urls.get(r, r) if isinstance(r, str) else (r or {}).get("repo", "")
        out.append("/".join(str(url).rstrip("/").removesuffix(".git").split("/")[-2:]))
    return [x for x in out if x]


def project_repositories(config):
    """owner/repo of each `role: project` repository of the descriptor: the CI label sync's repositories."""
    return ["/".join(r["url"].rstrip("/").split("/")[-2:]) for r in config.get("repositories") or []
            if r.get("role") == "project"]


def foreign_projects(repositories, environ=os.environ):
    """`repositories` without the environment's own repository ($FACTORY_ENV_REPOSITORY): Air's GH_TOKEN writes it."""
    own = (environ.get(ENV_REPOSITORY) or "").lower()
    return [r for r in repositories if r.lower() != own]


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
    """Wrap a `gh` runner: a write to one of `repositories` runs with that repository's `token` (the secret
    `tokens` names for it) as its GH_TOKEN, else with $FACTORY_WRITE_TOKEN, when one is set; every other call,
    every call without a token and every write to the environment's own repository runs as before."""

    def __init__(self, run, repositories, environ=os.environ, tokens=None, checkouts=(), issues_token_env=None):
        """`tokens`: {owner/repo: secret name}, as `repository_tokens` reads it. `checkouts`: the run's checkouts; a
        call to one without a declared token runs from its directory, where Air's token writes it (#1246).
        `issues_token_env`: deprecated (#1236), the token of a repository that is not checked out and of a link."""
        self.run, self.environ, self.issues_token_env = run, environ, issues_token_env
        self.tokens = {r.lower(): secret for r, secret in (tokens or {}).items()}
        self.checkouts = {c.lower(): c for c in checkouts}
        # a repository with a declared token is written with it whatever its role (a foreign primary included)
        self.repositories = {r.lower() for r in foreign_projects([*repositories, *self.tokens], environ)}

    def token(self, repo):
        """The token for a write to `repo`: its declared one; else $FACTORY_WRITE_TOKEN when `repo` is not checked
        out here; None otherwise."""
        repo = repo.lower()
        declared = self.environ.get(self.tokens.get(repo, ""))
        return declared or (None if repo in self.checkouts else self.fallback()) or None

    def fallback(self):
        """The deprecated issues token, else $FACTORY_WRITE_TOKEN; None when neither is set."""
        return (self.issues_token_env and self.environ.get(self.issues_token_env)) or self.environ.get(WRITE_TOKEN) or None

    def checkout_dir(self, repo):
        """The directory of the run's checkout of `repo`, or None when it is not checked out here."""
        name = self.checkouts.get((repo or "").lower())
        path = Path(self.environ.get(WORKSPACES) or "/workspaces") / name.rsplit("/", 1)[-1] if name else None
        return str(path) if path and path.is_dir() else None

    def __call__(self, args, touches=None, **kw):
        target = (gh_target(args) or "").lower()
        if gh_writes(args):  # a link to another repository's issue: a token that reads both, from either side
            token = next((t for repo in (target, (touches or "").lower())
                          if repo in self.repositories and (t := self.token(repo))), None)
            if not token and touches and touches.lower() != target:  # no checkout's own token reads both issues
                token = self.fallback()
            if token:
                kw["token"] = token
                return self.run(args, **kw)
        if (where := self.checkout_dir(target)):
            kw["cwd"] = where
        return self.run(args, **kw)

    def for_repository(self, repo):
        """This runner for a write to the own repository that refers to an issue of `repo` (a sub-issue or blocker
        link): Air's token cannot read another repository's issue, so a foreign `repo` takes the issues token too."""
        return lambda args, **kw: self(args, touches=repo, **kw)


class GitHubTracker:
    def __init__(self, repo, stages, map_label=None, run=gh, risk_labels=None, docs_label="docs-impact",
                 ticket_repos=None):
        self.repo, self.stages, self.map_label, self.run = repo, stages, map_label or vocab.map_label(), run
        # ADR-0024: the repositories that hold tickets, the tracker first; a bare ticket number is `repo`'s issue
        self.primary, self.ticket_repos = repo, list(ticket_repos or [repo])
        self.risk_labels = risk_labels or {"low": "risk:low", "high": "risk:high"}
        self.docs_label = docs_label
        self._login = None

    def at(self, repo):
        """This tracker on ticket repository `repo`: its bare numbers are that repository's issues."""
        view = copy.copy(self)
        view.repo = repo
        return view

    def ref(self, repo, number):
        """The id of issue `number` of `repo`: bare in the tracker, `owner/repo#n` elsewhere."""
        return ticket_ref(repo, number, self.primary)

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
        repos = tracker_repositories(t)
        return cls(t.get("repository") or repos[0], t["stages"], t.get("map_label") or vocab.map_label(), run,
                   t.get("risk_labels"), t.get("docs_impact_label", "docs-impact"), repos)

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

    @in_ticket_repository
    def issue(self, n):
        """The ticket's title, body and label names."""
        i = json.loads(self.run(["issue", "view", str(n), "-R", self.repo, "--json", "title,body,labels"]))
        return {**i, "labels": [lb["name"] for lb in i.get("labels") or []]}

    @in_ticket_repository
    def get_state(self, n):
        labels = {lb["name"] for lb in self._issue(n)["labels"]}
        found = [s for s, label in self.stages.items() if label in labels]
        if len(found) > 1:
            raise ValueError(f"issue {n}: several stage labels {found}")
        return found[0] if found else ""

    @in_ticket_repository
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

    @in_ticket_repository
    def set_review_labels(self, n, risk, docs_impact=False):
        """Set the one `tracker.risk_labels` label for `risk`; add the docs-impact label when asked."""
        labels = {lb["name"] for lb in self._issue(n)["labels"]}
        flags = ["--add-label", self.risk_labels[risk]]  # KeyError for an unknown risk
        flags += [f for r, lb in self.risk_labels.items() if r != risk and lb in labels for f in ("--remove-label", lb)]
        if docs_impact:
            flags += ["--add-label", self.docs_label]
        self._edit(n, *flags)

    @in_ticket_repository
    def comment(self, n, body):
        self.run(["issue", "comment", str(n), "-R", self.repo, "--body", body])

    @in_ticket_repository
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

    @in_ticket_repository
    def read_vars(self, n):
        return self._state_comment(n)[1]

    @in_ticket_repository
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

    @in_ticket_repository
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

    @in_ticket_repository
    def release(self, n, executor):
        if executor in self._assignees(n):
            self._edit(n, "--remove-assignee", executor)

    def in_stage(self, stage, closed=False):
        """Ids of the open issues (with `closed`: also the closed ones) with the stage label of `stage`, in every
        ticket repository: the tracker's numbers sorted, then each other repository's `owner/repo#n`."""
        out = []
        for repo in self.ticket_repos:
            found = json.loads(self.run(["issue", "list", "-R", repo, "--state", "all" if closed else "open", "--label",
                                         self.stages[stage], "--json", "number", "--limit", "1000"]))
            out += [self.ref(repo, n) for n in sorted(i["number"] for i in found)]
        return out

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

    @in_ticket_repository
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
        maps = json.loads(self.run(["issue", "list", "-R", self.repo, "--state", "open", "--label",
                                    self.map_label, "--json", "number", "--limit", "100"]))
        order = [self.ref(self.repo, c["number"]) for m in sorted(x["number"] for x in maps)
                 for c in self._api_list(f"issues/{m}/sub_issues")]
        out = [n for repo in self.ticket_repos for n in self.at(repo)._ready()]
        return sorted(out, key=lambda n: order.index(n) if n in order else len(order))

    def _ready(self):
        """Ids of this repository's open `ready` issues that are unassigned with every blocker closed, by number."""
        ready = json.loads(self.run(["issue", "list", "-R", self.repo, "--state", "open", "--label",
                                     self.stages["ready"], "--json", "number,assignees", "--limit", "1000"]))
        return [self.ref(self.repo, i["number"]) for i in sorted(ready, key=lambda i: i["number"]) if not i["assignees"]
                and all(b["state"] == "closed" for b in self._api_list(f"issues/{i['number']}/dependencies/blocked_by"))]

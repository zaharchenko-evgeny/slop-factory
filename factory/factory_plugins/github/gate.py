"""The GitHub adapter behind the gate's Tracker port (factory/gate_port.py).

Facts are read from GitHub on every call and nothing is stored (ADR-0014): no `seen` list, the stage
label and the marked reviews at the head are the state. The PR is found from the branch `ez/<n>-*`.
A marker `<!-- factory:review focus=<f> round=<n> verdict=approve|changes -->` counts only on a review by `bot_login`
(agent reviews are COMMENT reviews: the author cannot approve its own PR; the verdict is in the marker)
(`targets.air.bot_login`; `gh api user` answers 403 inside an Air run, so it is never called).
"""
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

import config
from gate_tracker import GateTracker
from gate_port import (DescriptorPort, child_attrs, child_links, child_order, in_ticket_repository, number_of, ticket_view,
                       ticket_from_ref, ticket_ref, ticket_repositories, tracker_repositories)
from scopes import matches
from factory_plugins.github import graphql
from factory_plugins.github.tracker import CountedRun, ProjectWriteRun, gh, project_repositories, random_color
import derive
import effects as effects_mod
from derive import clean_notes, notes_block, parse_notes, redact  # noqa: F401  (the agent notes, ADR-0026)
from ports.records import Lazy, PrRecord, TicketRecord
from ports.system import Effect, Event, Query, Subject
import vocab

COMPARE_FILE_LIMIT = 300  # GitHub compare lists at most this many files
NOTES_PARKED = "[factory] Agent notes for the PR description"  # ticket comment that holds them until the PR exists


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
        # Air's checkout is shallow: a commit before the merge may be missing, so fetch it first.
        if subprocess.run(["git", "cat-file", "-e", f"{revision}^{{commit}}"], capture_output=True).returncode:
            subprocess.run(["git", "fetch", "--quiet", "--depth", "1", "origin", revision], check=True,
                           capture_output=True, text=True)
        subprocess.run(["git", "worktree", "add", "--detach", "--quiet", path, revision], check=True,
                       capture_output=True, text=True)
        try:
            output = subprocess.run(["uv", "run", "--quiet", "factory/deploy.py", "plan", "--target", "air"],
                                    cwd=path, check=True, capture_output=True, text=True).stdout
            return json.loads(output)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", path], check=True,
                           capture_output=True, text=True)

MARKER = re.compile(r"<!-- factory:review focus=(\S+) round=(\d+)(?: verdict=(approve|changes))?(?: rereview=(1))?(?: outcome=([\w-]+))? -->")
FOCUSES = {"low": ["full"], "high": ["behaviour", "structure-security"]}
BAD_CI = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}
GOOD_CI = {"success", "neutral", "skipped"}
WRITE = {"admin", "maintain", "write"}  # repository roles with write access


def repository_names(c):
    """{name: owner/repo} of the descriptor's GitHub repositories, the primary first; None without any."""
    repos = sorted(c.get("repositories") or [], key=lambda r: r.get("role") != "primary")
    return {r["name"]: "/".join(r["url"].rstrip("/").split("/")[-2:]) for r in repos} or None


class GitHubCodeHost:
    """The GitHub repository that hosts the pull requests; `code_host.repository`, else the tracker's."""

    def __init__(self, repo, run=gh, ticket_repos=None):
        self.repo, self.run = repo, run
        self.ticket_repos = list(ticket_repos or [])  # ADR-0024: the repositories that hold tickets, the tracker first

    @classmethod
    def from_config(cls, path=None, run=gh, descriptor=None):
        c = yaml.safe_load(Path(path).read_text()) if path else descriptor if descriptor is not None else config.load()
        tickets = tracker_repositories(c["tracker"])
        return cls((c.get("code_host") or {}).get("repository") or c["tracker"].get("repository") or tickets[0],
                   ProjectWriteRun(run, project_repositories(c), issues_token_env=c.get("issues_token_env")), tickets)

    @staticmethod
    def gate_tracker(c, tickets, run=gh):
        """The gate tracker of a team whose tickets live in another system (`tickets`, a TicketStore built by that
        system's plugin) and whose pull requests live here, in `code_host.repository`; `c` is the loaded descriptor."""
        return GitHubGateTracker.from_config(run=run, tickets=tickets, descriptor=c)

    def ticket_ref(self, repository, number):
        """The id of issue `number` of `repository` (ADR-0024): bare in the tracker, else `owner/repo#n`."""
        return ticket_ref(repository, number, self.ticket_repos[0] if self.ticket_repos else None)

    def ticket_for_pr(self, number, repository=None):
        """Resolve a PR number to its ticket from the live head ref, else the live title; `repository` (owner/repo)
        when the PR is in another of the ticket's repositories. The ticket is in the PR's own repository when that
        holds tickets, else (a repository with no tickets) in the tracker."""
        repository = repository or self.repo
        pr = json.loads(self.run(["api", f"repos/{repository}/pulls/{number}"]))
        ticket = ticket_from_ref((pr.get("head") or {}).get("ref")) or ticket_from_ref(pr.get("title"))
        holds = repository.lower() in {r.lower() for r in self.ticket_repos}
        return self.ticket_ref(repository, ticket) if holds and isinstance(ticket, int) else ticket

    def event(self, payload):
        """The Event of a pull request or PR comment payload, the ticket from the PR's live head ref (the Vcs port);
        None when it names no PR or the PR is on no factory branch."""
        pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else payload.get("issue")
        number = pr.get("number") if isinstance(pr, dict) else None
        if not isinstance(number, int) or number < 1:
            return None
        repository = (payload.get("repository") or {}).get("full_name") if isinstance(payload.get("repository"), dict) else None
        ticket = self.ticket_for_pr(number, repository) if repository else self.ticket_for_pr(number)
        return None if ticket is None else Event(ticket, "pull_request", payload.get("action"), number, repository, payload)

    # --- the CI label sync (gate.ci_sync); `repository` is owner/repo
    def _list(self, path):
        return [json.loads(x) for x in self.run(["api", "--paginate", path, "--jq", ".[]"]).splitlines() if x.strip()]

    def factory_prs(self, repository):
        """[{number, head, labels}] of the open PRs from a factory branch `ez/<ticket>-*` of `repository`, never a fork's."""
        return [{"number": p["number"], "head": p["head"]["sha"], "labels": [x["name"] for x in p.get("labels") or []]}
                for p in self._list(f"repos/{repository}/pulls?state=open&per_page=100")
                if ticket_from_ref(p["head"]["ref"]) is not None
                and ((p["head"].get("repo") or {}).get("full_name") or "").lower() == repository.lower()]

    def head_checks(self, repository, sha):
        """[{conclusion, at}] of the latest check runs and commit statuses at `sha`; conclusion None while one runs."""
        runs = [json.loads(x) for x in self.run(["api", "--paginate", f"repos/{repository}/commits/{sha}/check-runs?per_page=100",
                                                 "--jq", ".check_runs[]"]).splitlines() if x.strip()]
        statuses = json.loads(self.run(["api", f"repos/{repository}/commits/{sha}/status"])).get("statuses") or []
        return ([{"conclusion": r.get("conclusion") if r.get("status") == "completed" else None, "at": r.get("completed_at")}
                 for r in runs]
                + [{"conclusion": None if s["state"] == "pending" else "success" if s["state"] == "success" else "failure",
                    "at": s.get("updated_at")} for s in statuses])

    def label_added_at(self, repository, number, label):
        """When `label` was last added to PR `number`, else None."""
        return max((e["at"] for e in normalise_events(self._list(f"repos/{repository}/issues/{number}/events?per_page=100"))
                    if e["kind"] == "labeled" and e["value"] == label), default=None)

    def edit_pr_labels(self, repository, number, add=(), remove=()):
        """Remove, then add, in two writes: a label removed and added in one edit fires no `labeled` event."""
        for flag, labels in (("--remove-label", remove), ("--add-label", add)):
            if labels:
                self.run(["pr", "edit", str(number), "-R", repository, *[x for label in labels for x in (flag, label)]])


def normalise_events(raw):
    """GitHub issue events as `{kind, value, actor, at}`: `kind` is the event name (`labeled`, `unlabeled`,
    `closed`, `reopened`, or another the tracker only uses for timing), `value` the label name."""
    return [{"kind": e.get("event"), "value": (e.get("label") or {}).get("name"),
             "actor": (e.get("actor") or {}).get("login"), "at": e.get("created_at")} for e in raw]


class GitHubTicketStore:
    """The ticket side of the GitHub tracker (the TicketStore port, factory/gate_port.py): the issues of `repo`, read
    and written with `gh`. `run` is the tracker's `CountedRun`, whose cache `GitHubGateTracker.prefetch` fills.
    `scope` (`{label}`, #1069) is an environment's share of the repository's issues: its searches hold only the issues
    with the label, and an issue it creates gets it."""

    def __init__(self, repo, run=gh, bot=None, scope=None, repositories=None, ticket_repos=None):
        self.repo, self.run, self.bot = repo, run, bot  # `bot`: the factory's login, `targets.air.bot_login`
        self.scope = scope
        self.others = dict(repositories or {})  # {name: owner/repo} of the definition's other repositories
        # ADR-0024: the repositories that hold tickets, the first is the tracker; a bare number is a ticket of `repo`
        self.primary = repo
        self.ticket_repos = list(ticket_repos or [repo])

    def at(self, repo):
        """This store on ticket repository `repo`: its bare numbers are that repository's issues."""
        view = copy.copy(self)
        view.repo = repo
        return view

    def ref(self, repo, number):
        """The id of issue `number` of `repo`: bare in the tracker, `owner/repo#n` elsewhere."""
        return ticket_ref(repo, number, self.primary)

    def _id(self, item):
        """The id of an issue body of the REST API: its own repository (`repository_url`), else this one."""
        url = item.get("repository_url") or ""
        return self.ref(url.split("/repos/", 1)[1] if "/repos/" in url else self.repo, item["number"])

    def child_labels(self, child):
        """The labels a created child carries besides its risk: `child_attrs` (ADR-0024 `repo:` and `subsystem:`), or
        none under ADR-0027, where the child's place says its repository: the repository whose issues it is, or its
        tracker's scope field (the environment defines it once; a label per repository would repeat it)."""
        return [] if getattr(self, "one_repository", False) else child_attrs(child)

    def repo_of(self, child):
        """The repository a child is created in: the one its `repo` names (a name or `owner/repo`) among the other
        repositories, else this one."""
        want = str(child.get("repo") or "")
        return self.others.get(want) or next((r for r in self.others.values() if r and r.lower() == want.lower()), None) \
            or self.repo

    def scope_labels(self):
        return [self.scope["label"]] if self.scope else []

    @in_ticket_repository
    def in_scope(self, n):
        """Whether issue `n` carries the scope's label (every issue without a scope)."""
        return not self.scope or matches(self.scope, self.issue(n)["attrs"])

    # --- reads
    def _cached(self, path, repo):
        """A body `prefetch` read, until the first write (`CountedRun` drops them)."""
        cache = getattr(self.run, "cache", {})
        return copy.deepcopy(cache[path]) if (repo or self.repo) == self.primary and path in cache else None

    def api(self, path, *flags, repo=None):
        if not flags and (hit := self._cached(path, repo)) is not None:
            return hit
        return json.loads(self.run(["api", f"repos/{repo or self.repo}/{path}", *flags]))

    def read_list(self, path, repo=None):
        if (hit := self._cached(path, repo)) is not None:
            return hit
        out = self.run(["api", "--paginate", f"repos/{repo or self.repo}/{path}", "--jq", ".[]"])
        return [json.loads(x) for x in out.splitlines() if x.strip()]

    @in_ticket_repository
    def issue(self, n):
        i = self.api(f"issues/{n}")
        return {"title": i.get("title", ""), "body": i.get("body") or "", "state": i.get("state", "open"),
                "close_reason": i.get("state_reason"), "author": (i.get("user") or {}).get("login"),
                "updated_at": i.get("updated_at"), "attrs": [x["name"] for x in i["labels"]],
                "assignees": [a["login"] for a in i.get("assignees", [])]}

    @in_ticket_repository
    def events(self, n):
        return normalise_events(self.read_list(f"issues/{n}/events"))

    @in_ticket_repository
    def comments(self, n):
        out = []
        for c in self.read_list(f"issues/{n}/comments"):
            login, user = (c.get("user") or {}).get("login", ""), c.get("user") or {}
            out.append({"id": c.get("id"), "author": login,
                        "author_is_bot": user.get("type") == "Bot" or login.endswith("[bot]"),
                        "author_is_factory": bool(self.bot) and login == self.bot, "body": c["body"], "at": c.get("created_at") or ""})
        return out

    @in_ticket_repository
    def open_blockers(self, n):
        """Numbers of the open issues blocking `n`: native links, then `Blocked by: #<n>` body lines."""
        blockers = [self._id(b) for b in self.read_list(f"issues/{n}/dependencies/blocked_by") if b["state"] == "open"]
        for repo, number in re.findall(r"(?im)^Blocked by: (?:([\w.-]+/[\w.-]+))?#(\d+)\s*$", self.issue(n)["body"]):
            if self.api(f"issues/{number}", repo=repo or None).get("state") == "open":
                blockers.append(self.ref(repo or self.repo, int(number)))
        return blockers

    @in_ticket_repository
    def parent(self, n):
        try:
            parent = self.api(f"issues/{n}/parent")
            return self._id(parent) if parent.get("number") else None
        except Exception as e:
            if "404" in str(e):
                return None
            raise

    @in_ticket_repository
    def children(self, n):
        return [{"number": self._id(k), "state": k.get("state"),
                 "not_planned": k.get("state") == "closed" and k.get("state_reason") == "not_planned",
                 "attrs": [x["name"] for x in k.get("labels") or []]} for k in self.read_list(f"issues/{n}/sub_issues")]

    @in_ticket_repository
    def blocked_by(self, n):
        """Every native blocker of `n`, open or closed (`[{number, state}]`)."""
        return [{"number": self._id(b), "state": b["state"]} for b in self.read_list(f"issues/{n}/dependencies/blocked_by")]

    @in_ticket_repository
    def blocking(self, n, repo=None):
        """Issues whose native or body fallback dependency list names `n` as a blocker: `n` of `repo`; with no `repo`,
        of this repository or, in a body line, of another repository of the definition (the gate knows tickets by
        number, and a woken ticket re-checks its open blockers)."""
        other = repo if repo and repo != self.repo else None
        dependents = [{**i, "number": self._id(i)} for i in self.read_list(f"issues/{n}/dependencies/blocking", repo=other)]
        known = {number_of(i["number"]) for i in dependents}
        refs = [other] if other else [""] + [r for r in self.others.values() if r and r != self.repo] if repo is None else [""]
        line = "(?:" + "|".join(f"{re.escape(r)}#{n}" for r in refs) + ")"
        for issue in self.read_list("issues?state=open&per_page=100"):
            if issue["number"] != n and issue["number"] not in known and re.search(
                    rf"(?im)^Blocked by: {line}\s*$", issue.get("body") or ""):
                dependents.append({**issue, "number": self.ref(self.repo, issue["number"])})
                known.add(issue["number"])
        return dependents

    def tickets_by_stage(self, attrs, open_only=False, now=None):
        """Issue numbers carrying one of the stage `attrs`: the open ones and, unless `open_only`, those closed in the
        last `gate_port.CLOSED_HOURS`, from one paged GraphQL search each."""
        found = []  # the tracker's tickets, then each other ticket repository's: bare numbers, then `owner/repo#n`
        scoped = {"scope_label": self.scope["label"]} if self.scope else {}
        for repo in self.ticket_repos:
            numbers = set()
            for q in graphql.search_query(repo, attrs, now, **scoped)[:1 if open_only else 2] if attrs else []:
                after = None
                while True:
                    args = ["api", "graphql", "-f", f"query={graphql.SEARCH}", "-f", f"q={q}"]
                    page = json.loads(self.run(args + (["-f", f"after={after}"] if after else [])))["data"]["search"]
                    numbers |= {x["number"] for x in page["nodes"] if x}
                    if not page["pageInfo"]["hasNextPage"]:
                        break
                    after = page["pageInfo"]["endCursor"]
            found += [self.ref(repo, n) for n in sorted(numbers)]
        return found

    def can_write(self, login, repo=None):
        """Write access from the collaborator permission API (author_association says nothing about the role)."""
        try:
            return self.api(f"collaborators/{login}/permission", repo=repo).get("role_name") in WRITE
        except Exception as e:
            if "404" in str(e):  # a stranger is no collaborator
                return False
            raise  # any other failure must not drop a blocking review: the join fails and is retried

    def can_answer(self, login):
        return self.can_write(login)

    # --- writes
    @in_ticket_repository
    def edit_attrs(self, n, add=(), remove=()):
        flags = [x for a in add for x in ("--add-label", a)] + [x for r in remove for x in ("--remove-label", r)]
        self.run(["issue", "edit", str(n), "-R", self.repo, *flags])
        return True

    @in_ticket_repository
    def add_attr(self, n, attr):
        return self.edit_attrs(n, [attr])

    @in_ticket_repository
    def remove_attr(self, n, *attrs):
        return self.edit_attrs(n, remove=attrs)

    @in_ticket_repository
    def set_stage(self, n, stage, have):
        """Stage `stage` replaces the stage attrs `have` the ticket holds."""
        return self.edit_attrs(n, [stage] if stage not in have else [], [x for x in have if x != stage])

    @in_ticket_repository
    def assign(self, n, people):
        """The ticket's assignees become exactly `people`; True when that wrote."""
        current, desired = set(self.issue(n)["assignees"]), set(people)
        if current == desired:
            return False
        flags = [x for a in sorted(desired - current) for x in ("--add-assignee", a)]
        flags += [x for a in sorted(current - desired) for x in ("--remove-assignee", a)]
        self.run(["issue", "edit", str(n), "-R", self.repo, *flags])
        return True

    @in_ticket_repository
    def comment(self, n, body):
        self.run(["issue", "comment", str(n), "-R", self.repo, "--body", body])

    @in_ticket_repository
    def close(self, n):
        self.run(["issue", "close", str(n), "-R", self.repo])

    def create_children(self, n, children, ready):
        """Per child: reuse an open issue of that title, then add each link that is missing (a retry converges)."""
        subs = {c["title"]: c for c in self.read_list(f"issues/{n}/sub_issues")}
        blockers = {b["id"] for b in self.read_list(f"issues/{n}/dependencies/blocked_by")}
        mark = f"<!-- factory:child of={n} -->"  # only an issue this ticket created is reused
        listed = {}  # repository -> {title: its open child of this ticket}

        def open_issues(repo):
            if repo not in listed:
                listed[repo] = {i["title"]: i for i in reversed(self.read_list("issues?state=open&per_page=100", repo=repo))
                                if "pull_request" not in i and mark in (i.get("body") or "")}
            return listed[repo]
        # a slice keeps its parent's risk path, and its environment's scope label
        risk = [x for x in self.issue(n)["attrs"] if x.startswith("risk:")] + self.scope_labels()
        wrote, made, links = False, {}, child_links(children)  # made: child index -> (id, number); prerequisites go first
        parent_body = self.issue(n)["body"]
        for k in child_order(links):
            c, after = children[k], links[k]
            c = c if isinstance(c, dict) else {"title": str(c), "body": ""}
            repo = self.repo_of(c)
            new = open_issues(repo).get(c["title"])
            linked = subs.get(c["title"])
            if linked and not str(linked.get("repository_url") or f"/{repo}").endswith(f"/{repo}"):
                linked = None  # a same-titled child of another repository is not this one
            if linked and new is None:  # not created by this ticket: leave it alone
                made[k] = (linked.get("id"), linked.get("number"), repo)
                continue
            prereqs = [made[j] for j in after if j in made]
            want = f"{c.get('body', '')}\n\n{mark}"
            if new is None:
                new = json.loads(self.run(["api", "-X", "POST", f"repos/{repo}/issues", "-f", f"title={c['title']}",
                                           "-f", f"body={want}",
                                           *[x for r in [*risk, *self.child_labels(c)] for x in ("-f", f"labels[]={r}")]]))  # no stage yet: not runnable
                chained, have = set(), set()
            else:  # a reused child converges too: body, risk labels, chain
                path = f"repos/{repo}/issues/{new['number']}"
                if (new.get("body") or "") != want:
                    self.run(["api", "-X", "PATCH", path, "-f", f"body={want}"])
                    wrote = True
                have = {x["name"] for x in new.get("labels") or []}
                stale = [x for x in have if x.startswith("risk:") and x not in risk] if risk else []
                for x in stale:  # an opposing risk label would pick the wrong review path
                    self.run(["api", "-X", "DELETE", f"{path}/labels/{x}"])
                missing = [r for r in [*risk, *self.child_labels(c)] if r not in have]
                if missing:
                    self.run(["api", "-X", "POST", f"{path}/labels", *[x for r in [*risk, *self.child_labels(c)] for x in ("-f", f"labels[]={r}")]])
                wrote = wrote or bool(stale or missing)
                chained = {b["id"] for b in self.read_list(f"issues/{new['number']}/dependencies/blocked_by", repo=repo)} if prereqs else set()
            for prev, prev_number, prev_repo in prereqs:
                if prev in chained:
                    continue
                try:
                    self.run(["api", "-X", "POST", f"repos/{repo}/issues/{new['number']}/dependencies/blocked_by", "-F", f"issue_id={prev}"])
                except RuntimeError as e:
                    if "403" not in str(e) and "Resource not accessible" not in str(e):
                        raise
                    want = self._blocked_body(want, prev_number, prev_repo, repo) if prev_number else want
                    self.run(["api", "-X", "PATCH", f"repos/{repo}/issues/{new['number']}", "-f", f"body={want}"])
                wrote = True
            link = self.run.for_repository(repo) if repo != self.repo and hasattr(self.run, "for_repository") else self.run
            if not linked:  # a child of another repository: the issues token links it (#1155: Air's token cannot)
                link(["api", "-X", "POST", f"repos/{self.repo}/issues/{n}/sub_issues", "-F", f"sub_issue_id={new['id']}"])
            if new["id"] not in blockers:
                try:
                    link(["api", "-X", "POST", f"repos/{self.repo}/issues/{n}/dependencies/blocked_by", "-F", f"issue_id={new['id']}"])
                except RuntimeError as e:
                    if "403" not in str(e) and "Resource not accessible" not in str(e):
                        raise
                    parent_body = self._blocked_body(parent_body, new["number"], repo, self.repo)
                    self.run(["api", "-X", "PATCH", f"repos/{self.repo}/issues/{n}", "-f",
                              f"body={parent_body}"])
                wrote = True
            if ready not in have:  # publish `ready` only once the links exist
                for x in have:  # a reused draft child leaves its old stage
                    if x.startswith("factory:") and x != ready:
                        self.run(["api", "-X", "DELETE", f"repos/{repo}/issues/{new['number']}/labels/{x}"])
                self.run(["api", "-X", "POST", f"repos/{repo}/issues/{new['number']}/labels", "-f", f"labels[]={ready}"])
                wrote = True
            wrote = wrote or not linked or new["id"] not in blockers
            made[k] = (new["id"], new["number"], repo)
        return wrote

    def park(self, n, check, triage):
        """Open or reuse the one `Main is red in Air: <check>` ticket, then block ticket `n` by it."""
        title = f"Main is red in Air: {check}"
        red = next((i for i in self.read_list("issues?state=open&per_page=100") if "pull_request" not in i and i["title"] == title
                    and all(x in [y["name"] for y in i.get("labels") or []] for x in self.scope_labels())), None)
        if red is None:
            body = (f"[factory] The required check `{check}` fails identically on unchanged `main` in Air (found while working on #{n}). "
                    "Fix main; the tickets blocked by this one restart when it closes.")
            red = json.loads(self.run(["api", "-X", "POST", f"repos/{self.repo}/issues", "-f", f"title={title}", "-f", f"body={body}",
                                       *[x for label in [triage, *self.scope_labels()] for x in ("-f", f"labels[]={label}")]]))
        if red["id"] in {b["id"] for b in self.read_list(f"issues/{n}/dependencies/blocked_by")}:
            return False
        self.run(["api", "-X", "POST", f"repos/{self.repo}/issues/{n}/dependencies/blocked_by", "-F", f"issue_id={red['id']}"])
        return True

    @staticmethod
    def _blocked_body(body, number, blocker_repo=None, repo=None):
        """`body` with a `Blocked by:` line for issue `number`; `owner/repo#n` when it is in another repository."""
        line = f"Blocked by: {blocker_repo}#{number}" if blocker_repo != repo else f"Blocked by: #{number}"
        return body if re.search(rf"(?m)^{re.escape(line)}$", body) else f"{line}\n\n{body}"


class GitHubGateTracker(DescriptorPort):
    def __init__(self, repo, stages, bot_login, risk_labels=None, fix_cap=3, run=gh, trusted_bots=(), run_cap=8,
                 ci_workflows=("quality-gates",), pr_repo=None, project=None, repositories=None, tickets=None,
                 required=None, ticket_repos=None):
        self.trusted, self.run_cap, self.ci_workflows = set(trusted_bots), run_cap, set(ci_workflows)
        self.required = {**FOCUSES, **(required or {})}  # review focuses by risk; `reviews.required` overrides
        import definition
        if definition.present(config.team_root()):  # ADR-0021: no focuses; workflow.yaml names the reviews
            self.required = {risk: [] for risk in self.required}
        self.project = project  # factory.project.yaml: the stores of states and commands (DescriptorPort)
        self.pr_repo = pr_repo or repo  # the code host: pull requests, CI, commits, merges
        # {name: owner/repo} of the descriptor's repositories, the primary first; the primary is the code host
        self.repositories = dict(repositories or {"primary": None})
        self.repositories[next(iter(self.repositories))] = self.pr_repo
        self.writable = None  # repository names this environment may write; None: every one (#994)
        self.repo, self.stages, self.bot, self.run = repo, stages, bot_login, CountedRun(run)
        # the ticket side: every ticket read and write goes through it (GitHub issues unless a store is given)
        self.ticket_repos = list(ticket_repos or [repo])  # ADR-0024: the repositories that hold tickets, the tracker first
        self.tickets = tickets or GitHubTicketStore(repo, self.run, bot_login, ticket_repos=self.ticket_repos,
                                                    repositories=dict(list(self.repositories.items())[1:]))
        self.github = self.tickets if tickets is None else GitHubTicketStore(repo, self.run, bot_login)  # the code host's REST reads
        if tickets:  # another ticket system: its store kinds, and the ticket effects it declares replace these; `repo` is the code host
            self.state_kinds = tuple(getattr(tickets, "state_kinds", self.state_kinds))
            self.command_kinds = (*getattr(tickets, "command_kinds", ("label",)), "pr_label")
            self.effects = (*type(self).effects, *getattr(tickets, "effects", ()))  # a later (kind, target) wins
        self.risk_labels, self.fix_cap = risk_labels or {"low": "risk:low", "high": "risk:high"}, fix_cap

    @classmethod
    def from_config(cls, path=None, run=gh, tickets=None, descriptor=None):
        """From the descriptor at `path` (else `descriptor`, else the team's). With `tickets` (another system's
        TicketStore) the tickets live there and the pull requests in `code_host.repository`. An environment's
        descriptor (#1069) has its merged tracker block as `tracker`, whose `scope` narrows the issues."""
        c = descriptor if descriptor is not None else yaml.safe_load(Path(path).read_text()) if path else config.load()
        bot = ((c.get("targets") or {}).get("air") or {}).get("bot_login")
        if not bot:
            raise ValueError("descriptor: targets.air.bot_login is not set")
        t, code_repo = c["tracker"], (c.get("code_host") or {}).get("repository")
        repo = code_repo if tickets is not None else t.get("repository") or tracker_repositories(t)[0]
        return cls(repo, t["stages"], bot, t.get("risk_labels"), c["limits"]["fix_rounds"],
                   ProjectWriteRun(run, project_repositories(c), issues_token_env=c.get("issues_token_env")),
                   (c.get("reviews") or {}).get("trusted_bots") or [], c["limits"].get("agent_runs_per_ticket", 8),
                   (c.get("ci") or {}).get("workflows") or ["quality-gates"],
                   (c.get("code_host") or {}).get("repository"),
                   {k: c[k] for k in ("states", "commands") if k in c} if "states" in c else None,
                   repository_names(c), tickets, (c.get("reviews") or {}).get("required"),
                   tracker_repositories(t) if tickets is None else None)._with_scope(t)._with_writable(c)._with_one(c)

    @classmethod
    def for_repository(cls, repo, run=gh):
        """The reads of repository `repo` alone, without a descriptor: the local surface's poll of one repository."""
        return cls(repo, {}, None, run=run)

    def _with_scope(self, t):
        """The scope of tracker block `t` on the GitHub issues (a store of another system takes its own)."""
        if t.get("scope") and isinstance(self.tickets, GitHubTicketStore):
            self.tickets.scope = t["scope"]
        return self

    @property
    def scope(self):
        """The environment's share of the tickets (ports/system.py Subject.scope); None is all of them."""
        return getattr(self.tickets, "scope", None)

    def in_scope(self, n):
        """Whether ticket `n` is in this tracker's scope."""
        return self.tickets.in_scope(n) if hasattr(self.tickets, "in_scope") else True

    def _with_one(self, c):
        """ADR-0027: a ticket changes the one repository it lives in, so its body's `repositories:` line is ignored."""
        self.one_repository = bool(c.get("one_repository_per_ticket"))
        self.tickets.one_repository = self.one_repository  # its created children carry no `repo:` label
        return self

    def _with_writable(self, c):
        """The writable repositories of the environment in FACTORY_ENV, from the compiled definition (#994)."""
        env = ((c.get("environments") or {}).get(os.environ.get("FACTORY_ENV") or "") or {})
        if "writable" in env:
            self.writable = set(env["writable"]) | {next(iter(self.repositories))}
        return self

    # --- reads
    def _api(self, path, *flags, repo=None):
        return self.github.api(path, *flags, repo=repo)

    def _list(self, path, repo=None):
        return self.github.read_list(path, repo=repo)

    @in_ticket_repository
    def pr_comments(self, n):
        """The comments on ticket `n`'s PR, as `tickets.comments` gives them ({author, author_is_bot, author_is_factory, body, at})."""
        pr = self._pr(n)
        if not pr:
            return []
        return [{"id": c.get("id"), "author": (c.get("user") or {}).get("login"), "author_is_bot": (c.get("user") or {}).get("type") == "Bot",
                 "author_is_factory": bool(self.bot) and (c.get("user") or {}).get("login") == self.bot,
                 "body": c.get("body") or "", "at": c.get("created_at")}
                for c in self._list(f"issues/{pr['number']}/comments", repo=self.pr_repo)]

    def _labels(self, number, repo=None):
        """The labels of ticket `number`, or of the pull request `number` in `repo`."""
        if repo is None:
            return self.tickets.issue(number)["attrs"]
        return [x["name"] for x in self._api(f"issues/{number}", repo=repo)["labels"]]

    def _pr_events(self, pr):
        return normalise_events(self._list(f"issues/{pr}/events", repo=self.pr_repo))

    def main_green(self):
        """The latest completed quality-gates run on main succeeded and tested the current main head."""
        runs = json.loads(self.run(["run", "list", "-R", self.repo, "--workflow", "quality-gates.yml", "--branch", "main",
                                    "--event", "push", "--status", "completed", "--limit", "1", "--json", "conclusion,headSha"]))
        return (bool(runs) and runs[0]["conclusion"] == "success"
                and runs[0].get("headSha") == self._api("commits/main")["sha"])

    def sweep_tickets(self, stages, open_only=False, now=None):
        """Issue numbers whose stage label is one of `stages`: the open ones and, unless `open_only`, those closed in the
        last `gate_port.CLOSED_HOURS`, from one paged GraphQL search each."""
        return self.tickets.tickets_by_stage([self.stages[st] for st in stages if st in self.stages], open_only, now)

    def prefetch(self, numbers):
        """Read tickets `numbers` in batched GraphQL queries into the cache `_api` and `_list` serve. A failed batch
        caches nothing: the gate then reads those tickets over REST."""
        owner, name = self.repo.split("/")
        numbers = sorted({n for n in numbers if isinstance(n, int)})  # the tracker's tickets: the cache holds its bodies
        for i in range(0, len(numbers), graphql.BATCH):
            batch = numbers[i:i + graphql.BATCH]
            try:
                data = json.loads(self.run(["api", "graphql", "-f", f"query={graphql.batch_query(owner, name, batch)}"]))
                found = {}
                for node in data["data"]["repository"].values():
                    if node:
                        found.update(graphql.to_rest(node, list(self.stages.values())))
                self.run.cache.update(found)
            except Exception as e:  # an optimisation only: any failure reads over REST
                print(f"prefetch of {batch} failed, reading over REST: {e}", file=sys.stderr)

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
        specs = after.get("label_specs") or {}
        for label in (after["label_plan"].get(self.repo, []) if "label_plan" in after else after["labels"]):
            if label not in known:
                color = (specs.get(label) or {}).get("color") or random_color()
                self.run(["api", f"repos/{self.repo}/labels", "-f", f"name={label}", "-f", f"color={color}"])
        title = "Deploy needed"
        existing = next((i for i in self._list("issues?state=open&per_page=100")
                         if "pull_request" not in i and i.get("title") == title), None)
        items = []
        for key in ("automations", "environments", "labels", "files"):
            old, new = ((before.get(key) or {}, after.get(key) or {}) if key == "files"
                        else (before.get(key) or [], after.get(key) or []))
            if key == "labels":  # ADR-0024: a plan with `label_plan` lists `repository label` entries
                old, new = ([f"{r} {n}" for r, ls in p["label_plan"].items() for n in ls] if "label_plan" in p else v
                            for p, v in ((before, old), (after, new)))
            if key in ("automations", "environments"):
                old = {x["name"]: x for x in old}
                new = {x["name"]: x for x in new}
            elif key == "labels":
                old, new = {x: x for x in old}, {x: x for x in new}
            items.extend(f"- {key}: {name}" for name in sorted(old.keys() | new.keys()) if old.get(name) != new.get(name))
        body = ("[factory] The merged commit changes the Air deploy plan. Run `factory deploy --apply` locally.\n\n"
                + "\n".join(items) + f"\n\nFactory-Run: finish:{pr}")
        if existing:
            self.run(["issue", "edit", str(existing["number"]), "-R", self.repo, "--body", body,
                      "--add-label", vocab.needs_human(), "--add-assignee", config.owner()])
            self.run(["issue", "comment", str(existing["number"]), "-R", self.repo, "--body",
                      f"[factory] Deploy plan changed by #{pr}.\n\nFactory-Run: finish:{pr}"])
        else:
            url = self.run(["issue", "create", "-R", self.repo, "--title", title, "--body", body,
                            "--label", vocab.needs_human(), "--assignee", config.owner()]).strip()
            self.run(["issue", "comment", url.rsplit("/", 1)[-1], "-R", self.repo, "--body",
                      f"[factory] Deploy plan changed by #{pr}.\n\nFactory-Run: finish:{pr}"])

        files = self._list(f"pulls/{pr}/files?per_page=100", repo=self.pr_repo)
        if not any(f.get("filename") == "factory.descriptor.yaml" or
                   f.get("filename", "").startswith(("engine/", "workflows/")) for f in files):
            return
        for other in self._list("pulls?state=open&per_page=100", repo=self.pr_repo):
            head = other.get("head") or {}
            ticket = ticket_from_ref(head.get("ref"))
            if ticket is None or \
                    ((head.get("repo") or {}).get("full_name") or "").lower() != self.pr_repo.lower():
                continue
            # The update node only starts from changes-requested. Move the ticket
            # before adding its PR command label so the label event can pass check.
            self.apply({"ticket": ticket, "kind": "stage", "value": "changes-requested"})
            labels = self._labels(other["number"], repo=self.pr_repo)
            if vocab.cmd("update-<tier>", "cheap") not in labels:
                self._edit(other["number"], [vocab.cmd("update-<tier>", "cheap")], on="pr")

    def _pr(self, n):
        """The ticket's PR from its branch: the open one, else the newest. None without a PR."""
        prs = [p for p in self._list("pulls?state=all&per_page=100", repo=self.pr_repo) if p["head"]["ref"].startswith(f"ez/{n}-")
               and ((p["head"].get("repo") or {}).get("full_name") or "").lower() == self.pr_repo.lower()]  # never a fork's PR
        prs.sort(key=lambda p: (p["state"] == "open", p["number"]))
        if not prs:
            return None
        pr = self._api(f"pulls/{prs[-1]['number']}", repo=self.pr_repo)
        for _ in range(3):  # GitHub computes `mergeable` lazily: null means UNKNOWN, read again
            if pr.get("state") != "open" or pr.get("mergeable") is not None:
                break
            time.sleep(2)
            pr = self._api(f"pulls/{prs[-1]['number']}", repo=self.pr_repo)
        return pr

    def _prs(self, n, open_only=False):
        """The ticket's PRs from its branch; cleanup also includes closed PRs."""
        prs = [p for p in self._list("pulls?state=all&per_page=100", repo=self.pr_repo)
               if p["head"]["ref"].startswith(f"ez/{n}-")
               and ((p["head"].get("repo") or {}).get("full_name") or "").lower() == self.pr_repo.lower()
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
        out = self.run(["api", "--paginate", f"repos/{self.pr_repo}/actions/runs?head_sha={sha}&per_page=100",
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
                out = self.run(["api", "--paginate", f"repos/{self.pr_repo}/actions/runs/{run['id']}/jobs?per_page=100",
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
                            logs = self.run(["api", f"repos/{self.pr_repo}/actions/jobs/{job['id']}/logs"], timeout=10)
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
        for r in self._list(f"pulls/{pr['number']}/reviews", repo=self.pr_repo):
            m = next(iter(list(MARKER.finditer(r.get("body") or ""))[-1:]), None)  # the footer marker is last; prose cannot follow it
            agent = bool(m) and r["user"]["login"] == self.bot
            verdict = (m[3] if agent else {"APPROVED": "approve", "CHANGES_REQUESTED": "changes"}.get(r["state"]))
            out.append({"author": r["user"]["login"], "state": r["state"], "commit_id": r["commit_id"],
                        "focus": m[1] if m else None, "round": int(m[2]) if m else None,
                        "rereview": bool(m and m[4]), "outcome": m[5] if m else None,
                        "verdict": verdict, "agent": agent, "assoc": r.get("author_association"), "submitted_at": r.get("submitted_at"),
                        "bot": r["user"].get("type") == "Bot" or r["user"]["login"].endswith("[bot]")})
            out[-1]["findings"] = re.findall(r"^\*\*([A-Z]\d+)\*\*", r.get("body") or "", re.M)
            out[-1]["body"] = r.get("body") or ""  # #590: a mention in a review asks that agent
        return out

    @in_ticket_repository
    def changed_since(self, n, old):
        """{file: [(first, last) head lines of each hunk] or None for the whole file} of what a fix changed between
        head `old` and ticket `n`'s current head: files whose PR patch differs (so a merge from the base branch does
        not count), with the fix's hunks. None when it cannot be compared."""
        try:
            number = self.facts(n).get("pr")
            if not number:
                return None
            pr = self._api(f"pulls/{number}", repo=self.pr_repo)
            before, after = self._pr_patch(pr["base"]["ref"], old), self._pr_patch(pr["base"]["ref"], pr["head"]["sha"])
        except Exception:  # an unknown or force-pushed-away head: compare is not possible
            return None
        if before is None or after is None:
            return None
        files = {f for f in before.keys() | after.keys() if before.get(f) != after.get(f)}
        try:  # the fix's own hunks, in head line numbers; a merge's hunks fall outside `files` or add nothing found
            diff = self._api(f"compare/{old}...{pr['head']['sha']}", repo=self.pr_repo)["files"]
        except Exception:
            return {f: None for f in files}
        out = {f: None for f in files}
        for x in diff:
            if x["filename"] in files and x.get("patch") is not None:
                out[x["filename"]] = [(int(m[1]), int(m[1]) + int(m[2] or 1))
                                      for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", x["patch"], re.M)]
        return out

    def _pr_patch(self, base, sha):
        """The PR's patch at `sha` as {file: patch with hunk line numbers dropped}, like `git patch-id --stable`.
        None when GitHub omits a patch (binary or huge diff) or the compare list may be cut at its 300-file limit,
        so the caller cannot compare."""
        files = self._api(f"compare/{base}...{sha}", repo=self.pr_repo)["files"]
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
                parents = [x["sha"] for x in self._api(f"commits/{cur}", repo=self.pr_repo)["parents"]]
                if len(parents) != 2 or self._api(f"compare/{base}...{parents[1]}", repo=self.pr_repo)["ahead_by"] != 0:
                    return False
                cur = parents[0]
            patch = self._pr_patch(base, old)
            return cur == old != head and patch is not None and patch == self._pr_patch(base, head)
        except Exception:  # unknown means the new head needs a review
            return False

    def _can_write(self, login):
        """Write access to the code host (see `GitHubTicketStore.can_write`)."""
        return self.github.can_write(login, self.pr_repo)

    # --- records (ADR-0026): the facts are derived from them in the core (factory/derive.py)
    def rules(self):
        return derive.Rules(self.stages, self.risk_labels, self.required, self.fix_cap, self.run_cap, self.trusted)

    def read(self, subject, want=frozenset()):
        """The TicketRecord of ticket `subject.id`, or the PrRecord of its PR in repository `subject.repo` (None
        without one). `want` "behind" reads how far the PR's base is ahead of its head."""
        if subject.kind in ("ticket", "pr") and subject.id is not None:  # a ticket of another repository: its view
            view, number = ticket_view(self, subject.id)
            if view is not self:
                return view.read(replace(subject, id=number), want)
        if subject.kind == "ticket":
            return self._ticket_record(subject.id)
        if subject.kind == "pr":
            view = self._view(subject.repo)
            pr = view._pr(subject.id)
            return view._pr_record(pr, subject.repo, want) if pr else None
        raise ValueError(f"read: unknown subject {subject.kind}")

    def find(self, query):
        """`children`: the sub-tickets of `query.of`; `stage`: the tickets holding one of the stages `query.values`;
        `labeled`, `prs`, `closed`: the local surface's poll of this repository (`_labeled`, `_recent_prs`,
        `_recently_closed`)."""
        if query.kind == "children":
            return self.tickets.children(query.of)
        if query.kind == "stage":
            return self.sweep_tickets(query.values, query.open_only)
        if query.kind == "labeled":
            return self._labeled(query.values)
        if query.kind == "prs":
            return self._recent_prs(*query.values)
        if query.kind == "closed":
            return self._recently_closed()
        raise ValueError(f"find: unknown query {query.kind}")

    def _lines(self, *args):
        return [json.loads(x) for x in self.run(list(args)).splitlines() if x.strip()]

    def _labeled(self, watched):
        """Each open issue and PR carrying a watched label, as {number, pr, branch, label, event}: `watched` holds
        (`issue`|`pr`, label) pairs and `event` is GitHub's REST id of the item's latest `labeled` event of that label.
        All labels are read by one paged search (10 nodes a page); the id comes from the node id, or from `/events`
        when it does not decode or the timeline was paged past."""
        watched = set(watched)
        labels = sorted({label for _, label in watched})
        if not labels:
            return []
        items, after = [], None
        while True:
            page = json.loads(self.run(["api", "graphql", "-f", f"query={graphql.POLL}", "-f",
                                        f"q={graphql.poll_query(self.repo, labels)}", *(["-f", f"after={after}"] if after else [])]))
            page = page["data"]["search"]
            items += page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        out = []
        for item in items:
            n, pr = item["number"], item["__typename"] == "PullRequest"
            ids = graphql.labeled_events(item)
            for label in (x["name"] for x in item["labels"]["nodes"]):
                if ("pr" if pr else "issue", label) not in watched:
                    continue
                rid = ids.get(label) if ids is not None else None
                if rid is None:  # an undecodable id, or a timeline paged past what was read: the REST read
                    found = [e["id"] for e in self._lines("api", "--paginate", f"repos/{self.repo}/issues/{n}/events", "--jq",
                                                          '.[] | select(.event == "labeled") | {id, name: .label.name}')
                             if e["name"] == label]
                    rid = found[-1] if found else None
                if rid is not None:
                    out.append({"number": n, "pr": pr, "branch": item.get("headRefName"), "label": label, "event": rid})
        return out

    def _recent_prs(self, state):
        """The 20 latest PRs in `state` (`merged` or `open`), as {number, branch, head}."""
        return [{"number": x["number"], "branch": x["headRefName"], "head": x["headRefOid"]}
                for x in self._lines("pr", "list", "-R", self.repo, "--state", state, "--limit", "20",
                                     "--json", "number,headRefName,headRefOid", "--jq", ".[]")]

    def _recently_closed(self):
        """The 20 latest closed issues, as {number, closed_at}."""
        return [{"number": x["number"], "closed_at": x["closedAt"]}
                for x in self._lines("issue", "list", "-R", self.repo, "--state", "closed", "--limit", "20",
                                     "--json", "number,closedAt", "--jq", ".[]")]

    def _view(self, name):
        """This tracker on repository `name` of the descriptor (the primary when None)."""
        repo = self.repositories[name] if name is not None else self.pr_repo
        return self if repo == self.pr_repo else self._on(repo)

    def _ticket_record(self, n):
        issue = self.tickets.issue(n)
        events = self.tickets.events(n)
        blockers = self.tickets.open_blockers(n)
        return TicketRecord(n, issue["title"], issue["body"], issue["state"], issue["close_reason"], issue["author"],
                            issue["updated_at"], issue["attrs"], issue["assignees"], events, self.tickets.comments(n),
                            blockers, Lazy(self.tickets.can_answer))

    def _pr_record(self, pr, name, want=frozenset()):
        """The record of PR `pr` (the REST body) in this view's repository, descriptor name `name`."""
        head, user = pr["head"]["sha"], pr.get("user") or {}
        comments = [{"id": c.get("id"), "author": (c.get("user") or {}).get("login", ""),
                     "author_type": (c.get("user") or {}).get("type", ""), "body": c.get("body"), "at": c.get("created_at")}
                    for c in self._list(f"issues/{pr['number']}/comments", repo=self.pr_repo)]
        behind = None
        if "behind" in want and pr.get("state") == "open":
            behind = self._api(f"compare/{pr['base']['ref']}...{head}", repo=self.pr_repo)["behind_by"] > 0
        return PrRecord(name, pr["number"], head, base=pr["base"]["ref"], branch=pr["head"]["ref"], state=pr.get("state"),
                        merged=bool(pr.get("merged")), mergeable=pr.get("mergeable"),
                        labels=[x["name"] for x in pr.get("labels", [])],
                        requested_reviewers=[u["login"] for u in pr.get("requested_reviewers") or []],
                        body=pr.get("body"), author=user.get("login", ""), author_type=user.get("type", ""),
                        updated_at=pr.get("updated_at"), reviews=self._reviews(pr), comments=comments,
                        events=self._pr_events(pr["number"]), ci=self._ci(head),
                        commit_date=self._api(f"commits/{head}", repo=self.pr_repo)["commit"]["committer"]["date"],
                        can_write=Lazy(self._can_write),
                        clean_advance=Lazy(lambda old: self._clean_base_advance(old, head, pr)), behind=behind)

    def parent(self, n):
        """The parent issue number of sub-issue `n`, else None."""
        return self.tickets.parent(n)

    def children(self, n):
        """The sub-issues of `n` (the `children` guard, ADR-0021)."""
        return self.tickets.children(n)

    def blocking(self, n, repo=None):
        """Issues whose native or body fallback dependency list names `n` as a blocker."""
        return self.tickets.blocking(n, repo) if repo else self.tickets.blocking(n)

    def blocked_by(self, n):
        return self.tickets.blocked_by(n) if hasattr(self.tickets, "blocked_by") else []

    def not_planned(self, n):
        """True when issue `n` was closed with state_reason not_planned."""
        issue = self.tickets.issue(n)
        return issue["state"] == "closed" and issue["close_reason"] == "not_planned"

    @in_ticket_repository
    def facts(self, n):
        gate = GateTracker.single(self)  # this tracker hosts the PRs too: `read` finds each repository's view
        f = gate.facts(n)
        self._since = gate.since  # a person's factory:ready starts fresh (`_review` counts rounds from it)
        return f

    def _on(self, repo):
        """This tracker reading and writing the pull requests of `repo` (one repository of a ticket)."""
        view = copy.copy(self)
        view.pr_repo = repo
        return view

    def at(self, repo):
        """This tracker on ticket repository `repo` (ADR-0024): its tickets and their pull requests are that
        repository's, and a bare ticket number is its issue. `repo` is the view's primary repository."""
        view = self._on(repo)
        view.repo = repo
        view.tickets = self.tickets.at(repo)
        view.github = view.tickets if self.github is self.tickets else self.github
        name = next((k for k, v in self.repositories.items() if v == repo), repo.rsplit("/", 1)[-1])
        view.repositories = {name: repo, **{k: v for k, v in self.repositories.items() if v != repo}}
        return view

    @in_ticket_repository
    def stale_facts(self, n):
        """facts plus what the stale sweep needs: `since` (last label or commit event, ISO), the command label
        (`factory:implement-*` on the issue, `factory:fix-*` on the PR) and when it was added, the head commit
        date, the PR's CI label, and the `[factory] recover:` comments written since the stage began."""
        f, labeled, events = self.facts(n), {}, []
        pr = f["pr"]
        for t in [n] + ([pr] if pr else []):
            for e in self.tickets.events(n) if t == n else self._pr_events(t):
                events.append((t, e))
                if e["kind"] == "labeled":
                    labeled[(t, e["value"])] = max(e["at"], labeled.get((t, e["value"]), ""))
        pr_labels = f.get("pr_labels", [])
        review_labels = [(x, labeled.get((pr, x), "")) for x in pr_labels
                         if x.startswith((vocab.prefix("review-<focus>"), vocab.prefix("rereview-<focus>"))) and labeled.get((pr, x))]
        have = [(n, x) for x in self._labels(n)] + [(pr, x) for x in pr_labels]
        cmd = next(((t, x) for t, x in have if x.startswith((vocab.prefix("implement-<tier>"), vocab.prefix("fix-<tier>"), vocab.prefix("update-<tier>"), vocab.prefix("review-<focus>")))), None)
        command_labels = (vocab.stage("ready"), vocab.stage("triage"), vocab.prefix("implement-<tier>"), vocab.prefix("fix-<tier>"),
                          vocab.prefix("update-<tier>"), vocab.ci("ci-green"))
        active_commands = {(t, x) for t, x in have if x == vocab.stage("ready") or x == vocab.stage("triage")
                           or x == vocab.ci("ci-green") or x.startswith(command_labels[2:5]) or x.startswith(vocab.prefix("review-<focus>"))}
        command_events = [e for t, e in events if e["kind"] == "labeled" and (t, e["value"]) in active_commands]
        commit_at = self._api(f"commits/{f['head']}", repo=self.pr_repo)["commit"]["committer"]["date"] if f["head"] else None
        latest_event = max([e["at"] or "" for _, e in events] + [commit_at or ""])
        latest_command = max((e["at"] for e in command_events), default="")
        stage_at = labeled.get((n, self.stages[f["stage"]]), "") if f["stage"] in self.stages else ""
        comments = [c["body"] for c in self.tickets.comments(n) if c["body"].startswith(("[factory] recover:", "[factory] reconcile:", "[factory] end failed:"))
                    and c["at"] >= stage_at]
        return {**f, "since": max([*labeled.values(), commit_at or ""]) or None, "command": cmd and cmd[1], "stage_at": stage_at or None,
                "command_pending": bool(latest_command and latest_command == latest_event),
                "command_at": cmd and labeled.get(cmd), "commit_at": commit_at, "recover_comments": comments,
                "review_labels": review_labels, "label_at": {x: at for (_, x), at in labeled.items()},
                "ci_label": next((ci for ci in ("green", "red") if vocab.ci(f"ci-{ci}") in pr_labels
                                 and ci == f.get("ci")), None)}  # a label left over from an earlier head is not this head's CI

    # --- effects: read the target first, write only when it differs
    def _edit(self, n, add=(), remove=(), on="issue"):
        if on == "issue":
            return self.tickets.edit_attrs(n, add, remove)
        flags = [x for a in add for x in ("--add-label", a)] + [x for r in remove for x in ("--remove-label", r)]
        self.run([on, "edit", str(n), "-R", self.pr_repo, *flags])
        return True

    def apply(self, e):
        view, number = ticket_view(self, e["ticket"]) if e.get("ticket") is not None else (self, None)
        if view is not self:
            return view.apply({**e, "ticket": number})
        return GateTracker.single(self).apply(e)

    # --- the effect handlers (ADR-0026): (system, effect, ctx) -> wrote, declared in `effects` below
    def _issue_once(self, n, ctx):
        """Ticket `n` as read once per `effects.apply`: `stage: done` closes before the stage write reads the attrs."""
        if "issue" not in ctx:
            ctx["issue"] = self.tickets.issue(n)
        return ctx["issue"]

    def _fx_seen(self, e, ctx):
        return False  # no hidden state: conditions catch a duplicate

    def _fx_join(self, e, ctx):
        return self._join(e["ticket"], e.get("tier") or "mid")

    def _fx_close(self, e, ctx):
        n = e["ticket"]
        if self._issue_once(n, ctx)["state"] != "open":  # read before the close: the stage write reads its attrs
            return False
        self.tickets.close(n)
        return True

    def _fx_stage(self, e, ctx):
        n, value = e["ticket"], e["value"]
        want, have = self.stages[value], [x for x in self._issue_once(n, ctx)["attrs"] if x in self.stages.values()]
        return have != [want] and self.tickets.set_stage(n, want, have)

    @staticmethod
    def _gone(labels, value):
        """`prefix-*` removes every label with that prefix (a command label of any tier)."""
        return [x for x in labels if x.startswith(value[:-1])] if value.endswith("*") else [value] if value in labels else []

    def _fx_label(self, e, ctx):
        n, kind, value, on = e["ticket"], e["kind"], e["value"], e.get("on") or "issue"
        if on != "issue":
            raise ValueError(f"{kind}: bad target: {on}")
        labels = self._labels(n)
        if kind == "label":
            return value not in labels and self._edit(n, [value], on=on)
        gone = self._gone(labels, value)
        return bool(gone) and self._edit(n, remove=gone, on=on)

    def _fx_pr_label(self, e, ctx):
        n, kind, value = e["ticket"], e["kind"], e["value"]
        if value == vocab.needs_human():
            prs = self._prs(n, open_only=kind == "label")
        else:
            pr = self._pr(n)
            prs = [pr] if pr else []
        changed = False  # no PR: ticket-level waits also apply before a PR exists
        for pr in prs:
            labels = self._labels(pr["number"], repo=self.pr_repo)
            if kind == "label":
                if value not in labels:
                    changed |= self._edit(pr["number"], [value], on="pr")
            elif gone := self._gone(labels, value):
                changed |= self._edit(pr["number"], remove=gone, on="pr")
        return changed

    def _fx_set_field(self, e, ctx):  # an attribute label `<field>:<value>`; one value per field
        n, ((k, v),) = e["ticket"], e["value"].items()
        have = [x for x in self._labels(n) if x.startswith(f"{k}:")]
        return have != [f"{k}:{v}"] and self._edit(n, [f"{k}:{v}"] if f"{k}:{v}" not in have else [],
                                                   [x for x in have if x != f"{k}:{v}"])

    def _fx_assign(self, e, ctx):
        n, kind, value, on = e["ticket"], e["kind"], e["value"], e.get("on") or "issue"
        if on != "issue":
            raise ValueError(f"{kind}: bad target: {on}")
        return self.tickets.assign(n, (value if isinstance(value, list) else [value]) if kind == "assign" else [])

    def _fx_pr_assign(self, e, ctx):
        n, kind, value = e["ticket"], e["kind"], e["value"]
        changed = False
        for pr in self._prs(n, open_only=kind == "assign"):
            current = {a["login"] for a in self._api(f"issues/{pr['number']}", repo=self.pr_repo).get("assignees", [])}
            desired = set((value if isinstance(value, list) else [value])) if kind == "assign" else set()
            if current != desired:
                flags = [x for a in sorted(desired - current) for x in ("--add-assignee", a)]
                flags += [x for a in sorted(current - desired) for x in ("--remove-assignee", a)]
                self.run(["issue", "edit", str(pr["number"]), "-R", self.pr_repo, *flags])
                changed = True
        return changed

    def _fx_comment(self, e, ctx):
        n, body = e["ticket"], effects_mod.frame(e["value"], e.get("run_id"))
        if any(c["body"] == body for c in self.tickets.comments(n)):
            return False
        self.tickets.comment(n, body + self._block(e))
        return True

    def _fx_pr_comment(self, e, ctx):
        n, body = e["ticket"], effects_mod.frame(e["value"], e.get("run_id"))
        pr = self._pr(n)
        if pr is None:
            raise RuntimeError(f"comment: no PR for ticket {n}")
        if any(c["body"] == body for c in self._list(f"issues/{pr['number']}/comments", repo=self.pr_repo)):
            return False
        self.run(["issue", "comment", str(pr["number"]), "-R", self.pr_repo, "--body", body + self._block(e)])
        return True

    def _fx_pr_notes(self, e, ctx):  # a developer's `done` notes for the PR description
        n, kind, value = e["ticket"], e["kind"], e["value"]
        pr = self._pr(n)
        if kind == "pr-notes" and pr is None:  # Air opens the PR after the run: park the notes on the ticket
            body = effects_mod.frame(f"{NOTES_PARKED}\n\n{value}", e.get("run_id"), once=False)
            if any(c["body"] == body for c in self.tickets.comments(n)):
                return False
            self.tickets.comment(n, body)
            return True
        if pr is None:
            return False
        if kind == "pr-notes-sync":  # the first gate step that sees the PR copies the parked notes
            parked = [c for c in self.tickets.comments(n) if c["body"].startswith(NOTES_PARKED)]
            found = parse_notes(parked[-1]["body"], "", "Bot" if parked[-1]["author_is_factory"] else "User") if parked else []
            if not found:
                return False
            value = notes_block(found[-1])
        if value in (pr.get("body") or ""):
            return False
        self.run(["pr", "edit", str(pr["number"]), "-R", self.pr_repo, "--body", f"{pr.get('body') or ''}\n\n{value}"])
        return True

    def _fx_react(self, e, ctx):  # a reaction on one issue comment of the ticket or its PR; the API keeps one per user
        repo = self.pr_repo if e.get("on") == "pr" else None
        return bool(self._api(f"issues/comments/{e['comment']}/reactions", "-f", f"content={e['value']}", repo=repo))

    def _fx_review(self, e, ctx):
        return self._review(e["ticket"], e)

    def _fx_request_reviewers(self, e, ctx):
        return self._rerequest(e["ticket"])

    def _fx_create_issues(self, e, ctx):
        return self.tickets.create_children(e["ticket"], e["value"], self.stages["ready"])

    def _fx_park(self, e, ctx):
        return self.tickets.park(e["ticket"], e["value"], self.stages["triage"])

    def _fx_merge(self, e, ctx):  # the primary last: only its merge event wakes `finish` (Air triggers on its repository)
        n = e["ticket"]
        views, wrote = self._views(n), False
        readonly = [f"{name} #{pr['number']}" for name, _, pr in views if pr and not pr["merged"] and not self._may_write(name)]
        if readonly:  # refuse before any merge: a half-merged ticket is worse than none
            raise RuntimeError(f"merge: {', '.join(readonly)} is read-only in this environment; a person merges it")
        for name, view, pr in effects_mod.primary_last(views):
            if pr is None:
                raise RuntimeError(f"merge: no PR for ticket {n} in {name}")
            if pr["merged"]:
                continue
            if not view._locked_merge(pr, self._judged_head(e, name, views) or pr["head"]["sha"]):
                return wrote  # locked, or behind and updated: the next sweep merges the rest
            wrote = True
        return wrote

    def _fx_update_branch(self, e, ctx):  # a merge commit of the base into the PR branch; refused when the head moved
        views, wrote = self._views(e["ticket"]), False
        for name, view, pr in views:
            if pr is None or pr["merged"] or not self._may_write(name):
                continue
            head = self._judged_head(e, name, views) or pr["head"]["sha"]
            if len(views) > 1 and view._api(f"compare/{pr['base']['ref']}...{head}", repo=view.pr_repo)["behind_by"] == 0:
                continue
            view._update_branch(pr, head)
            wrote = True
        return wrote

    effects = (
        Effect("seen", "ticket", _fx_seen), Effect("join", "ticket", _fx_join),
        Effect("close", "ticket", _fx_close), Effect("stage", "ticket", _fx_stage),
        Effect("label", "ticket", _fx_label), Effect("unlabel", "ticket", _fx_label),
        Effect("label", "pr", _fx_pr_label), Effect("unlabel", "pr", _fx_pr_label),
        Effect("set-field", "ticket", _fx_set_field),
        Effect("assign", "ticket", _fx_assign), Effect("unassign", "ticket", _fx_assign),
        Effect("assign", "pr", _fx_pr_assign), Effect("unassign", "pr", _fx_pr_assign),
        Effect("comment", "ticket", _fx_comment), Effect("comment", "pr", _fx_pr_comment),
        Effect("pr-notes", "pr", _fx_pr_notes), Effect("pr-notes-sync", "pr", _fx_pr_notes),
        Effect("react", "ticket", _fx_react), Effect("react", "pr", _fx_react),
        Effect("review", "pr", _fx_review, remote=True), Effect("request-reviewers", "pr", _fx_request_reviewers),
        Effect("create-issues", "ticket", _fx_create_issues), Effect("park-behind-main-red", "ticket", _fx_park),
        Effect("merge", "pr", _fx_merge, remote=True), Effect("update-branch", "pr", _fx_update_branch, remote=True),
    )

    def _may_write(self, name):
        return self.writable is None or name in self.writable

    def ticket_repositories(self, n, body):
        """The repository names ticket `n` changes (ADR-0017): the ticket store's answer when it links tickets to
        repositories (YouTrack `tracker.projects`, #981), else the ticket's `repositories:` line in `body`."""
        own = getattr(self.tickets, "ticket_repositories", None)
        linked = own(n) if own else None
        if linked is not None:
            return linked
        if getattr(self, "one_repository", False):  # ADR-0027: the ticket's own repository (`at` puts it first)
            return [next(iter(self.repositories))]
        return ticket_repositories(body, list(self.repositories), next(iter(self.repositories)))

    def _views(self, n):
        """(repository name, view, PR or None) per repository of ticket `n`, in the ticket's order (ADR-0017)."""
        body = self.tickets.issue(n)["body"]
        out = []
        for name in self.ticket_repositories(n, body):
            view = self if self.repositories[name] == self.pr_repo else self._on(self.repositories[name])
            out.append((name, view, view._pr(n)))
        return out

    @staticmethod
    def _judged_head(e, name, views):
        """The head of repository `name` that the gates judged: `heads` of a multi-repository ticket, else `head`."""
        return (e.get("heads") or {}).get(name) if len(views) > 1 else e.get("head")

    def _update_branch(self, pr, head):
        self.run(["api", "-X", "PUT", f"repos/{self.pr_repo}/pulls/{pr['number']}/update-branch",
                  "-f", f"expected_head_sha={head}"])

    MERGE_LOCK, RECENT_MERGE_SECONDS = "factory-merge-lock", 60

    def _locked_merge(self, pr, head):
        """One merge at a time across runs: the lock is a branch, created atomically (GitHub refuses a second create).
        Under it, wait out RECENT_MERGE_SECONDS after another PR's merge, so the base this
        merge integrates is the one the gates judged. False (nothing merged) means the next sweep reads it again.
        A crashed run leaves the branch; a person deletes it."""
        base = pr["base"]["ref"]
        try:
            self.run(["api", "-X", "POST", f"repos/{self.pr_repo}/git/refs", "-f", f"ref=refs/heads/{self.MERGE_LOCK}",
                      "-f", f"sha={self._api(f'git/ref/heads/{base}', repo=self.pr_repo)['object']['sha']}"])
        except RuntimeError:
            return False  # another run holds the lock
        try:
            now = datetime.now(timezone.utc)
            wait = max((self.RECENT_MERGE_SECONDS - (now - datetime.fromisoformat(o["merged_at"].replace("Z", "+00:00"))).total_seconds()
                        for o in self._api(f"pulls?state=closed&base={base}&sort=updated&direction=desc&per_page=5", repo=self.pr_repo)
                        if o["number"] != pr["number"] and o.get("merged_at")), default=0)
            if wait > 0:
                time.sleep(wait)  # no event wakes `merge` again after the window, so wait it out under the lock
            if self._api(f"compare/{base}...{head}", repo=self.pr_repo)["behind_by"] > 0:
                self._update_branch(pr, head)  # the merge that just landed moved the base: CI reruns, ci-green wakes `merge`
                return False
            self.run(["pr", "merge", str(pr["number"]), "-R", self.pr_repo, "--merge", "--match-head-commit", head])
            return True
        finally:
            self.run(["api", "-X", "DELETE", f"repos/{self.pr_repo}/git/refs/heads/{self.MERGE_LOCK}"])

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
            self.apply({"ticket": n, "kind": "unlabel", "value": vocab.cmd("fix-<tier>", "*"), "on": "pr"})
        wrote = self.apply({"ticket": n, "kind": "label", "value": vocab.cmd("fix-<tier>", tier), "on": "pr"})
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
        self.run(["api", "-X", "POST", f"repos/{self.pr_repo}/pulls/{pr['number']}/requested_reviewers",
                  *[x for a in who for x in ("-f", f"reviewers[]={a}")]])
        return True

    @staticmethod
    def _block(e):
        """The hidden notes block an effect carries (`notes`: the gate's cleaned object), after the visible text."""
        return f"\n\n{notes_block(e['notes'])}" if e.get("notes") else ""

    def _review(self, n, e):
        """The reviewer's verdict as a COMMENT review on each PR of the ticket, pinned to that PR's own head. The
        rereview command label lives on the primary PR, which wakes the reviewer."""
        # only open PRs in repositories this environment may write: a merged PR is done, a read-only one is not ours
        views = [(name, view, pr) for name, view, pr in self._views(n)
                 if pr and not pr.get("merged") and pr.get("state", "open") == "open" and self._may_write(name)]
        focus = e.get("focus")
        if not views or not focus:
            raise RuntimeError("review: needs a PR and a payload `focus`")
        _, first, primary = views[0]
        rereview = vocab.cmd("rereview-<focus>", focus) in first._labels(primary["number"], repo=first.pr_repo)
        facts, wrote = self.facts(n), False
        for name, view, pr in views:
            wrote |= view._post_review(pr, e, focus, rereview, [r for r in facts["reviews"] if r.get("repo", name) == name],
                                       self._since)
        if wrote and rereview:
            first._edit(primary["number"], remove=[vocab.cmd("rereview-<focus>", focus)], on="pr")
        return wrote

    def _post_review(self, pr, e, focus, rereview, reviews, since):
        head = pr["head"]["sha"]
        existing = [r for r in self._reviews(pr) if r["agent"] and r["focus"] == focus and r["commit_id"] == head]
        if existing and (not rereview or any(r["rereview"] for r in existing)):
            return False
        earlier_change_heads = {r["commit_id"] for r in reviews
                                if r["agent"] and r["verdict"] == "changes"
                                and r["commit_id"] != head
                                and (r.get("submitted_at") or "") >= since}
        rnd = e.get("round") or len(earlier_change_heads) + 1
        verdict = "approve" if e["value"].startswith("approve") else "changes"
        outcome = f" outcome={e['outcome']}" if e.get("outcome") else ""
        marker = f"<!-- factory:review focus={focus} round={rnd} verdict={verdict}{' rereview=1' if rereview else ''}{outcome} -->"
        found = sorted(e.get("findings") or [], key=lambda x: x.get("severity") != "blocking")  # blocking first
        defang = lambda t: str(t).replace("<!--", "<!\u200b--")  # noqa: E731  # prose never carries a marker
        lines = [defang(f"**{x['id']}** {x['file']} — {x['finding']}") for x in found]
        summary = f"[factory] {defang(e['summary'])}" if e.get("summary") else "[factory]"
        body = "\n\n".join(x for x in (summary, "\n".join(lines), marker) if x)
        if e.get("run_id"):
            body += f"\n\nFactory-Run: {e['run_id']}"
        self.run(["api", "-X", "POST", f"repos/{self.pr_repo}/pulls/{pr['number']}/reviews", "-f", f"commit_id={head}",
                  "-f", "event=COMMENT", "-f", f"body={body}{self._block(e)}"])
        return True

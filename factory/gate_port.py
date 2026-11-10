"""The tracker port of the gate (factory/gate.py) and two in-memory fakes.

`Tracker.facts(ticket)` returns a dict with: stage ("none" when unset), head, blockers_open (int),
focus_missing (list), round_complete (bool), round_verdict (changes|approve|None), round_head, fix_rounds, fix_cap, ci, due, seen (event ids).
`Tracker.apply(effect)` takes {ticket, kind, value, event?}, reads first and writes only when the target
state differs; it returns True when it wrote. The stage effect also records `event` in `seen`; so does the `seen` effect, which apply appends to an
outcome without a stage effect.
"""
import functools
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Protocol

from ports.system import Effect

CLOSED_HOURS = 48  # a ticket closed on merge while its finish run was lost (#567) is found for this long


def ticket_from_ref(ref):
    """The ticket id of a branch `ez/<ticket>-<slug>` or a PR title starting `<ticket> `: digits give an int
    (GitHub issue), an opaque id such as `INFRA-123` stays a string. None when `ref` names no ticket."""
    m = re.match(r"(?:refs/heads/)?ez/([A-Za-z][A-Za-z0-9]*-\d+|\d+)-", ref or "") or re.match(r"#?([A-Za-z][A-Za-z0-9]*-\d+|\d+)(?=[ :])", ref or "")
    return None if not m else int(m[1]) if m[1].isdigit() else m[1]


REF = re.compile(r"([\w.-]+/[\w.-]+)#(\d+)")


def ticket_ref(repo, number, tracker=None):
    """The id of issue `number` of `repo` (ADR-0024): `<owner>/<repo>#<n>`, or the bare number when `repo` is the
    tracker (the first ticket repository; no `tracker` given: the ticket has no other repository to tell apart)."""
    return number if not repo or not tracker or repo.lower() == tracker.lower() else f"{repo}#{number}"


def split_ticket(ticket, default=None):
    """`(repository, number)` of a ticket id: `<owner>/<repo>#<n>` names its repository; a bare `n` or `#n` is
    in `default` (the tracker). An opaque id such as `INFRA-123` is returned whole, in `default`."""
    if isinstance(ticket, str):
        if m := REF.fullmatch(ticket):
            return m[1], int(m[2])
        if re.fullmatch(r"#\d+", ticket):
            return default, int(ticket[1:])
    return default, ticket


def tracker_repositories(tracker):
    """The ticket repositories of a descriptor's `tracker` block as `owner/repo`, the tracker first: `repositories`,
    else the one `repository`."""
    listed = tracker.get("repositories")
    listed = listed if isinstance(listed, list) else [tracker["repository"]] if tracker.get("repository") else []
    return list(dict.fromkeys("/".join(str(r).removesuffix(".git").rstrip("/").split("/")[-2:]) for r in listed
                              if isinstance(r, str)))


def number_of(ticket):
    """The issue number of a ticket id, without its repository: what a branch `ez/<n>-<slug>` carries."""
    return split_ticket(ticket)[1]


def ticket_view(obj, ticket):
    """`(view, number)` of a ticket id for `obj`, an object with `repo` (the repository its bare ticket numbers belong
    to), `ticket_repos` and `at(repo)` (the same object on another ticket repository). A repository that holds no
    tickets is refused: nothing is read or written there."""
    repo, number = split_ticket(ticket, obj.repo)
    if repo.lower() == obj.repo.lower():
        return obj, number
    if repo.lower() not in {r.lower() for r in obj.ticket_repos}:
        raise ValueError(f"{ticket}: {repo} is not a ticket repository")
    return obj.at(repo), number


def in_ticket_repository(method):
    """A method of such an object that takes the ticket first: a ticket of another repository runs on that
    repository's view, with its bare number."""
    @functools.wraps(method)
    def run(self, ticket, *args, **kwargs):
        view, number = ticket_view(self, ticket)
        return getattr(view, method.__name__)(number, *args, **kwargs) if view is not self else method(self, number, *args, **kwargs)
    return run


def ticket_repositories(body, names, primary):
    """The repositories a ticket changes (ADR-0017): its `repositories: a, b` body line, else the primary one. The
    primary comes first: Air wakes a ticket's runs only from its PR, and its merge, the last, wakes `finish`."""
    m = re.search(r"(?m)^repositories:[ \t]*(.+)$", body or "")
    if not m:
        return [primary]
    # `owner/repo` names the repository whose name is `repo` (the router writes the full name)
    repositories = re.sub(r"\s*\([^)]*\)", "", m[1])
    want = list(dict.fromkeys(x if x in names or x.rsplit("/", 1)[-1] not in names else x.rsplit("/", 1)[-1]
                              for x in re.split(r"[,\s]+", repositories) if x))
    unknown = [x for x in want if x not in names]
    if unknown or not want:
        raise ValueError(f"ticket names unknown repositories: {', '.join(unknown) or m[1]}")
    if want[0] != primary:
        raise ValueError(f"ticket repositories must start with the primary repository {primary}")
    return want


# The facts one pull request gives its ticket; combine_prs joins them over a ticket's repositories.
PR_DEFAULTS = {"head": None, "pr": None, "branch": None, "mergeable": None, "conflict": False, "ci": "none",
               "reviews": [], "requested_reviewers": [], "focus_missing": [], "round_complete": False,
               "round_verdict": None, "round_head": None, "ignored_reviews": [], "waiting_reviewers": [],
               "pr_labels": [], "fix_rounds": 0, "pr_state": "none",
               "handed_to_fix": False, "behind": False}


def combine_prs(prs):
    """One ticket's PR facts over its PRs (`repo` plus the PR_DEFAULTS keys), in the ticket's repository order.
    The first PR gives head, pr and branch. Open PRs determine CI and review facts; when none are open, all PRs do.
    The ticket is approved only when every included round approves at its own head. One PR is returned as it is."""
    if len(prs) == 1:
        return dict(prs[0])
    active = [p for p in prs if p["pr_state"] == "open"] or prs
    union = lambda key: list(dict.fromkeys(x for p in prs for x in p.get(key) or []))
    cis, states = [p["ci"] for p in active], [p["pr_state"] for p in prs]
    changes = any(p["round_complete"] and p["round_verdict"] == "changes" for p in active)
    complete = changes or all(p["round_complete"] for p in active)
    moved = next((f"{p['repo']}:{p['round_head']}" for p in active if p["round_head"] not in (None, p["head"])), None)
    reviewed = all(p["round_head"] is not None for p in active)
    mergeable = [p["mergeable"] for p in prs]
    return {**{k: v for k, v in prs[0].items() if k != "repo"}, "heads": {p["repo"]: p["head"] for p in prs},
            "prs": [{k: p[k] for k in ("repo", "pr", "head", "branch", "pr_state", "ci", "round_complete",
                                    "round_verdict", "round_head")} for p in prs],
            "ci": next((c for c in ("red", "pending", "none") if c in cis), "green"),
            "pr_state": next((s for s in ("open", "none", "closed") if s in states), "merged"),
            "mergeable": False if False in mergeable else None if None in mergeable else True,
            "conflict": any(p["conflict"] for p in prs), "behind": any(p["behind"] for p in prs),
            "round_complete": complete,
            "round_verdict": "changes" if changes else "approve" if complete and all(
                p["round_verdict"] == "approve" for p in active) else None,
            "round_head": moved or (active[0]["head"] if reviewed else None),
            "handed_to_fix": any(p["handed_to_fix"] for p in prs), "fix_rounds": max(p["fix_rounds"] for p in prs),
            "reviews": [{**r, "repo": p["repo"]} for p in prs for r in p["reviews"]],
            **{k: union(k) for k in ("requested_reviewers", "focus_missing", "ignored_reviews", "waiting_reviewers",
                                     "pr_labels")}}


class Tracker(Protocol):
    def facts(self, ticket) -> dict: ...
    def apply(self, effect: dict) -> bool: ...


def fix_round_count(reviews, since=""):
    """Rounds a fix answers and that count against `limits.fix_rounds`: agent change requests, per head, since the person's last `factory:ready`, with at least one blocking finding (id `B<n>`). A round with
    none (a verdict bug, environment noise) is not a round. The first round counts; a later one counts only when it
    still carries a blocking id an earlier round raised (the fix did not resolve it), so a review that finds a new
    issue each round does not exhaust the developer's rounds (#1048). Backstop: at least half of all rounds count."""
    rounds = {}
    for r in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
        if r.get("agent") and r.get("verdict") == "changes" and (r.get("submitted_at") or "") >= since:
            ids = {str(i) for i in r.get("findings", []) if str(i).startswith("B")}
            if ids:
                rounds.setdefault(r["commit_id"], set()).update(ids)
    seen, counted = set(), 0
    for ids in rounds.values():
        counted += not seen or bool(ids & seen)
        seen |= ids
    return max(counted, (len(rounds) + 1) // 2)


class UnsupportedStore(ValueError):
    """A descriptor store this tracker cannot read or write (a `field` or `tag` store on a GitHub tracker, a `label` store on YouTrack)."""


# The facts a PR-fact state store (`store: {pr: <fact>}`) holds on.
PR_FACTS = {"open": lambda f: f.get("pr_state") == "open", "merged": lambda f: f.get("pr_state") == "merged",
            "ci-green": lambda f: f.get("ci") == "green", "ci-red": lambda f: f.get("ci") == "red",
            "conflicting": lambda f: bool(f.get("conflict")),
            "approved": lambda f: bool(f.get("round_complete")) and f.get("round_verdict") == "approve"}


class DescriptorPort:
    """States and commands by name, through the `store` each has in factory.project.yaml (ADR-0016). A tracker
    mixes it in and sets `project`; reads go through its `facts`, writes through its `apply`."""
    project = None
    state_kinds = ("label",)  # the store kinds this tracker holds states (ticket side) and commands in
    command_kinds = ("label", "pr_label")

    def _entries(self, section):
        if not isinstance(self.project, dict):
            raise ValueError("no project descriptor: the tracker reads states and commands from factory.project.yaml")
        return self.project.get(section) or {}

    def _state_store(self, name):
        import project as project_mod
        states = self._entries("states")
        if name not in states:
            raise KeyError(f"unknown state: {name}")
        store = states[name]["store"]
        return project_mod.store_kind(store), store

    def _command(self, name):
        import workflow
        _, kind, value = workflow.resolve(self._entries("commands"), name)
        if kind not in self.command_kinds:
            raise UnsupportedStore(f"command {name}: this tracker has no {kind} store")
        return kind, value

    def state(self, ticket):
        """The ticket's state held in a ticket store (label, field or tag), else `none`."""
        stage = self.facts(ticket)["stage"]
        states = self._entries("states")
        return stage if stage in states and any(k in (states[stage].get("store") or {}) for k in self.state_kinds) else "none"

    def pr_states(self, ticket):
        """The PR-fact states (`store: {pr: ...}`) that hold for the ticket's PR."""
        facts = self.facts(ticket)
        return [name for name, entry in self._entries("states").items()
                if "pr" in (entry.get("store") or {}) and PR_FACTS[entry["store"]["pr"]](facts)]

    def set_state(self, ticket, name, event=None):
        kind, store = self._state_store(name)
        if kind == "pr":
            raise ValueError(f"state {name} is a PR fact ({store['pr']}): the code host sets it, not the gate")
        if kind not in self.state_kinds:
            raise UnsupportedStore(f"state {name}: this tracker has no {kind} store")
        return self.apply({"ticket": ticket, "kind": "stage", "value": name,
                           **({"event": event} if event is not None else {})})

    def commands(self, ticket):
        """Command references present on the ticket (label stores) and its PR (pr_label stores), such as
        `implement-mid` for the template `implement-<tier>`."""
        import workflow
        facts, found = self.facts(ticket), []
        attrs = facts.get("attrs") or []
        held = {"label": attrs, "tag": attrs, "pr_label": facts.get("pr_labels") or []}
        held = {k: v for k, v in held.items() if k in self.command_kinds}
        for name, entry in self._entries("commands").items():
            store = entry.get("store") or {}
            kind = next(iter(store), None)
            if kind not in held:
                continue
            parts = workflow._template(name)
            for label in held[kind]:
                if not parts and label == store[kind]:
                    found.append(name)
                elif parts:
                    prefix, _, suffix = store[kind].partition(parts[0])
                    if len(label) > len(prefix) + len(suffix) and label.startswith(prefix) and label.endswith(suffix):
                        found.append(name.replace(parts[0], label[len(prefix):len(label) - len(suffix)]))
        return found

    def add_command(self, ticket, name):
        kind, value = self._command(name)
        return self.apply({"ticket": ticket, "kind": "label", "value": value, **({"on": "pr"} if kind == "pr_label" else {})})

    def remove_command(self, ticket, name):
        kind, value = self._command(name)
        return self.apply({"ticket": ticket, "kind": "unlabel", "value": value, **({"on": "pr"} if kind == "pr_label" else {})})


class TicketStore(Protocol):
    """The ticket side of a tracker: tickets by id, read and written with their own system. A GitHub tracker keeps
    pull requests, CI, reviews and merges apart (Vcs) so a second ticket system can supply the tickets.
    `attrs` are the labels or tags of a ticket, stage ones included; `at` is an ISO time. Writes return True
    when they wrote, the others nothing."""
    def issue(self, n) -> dict: ...
    """{title, body, state (open|closed), close_reason, author, updated_at, attrs, assignees}"""
    def events(self, n) -> list: ...
    """Oldest first: {kind: labeled|unlabeled|closed|reopened, value (the attr), actor, at}"""
    def comments(self, n) -> list: ...
    """Oldest first: {author, author_is_bot, author_is_factory, body, at, id?}; `id` (an integer that grows) is what the comment
    cursor reads. `author_is_factory` is true only for the factory's own login (`targets.air.bot_login`; YouTrack `bot_logins`).
    Callers that ask "did the factory write this?" read it, never `author_is_bot`, which is true for any bot"""
    def add_attr(self, n, attr): ...
    def remove_attr(self, n, *attrs): ...
    def set_stage(self, n, stage, have): ...  # `stage` replaces the stage attrs `have`
    def assign(self, n, people): ...  # the assignees become exactly `people`; [] unassigns
    def comment(self, n, body): ...
    def close(self, n): ...
    def open_blockers(self, n) -> list: ...  # ids of the open tickets blocking `n`
    def blocking(self, n) -> list: ...  # [{number, state}] of the tickets `n` blocks
    def parent(self, n): ...  # the parent's id, else None
    def children(self, n) -> list: ...  # [{number, state, not_planned, attrs}]
    def tickets_by_stage(self, attrs, open_only=False, now=None) -> list: ...  # ids of tickets holding one of `attrs`
    def create_children(self, n, children, ready) -> bool: ...  # sub-tickets {title, body}, chained, each given `ready`
    def park(self, n, check, triage) -> bool: ...  # block `n` by the open `Main is red in Air: <check>` ticket
    def can_answer(self, login) -> bool: ...  # whether `login` may answer a question the factory asked


class Vcs(Protocol):
    """Where the pull requests live; a tracker's tickets need not be on the same host."""
    def event(self, payload) -> object: ...  # the ports.system.Event of a PR payload, else None
    def ticket_for_pr(self, number, repository=None): ...


def child_links(children):
    """Each child's prerequisite indexes: the children of the repositories it is `after`, else (no `repo` anywhere)
    the previous child, which chains a plain split. An `after` entry is a repository name or the 1-based position of
    a child in the list."""
    kids = [c if isinstance(c, dict) else {"title": str(c)} for c in children]
    if not any(c.get("repo") for c in kids):
        return [[i - 1] if i else [] for i in range(len(kids))]
    return [[j for j, d in enumerate(kids) if d.get("repo") in (c.get("after") or []) or j + 1 in (c.get("after") or [])]
            for c in kids]


def child_order(links):
    """Child indexes with each prerequisite before the children that follow it (a cycle keeps its listed order)."""
    order = []
    while len(order) < len(links):
        ready = [i for i, after in enumerate(links) if i not in order and all(j in order for j in after)]
        order += ready or [i for i in range(len(links)) if i not in order][:1]
    return order


def child_attrs(child):
    """The labels a per-repository child carries: its repository and the project subsystem."""
    return ([f"repo:{child['repo']}"] if child.get("repo") else []) + (
        [f"subsystem:{child['subsystem']}"] if child.get("subsystem") else [])


class MemoryCodeHost:
    """The CI label sync's code host (gate.ci_sync) in memory: `prs` {owner/repo: [{number, branch, head, labels,
    labeled_at: {label: at}}]}, `checks` {sha: [{conclusion, at}]}; `edits` records each label write, added at `now`."""

    def __init__(self, prs=None, checks=None, now=None):
        self.prs, self.checks, self.edits = prs or {}, checks or {}, []
        self.now = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _pr(self, repository, number):
        return next(p for p in self.prs[repository] if p["number"] == number)

    def ticket_for_pr(self, number, repository=None):
        """The ticket of the factory branch of PR `number` in `repository` (None: no such PR here)."""
        pr = next((p for p in self.prs.get(repository, []) if p["number"] == number), None)
        return ticket_from_ref(pr.get("branch")) if pr else None

    def event(self, payload):
        """The Event of a payload {number, repository?, action?} naming one of `prs`, else None."""
        from ports.system import Event
        ticket = self.ticket_for_pr(payload.get("number"), payload.get("repository"))
        return None if ticket is None else Event(ticket, "pull_request", payload.get("action"), payload.get("number"),
                                                 payload.get("repository"), payload)

    def factory_prs(self, repository):
        return [{k: p[k] for k in ("number", "head", "labels")} for p in self.prs[repository]
                if ticket_from_ref(p.get("branch")) is not None]

    def head_checks(self, repository, sha):
        return list(self.checks.get(sha, []))

    def label_added_at(self, repository, number, label):
        return (self._pr(repository, number).get("labeled_at") or {}).get(label)

    def edit_pr_labels(self, repository, number, add=(), remove=()):
        pr = self._pr(repository, number)
        pr["labels"] = [x for x in pr["labels"] if x not in remove] + list(add)
        pr.setdefault("labeled_at", {}).update(dict.fromkeys(add, self.now))
        self.edits.append((repository, number, list(add), list(remove)))


class MemorySystem(DescriptorPort):
    """The tracker and code host in memory (the System contract, factory/ports/system.py): integer ids, stage in
    its own field, facts read straight from the record. `fail_on` makes the effect kind raise."""
    name = "memory"

    def __init__(self, tickets=None, fail_on=None, project=None):
        self.tickets, self.fail_on, self.project = tickets or {}, fail_on, project
        self.green_main = False  # CI at the main head is green

    def main_green(self):
        return self.green_main

    def _t(self, ticket):
        return self.tickets[ticket] if ticket in self.tickets else self.tickets[str(ticket)]  # JSON files key by string

    def ticket_for_pr(self, number, repository=None):
        return next((int(k) for k, t in self.tickets.items() if (t.get("pr") == number and (repository is None or not t.get("prs")))
                     or any(p.get("pr") == number and repository in (None, p.get("repo")) for p in t.get("prs") or [])), None)

    def get_stage(self, t):
        return t.get("stage", "none")

    def set_stage(self, t, stage):
        t["stage"] = stage

    def sweep_tickets(self, stages, open_only=False):
        return sorted(k for k, t in self.tickets.items() if self.get_stage(t) in stages
                      and not (open_only and t.get("issue_state", "open") != "open"))

    def consistency_view(self):
        """Reconcile's consistency pass over the records: a record's `labels` (else its `attrs` plus its stage's label),
        its `prs` (else its `pr`, `pr_state` and `branch`) and its `issue_state`."""
        import vocab
        tickets, orphans = [], []
        for k, t in sorted(self.tickets.items(), key=lambda kv: str(kv[0])):
            prs = t.get("prs") or ([{"pr": t["pr"], "pr_state": t.get("pr_state", "open")}]
                                   if t.get("pr") else [])
            prs = [{"number": p["pr"], "state": p["pr_state"], "branch": p.get("branch") or f"ez/{k}-x"} for p in prs]
            stage = self.get_stage(t)
            stage_label = [vocab.stage(stage)] if stage in vocab._tracker()["stages"] else []
            labels = t["labels"] if "labels" in t else list(dict.fromkeys([*t.get("attrs", []), *stage_label]))
            if t.get("issue_state", "open") == "open":
                tickets.append({"number": k, "labels": labels, "updated_at": t.get("updated_at"), "prs": prs})
            else:
                orphans += [{"number": p["number"], "ticket": k} for p in prs if p["state"] == "open"]
        return {"tickets": tickets, "orphan_prs": orphans}

    def comment_bodies(self, n, pr=None):
        return [json.loads(v) for kind, v in self._t(n).get("log", []) if kind == "comment"]

    def close_ticket(self, n):
        self._t(n)["issue_state"] = "closed"

    def blocking(self, ticket):
        return self._t(ticket).get("blocking", [])

    def not_planned(self, ticket):
        return (self.tickets.get(ticket) or self.tickets.get(str(ticket)) or {}).get("state_reason") == "not_planned"

    # --- events in, records out (ADR-0026)
    def event(self, payload):
        """The event of a payload {ticket, event_name?, action?, pr?}, else None without a ticket."""
        from ports.system import Event
        ticket = payload.get("ticket")
        if ticket is None and payload.get("pr") is not None:
            ticket = self.ticket_for_pr(payload["pr"], payload.get("repository"))
        if ticket is None:
            return None
        return Event(ticket, payload.get("event_name") or "event", payload.get("action"), payload.get("pr"),
                     payload.get("repository"), payload)

    def read(self, subject, want=frozenset()):
        """The record of a ticket, or of its PR in repository `subject.repo` (the record's `prs`, else its `pr`).
        The comments, reviews and labels the effects wrote read back with the record's own."""
        from ports.records import PrRecord, TicketRecord
        t = self._t(subject.id)
        if subject.kind == "ticket":
            written = [{"author": "factory", "author_is_bot": True, "author_is_factory": True, "body": json.loads(v), "at": None}
                       for k, v in t.get("log", []) if k == "comment"]
            return TicketRecord(subject.id, t.get("title", ""), t.get("body", ""), t.get("issue_state", "open"),
                                t.get("state_reason"), t.get("author"), t.get("updated_at"), list(t.get("attrs", [])),
                                list(t.get("assignees", [])), comments=list(t.get("comments", [])) + written,
                                blockers_open=list(t.get("blocked_by", [])),
                                children=list(t.get("children", [])) if "children" in want else None)
        if subject.kind == "pr":
            prs = t.get("prs") or ([{"repo": subject.repo, "pr": t["pr"], "head": t.get("head")}] if t.get("pr") else [])
            p = next((p for p in prs if subject.repo in (None, p.get("repo"))), None)
            if p is None:
                return None
            return PrRecord(p.get("repo") or "primary", p["pr"], p.get("head"), branch=t.get("branch"),
                            merged=p.get("pr_state", t.get("pr_state")) == "merged", labels=list(t.get("pr_labels", [])),
                            reviews=list(t.get("reviews", [])) + [r for r in t.get("posted", []) if r.get("repo") in (None, p.get("repo"))],
                            ci=p.get("ci", t.get("ci", "none")), behind=bool(t.get("behind")) if "behind" in want else None)
        raise ValueError(f"read: unknown subject {subject.kind}")

    def find(self, query):
        if query.kind == "children":
            return list(self._t(query.of).get("children", []))
        if query.kind == "stage":
            return self.sweep_tickets(query.values, query.open_only)
        raise ValueError(f"find: unknown query {query.kind}")

    def facts(self, ticket):
        t = self._t(ticket)
        base = {"head": None, "blockers_open": 0, "focus_missing": [], "round_complete": False,
                "round_verdict": None, "round_head": None, "attrs": [], "agent_runs": 0, "run_cap": 8, "handed_to_fix": False,
                "fix_rounds": 0, "fix_cap": 3, "ci": "none", "due": False, "conflict": False,
                "pr_state": "none", "risk": "high", "behind": False, "answered": False, "updated_at": None,
                "review_labels": [], "assignees": []}
        t = {**t, "fix_rounds": fix_round_count(t["reviews"])} if "reviews" in t and "fix_rounds" not in t else t
        if t.get("prs"):  # one PR per repository: the record lists them, in the ticket's repository order
            t = {**t, **combine_prs([{**PR_DEFAULTS, **p} for p in t["prs"]])}
        return {**base, **{k: v for k, v in t.items() if k in base or k in ("title", "body", "branch", "reviews", "pr", "ci_failure", "author", "waiting_reviewers", "agent_notes", "pr_labels", "prs", "heads")}, "stage": self.get_stage(t),
                "seen": list(t.get("seen", [])), "comments": t.get("comments", [])}

    def stale_facts(self, ticket):
        """facts plus the stale-sweep fields a record may set (since, command, command_at, commit_at, ci_label);
        the recover comments come from the log."""
        t = self._t(ticket)
        sent = [json.loads(v) for k, v in t.get("log", []) if k == "comment"]
        return {**self.facts(ticket), **{k: t.get(k) for k in ("since", "stage_at", "command", "command_at", "commit_at", "ci_label", "command_pending", "pr_state", "issue_state", "label_at") if k in t},
                "recover_comments": [c for c in sent if c.startswith(("[factory] recover:", "[factory] reconcile:", "[factory] end failed:"))]}

    def apply(self, effect):
        import effects
        if effect["kind"] == self.fail_on:
            raise RuntimeError(f"effect failed: {effect['kind']}")
        return effects.apply(self, effect)

    # --- the effect handlers: (system, effect, ctx) -> wrote
    def _fx_stage(self, effect, ctx):  # `stage` and `seen`: the stage field and the event ids seen
        t, kind, value = self._t(effect["ticket"]), effect["kind"], effect["value"]
        seen, wrote = t.setdefault("seen", []), False
        if kind == "stage" and self.get_stage(t) != value:
            self.set_stage(t, value)
            wrote = True
        ev = effect.get("event") if kind == "stage" else value
        if ev is not None and ev not in seen:
            seen.append(ev)
            wrote = True
        return wrote

    def _fx_park(self, effect, ctx):  # open or reuse `Main is red in Air: <check>`, block this ticket by it, wait in ready
        t, value = self._t(effect["ticket"]), effect["value"]
        title = f"Main is red in Air: {value}"
        red = next((i for i, x in self.tickets.items() if x.get("title") == title and x.get("state", "open") == "open"), None)
        wrote = red is None
        if wrote:
            red = f"red-{value}"
            self.tickets[red] = {"title": title, "stage": "triage", "state": "open"}
        if red not in t.setdefault("blocked_by", []):
            t["blocked_by"].append(red)
            t["blockers_open"] = t.get("blockers_open", 0) + 1
            wrote = True
        return wrote

    def _fx_set_field(self, effect, ctx):
        fields, value = self._t(effect["ticket"]).setdefault("fields", {}), effect["value"]
        if all(fields.get(k) == v for k, v in value.items()):
            return False
        fields.update(value)
        return True

    def _fx_assign(self, effect, ctx):
        t, kind, value = self._t(effect["ticket"]), effect["kind"], effect["value"]
        if kind == "assign":
            people = value if isinstance(value, list) else [value]
            if t.get("assignees", []) == people:
                return False
            t["assignees"] = people
            t.setdefault("log", []).append([kind, json.dumps(value, sort_keys=True)])
            return True
        people = t.get("assignees", [])
        if not people:
            return False
        t["assignees"] = []
        t.setdefault("log", []).append([kind, json.dumps(people, sort_keys=True)])
        return True

    def _fx_merge(self, effect, ctx):  # the other repositories first, the primary last
        import effects
        t = self._t(effect["ticket"])
        if len(t.get("prs") or []) <= 1:
            return self._fx_log(effect, ctx)
        todo = [p for p in effects.primary_last(t["prs"]) if p.get("pr_state") != "merged"]
        for p in todo:
            p["pr_state"] = "merged"
            t.setdefault("log", []).append(["merge", json.dumps({"repo": p["repo"], "head": (effect.get("heads") or {}).get(p["repo"])},
                                                                 sort_keys=True)])
        return bool(todo)

    def _fx_unlabel(self, effect, ctx):
        return self._drop(self._t(effect["ticket"]), effect["value"])

    def _fx_log(self, effect, ctx):  # label, comment, review, create-issues, merge, join: a set of applied values
        t, kind, value = self._t(effect["ticket"]), effect["kind"], effect.get("value")
        log = t.setdefault("log", [])
        entry = [kind, json.dumps(value, sort_keys=True)]
        if entry in log:
            return False
        log.append(entry)
        if kind == "label" and value not in t.setdefault("attrs", []):
            t["attrs"].append(value)
        if kind == "label" and effect.get("on") == "pr" and value not in t.setdefault("pr_labels", []):
            t["pr_labels"].append(value)
        if kind == "review":  # pinned to the head it was posted at, as a code host keeps it
            t.setdefault("posted", []).append({"agent": True, "focus": effect.get("focus"), "commit_id": t.get("head"),
                                               "verdict": "approve" if str(value).startswith("approve") else "changes"})
        return True

    def _drop(self, t, value):
        log = t.setdefault("log", [])
        attrs = t.get("attrs", [])
        if value.endswith("*"):  # every label with the prefix
            gone = [x for x in log if x[0] == "label" and json.loads(x[1]).startswith(value[:-1])]
            for x in gone:
                log.remove(x)
            t["attrs"] = [x for x in attrs if not x.startswith(value[:-1])]
            if "pr_labels" in t:
                t["pr_labels"] = [x for x in t["pr_labels"] if not x.startswith(value[:-1])]
            return bool(gone) or len(t["attrs"]) != len(attrs)
        entry = ["label", json.dumps(value, sort_keys=True)]
        present = entry in log
        if present:
            log.remove(entry)
        t["attrs"] = [x for x in attrs if x != value]
        if "pr_labels" in t:
            t["pr_labels"] = [x for x in t["pr_labels"] if x != value]
        return present or len(t["attrs"]) != len(attrs)

    effects = (
        Effect("stage", "ticket", _fx_stage), Effect("seen", "ticket", _fx_stage),
        Effect("park-behind-main-red", "ticket", _fx_park), Effect("set-field", "ticket", _fx_set_field),
        Effect("assign", "ticket", _fx_assign), Effect("unassign", "ticket", _fx_assign),
        Effect("unlabel", "ticket", _fx_unlabel), Effect("unlabel", "pr", _fx_unlabel),
        Effect("review", "pr", _fx_log, remote=True), Effect("update-branch", "pr", _fx_log, remote=True),
        Effect("merge", "pr", _fx_merge, remote=True),
        Effect("*", "ticket", _fx_log),
    )


class TagTracker(MemorySystem):
    """String ids (PROJ-1), stage held as a `stage:<name>` tag among the other tags, as in YouTrack."""

    def get_stage(self, t):
        return next((x[6:] for x in t.get("tags", []) if x.startswith("stage:")), "none")

    def set_stage(self, t, stage):
        t["tags"] = [x for x in t.get("tags", []) if not x.startswith("stage:")] + [f"stage:{stage}"]


class MemoryTicketStore:
    """A TicketStore in memory, the contract's reference (factory/tests/test_gate_github.py). Ids are ints."""

    def __init__(self, can_write=()):
        self.tickets, self.writers, self.actor = {}, set(can_write), "factory"

    def add(self, title="", body="", attrs=(), author="author", assignees=(), comments=(), events=(), parent=None):
        n = max(self.tickets, default=0) + 1
        self.tickets[n] = {"title": title, "body": body, "state": "open", "close_reason": None, "author": author,
                           "updated_at": None, "attrs": list(attrs), "assignees": list(assignees),
                           "events": list(events), "comments": list(comments), "blocked_by": [], "parent": parent}
        return n

    def issue(self, n):
        t = self.tickets[n]
        return {k: (list(t[k]) if isinstance(t[k], list) else t[k]) for k in (
            "title", "body", "state", "close_reason", "author", "updated_at", "attrs", "assignees")}

    def events(self, n):
        return list(self.tickets[n]["events"])

    def comments(self, n):
        return list(self.tickets[n]["comments"])

    def edit_attrs(self, n, add=(), remove=()):
        t = self.tickets[n]
        for a in remove:
            t["attrs"] = [x for x in t["attrs"] if x != a]
            t["events"].append({"kind": "unlabeled", "value": a, "actor": self.actor, "at": None})
        for a in add:
            if a not in t["attrs"]:
                t["attrs"].append(a)
            t["events"].append({"kind": "labeled", "value": a, "actor": self.actor, "at": None})
        return True

    def add_attr(self, n, attr):
        return self.edit_attrs(n, [attr])

    def remove_attr(self, n, *attrs):
        return self.edit_attrs(n, remove=attrs)

    def set_stage(self, n, stage, have):
        return self.edit_attrs(n, [stage] if stage not in have else [], [x for x in have if x != stage])

    def assign(self, n, people):
        t = self.tickets[n]
        if set(t["assignees"]) == set(people):
            return False
        t["assignees"] = sorted(people)
        return True

    def comment(self, n, body):
        self.tickets[n]["comments"].append({"author": self.actor, "author_is_bot": True, "author_is_factory": True, "body": body, "at": None})

    def close(self, n, at=None):
        t = self.tickets[n]
        t["state"], t["close_reason"] = "closed", "completed"
        t["closed_at"] = at or datetime.now(timezone.utc)
        t["events"].append({"kind": "closed", "value": None, "actor": self.actor, "at": None})

    def _open(self, ids):
        return [i for i in ids if self.tickets[i]["state"] == "open"]

    def open_blockers(self, n):
        return self._open(self.tickets[n]["blocked_by"])

    def blocking(self, n):
        return [{"number": i, "state": t["state"]} for i, t in self.tickets.items() if n in t["blocked_by"]]

    def parent(self, n):
        return self.tickets[n]["parent"]

    def children(self, n):
        return [{"number": i, "state": t["state"], "not_planned": t["state"] == "closed" and t["close_reason"] == "not_planned",
                 "attrs": list(t["attrs"])} for i, t in self.tickets.items() if t["parent"] == n]

    def tickets_by_stage(self, attrs, open_only=False, now=None):
        """Open tickets and, unless `open_only`, those closed within `CLOSED_HOURS` of `now`."""
        since = (now or datetime.now(timezone.utc)) - timedelta(hours=CLOSED_HOURS)

        def listed(t):
            if t["state"] == "open":
                return True
            return not open_only and (t.get("closed_at") or since) >= since

        return sorted(i for i, t in self.tickets.items() if set(attrs) & set(t["attrs"]) and listed(t))

    def create_children(self, n, children, ready):
        wrote, ids, links = False, [], child_links(children)
        for c, after in zip(children, links):
            c = c if isinstance(c, dict) else {"title": str(c), "body": ""}
            old = next((i for i, t in self.tickets.items() if t["parent"] == n and t["title"] == c["title"]), None)
            if old is None:
                old = self.add(c["title"], c.get("body", ""), [ready, *child_attrs(c)], parent=n)
                self.tickets[n]["blocked_by"].append(old)
                wrote = True
            ids.append(old)
        for i, after in zip(ids, links):  # link once every child exists: `after` may name a later child
            for j in after:
                if ids[j] not in self.tickets[i]["blocked_by"]:
                    self.tickets[i]["blocked_by"].append(ids[j])
                    wrote = True
        return wrote

    def park(self, n, check, triage):
        title = f"Main is red in Air: {check}"
        red = next((i for i, t in self.tickets.items() if t["title"] == title and t["state"] == "open"), None)
        if red is None:
            red = self.add(title, attrs=[triage])
        if red in self.tickets[n]["blocked_by"]:
            return False
        self.tickets[n]["blocked_by"].append(red)
        return True

    def can_answer(self, login):
        return login in self.writers

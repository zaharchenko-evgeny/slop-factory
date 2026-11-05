"""The tracker port of the gate (factory/gate.py) and two in-memory fakes.

`Tracker.facts(ticket)` returns a dict with: stage ("none" when unset), head, blockers_open (int),
focus_missing (list), round_complete (bool), round_verdict (changes|approve|None), round_head, fix_rounds, fix_cap, ci, due, seen (event ids).
`Tracker.apply(effect)` takes {ticket, kind, value, event?}, reads first and writes only when the target
state differs; it returns True when it wrote. The stage effect also records `event` in `seen`; so does the `seen` effect, which apply appends to an
outcome without a stage effect.
"""
import json
import re
from typing import Protocol


def ticket_from_ref(ref):
    """The ticket id of a branch `ez/<ticket>-<slug>` or a PR title starting `<ticket> `: digits give an int
    (GitHub issue), an opaque id such as `INFRA-123` stays a string. None when `ref` names no ticket."""
    m = re.match(r"(?:refs/heads/)?ez/([A-Za-z][A-Za-z0-9]*-\d+|\d+)-", ref or "") or re.match(r"#?([A-Za-z][A-Za-z0-9]*-\d+|\d+)(?=[ :])", ref or "")
    return None if not m else int(m[1]) if m[1].isdigit() else m[1]


def ticket_repositories(body, names, primary):
    """The repositories a ticket changes (ADR-0017): its `repositories: a, b` body line, else the primary one. The
    primary comes first: Air wakes a ticket's runs only from its PR, and its merge, the last, wakes `finish`."""
    m = re.search(r"(?m)^repositories:[ \t]*(.+)$", body or "")
    if not m:
        return [primary]
    want = list(dict.fromkeys(x for x in re.split(r"[,\s]+", m[1]) if x))
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
               "pending_rereviews": [], "pr_labels": [], "fix_rounds": 0, "pr_state": "none",
               "handed_to_fix": False, "behind": False}


def combine_prs(prs):
    """One ticket's PR facts over its PRs (`repo` plus the PR_DEFAULTS keys), in the ticket's repository order.
    The first PR gives head, pr and branch. The ticket is approved only when every PR's round is complete with
    approve at its own head; one PR with changes completes the round. One PR is returned as it is."""
    if len(prs) == 1:
        return dict(prs[0])
    union = lambda key: list(dict.fromkeys(x for p in prs for x in p.get(key) or []))
    cis, states = [p["ci"] for p in prs], [p["pr_state"] for p in prs]
    changes = any(p["round_complete"] and p["round_verdict"] == "changes" for p in prs)
    complete = changes or all(p["round_complete"] for p in prs)
    moved = next((f"{p['repo']}:{p['round_head']}" for p in prs if p["round_head"] not in (None, p["head"])), None)
    reviewed = all(p["round_head"] is not None for p in prs)
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
                p["round_verdict"] == "approve" for p in prs) else None,
            "round_head": moved or (prs[0]["head"] if reviewed else None),
            "handed_to_fix": any(p["handed_to_fix"] for p in prs), "fix_rounds": max(p["fix_rounds"] for p in prs),
            "reviews": [{**r, "repo": p["repo"]} for p in prs for r in p["reviews"]],
            **{k: union(k) for k in ("requested_reviewers", "focus_missing", "ignored_reviews", "waiting_reviewers",
                                     "pending_rereviews", "pr_labels")}}


class Tracker(Protocol):
    def facts(self, ticket) -> dict: ...
    def apply(self, effect: dict) -> bool: ...


def fix_round_count(reviews, since=""):
    """Rounds a fix answers: an agent change request, per head and dispute rereview, since the person's last
    `factory:ready`, with at least one blocking finding (id `B<n>`). A round with none (a verdict bug,
    environment noise) is not counted against `limits.fix_rounds`."""
    return len({(r["commit_id"], r.get("rereview")) for r in reviews
                if r.get("agent") and r.get("verdict") == "changes" and (r.get("submitted_at") or "") >= since
                and any(str(i).startswith("B") for i in r.get("findings", []))})


class UnsupportedStore(ValueError):
    """A descriptor store this tracker cannot read or write (a `field` or `tag` store before YouTrack, #163)."""


# The facts a PR-fact state store (`store: {pr: <fact>}`) holds on.
PR_FACTS = {"open": lambda f: f.get("pr_state") == "open", "merged": lambda f: f.get("pr_state") == "merged",
            "ci-green": lambda f: f.get("ci") == "green", "ci-red": lambda f: f.get("ci") == "red",
            "conflicting": lambda f: bool(f.get("conflict")),
            "approved": lambda f: bool(f.get("round_complete")) and f.get("round_verdict") == "approve"}


class DescriptorPort:
    """States and commands by name, through the `store` each has in factory.project.yaml (ADR-0016). A tracker
    mixes it in and sets `project`; reads go through its `facts`, writes through its `apply`."""
    project = None

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
        if kind not in ("label", "pr_label"):
            raise UnsupportedStore(f"command {name}: this tracker has no {kind} store")
        return kind, value

    def state(self, ticket):
        """The ticket's state held in a label store, else `none`."""
        stage = self.facts(ticket)["stage"]
        states = self._entries("states")
        return stage if stage in states and "label" in (states[stage].get("store") or {}) else "none"

    def pr_states(self, ticket):
        """The PR-fact states (`store: {pr: ...}`) that hold for the ticket's PR."""
        facts = self.facts(ticket)
        return [name for name, entry in self._entries("states").items()
                if "pr" in (entry.get("store") or {}) and PR_FACTS[entry["store"]["pr"]](facts)]

    def set_state(self, ticket, name, event=None):
        kind, store = self._state_store(name)
        if kind == "pr":
            raise ValueError(f"state {name} is a PR fact ({store['pr']}): the code host sets it, not the gate")
        if kind != "label":
            raise UnsupportedStore(f"state {name}: this tracker has no {kind} store")
        return self.apply({"ticket": ticket, "kind": "stage", "value": name,
                           **({"event": event} if event is not None else {})})

    def commands(self, ticket):
        """Command references present on the ticket (label stores) and its PR (pr_label stores), such as
        `implement-mid` for the template `implement-<tier>`."""
        import workflow
        facts, found = self.facts(ticket), []
        held = {"label": facts.get("attrs") or [], "pr_label": facts.get("pr_labels") or []}
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
    pull requests, CI, reviews and merges apart (CodeHost) so a second ticket system can supply the tickets.
    `attrs` are the labels or tags of a ticket, stage ones included; `at` is an ISO time. Writes return True
    when they wrote, the others nothing."""
    def issue(self, n) -> dict: ...
    """{title, body, state (open|closed), close_reason, author, updated_at, attrs, assignees}"""
    def events(self, n) -> list: ...
    """Oldest first: {kind: labeled|unlabeled|closed|reopened, value (the attr), actor, at}"""
    def comments(self, n) -> list: ...
    """Oldest first: {author, author_is_bot, body, at}"""
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


class CodeHost(Protocol):
    """Where the pull requests live; a tracker's tickets need not be on the same host."""
    def ticket_for_pr(self, number, repository=None): ...


class MemoryTracker(DescriptorPort):
    """Integer ids, stage in its own field. `fail_on` makes the effect kind raise."""

    def __init__(self, tickets=None, fail_on=None, project=None):
        self.tickets, self.fail_on, self.project = tickets or {}, fail_on, project
        self.green_main = False  # the latest quality-gates run on the main head passed

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

    def blocking(self, ticket):
        return self._t(ticket).get("blocking", [])

    def not_planned(self, ticket):
        return (self.tickets.get(ticket) or self.tickets.get(str(ticket)) or {}).get("state_reason") == "not_planned"

    def facts(self, ticket):
        t = self._t(ticket)
        base = {"head": None, "blockers_open": 0, "focus_missing": [], "round_complete": False,
                "round_verdict": None, "round_head": None, "attrs": [], "agent_runs": 0, "run_cap": 8, "handed_to_fix": False,
                "pending_rereviews": [], "fix_rounds": 0, "fix_cap": 3, "ci": "none", "due": False, "conflict": False,
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
        return {**self.facts(ticket), **{k: t.get(k) for k in ("since", "stage_at", "command", "command_at", "commit_at", "ci_label", "command_pending", "pr_state", "issue_state") if k in t},
                "recover_comments": [c for c in sent if c.startswith(("[factory] recover:", "[factory] reconcile:"))]}

    def apply(self, effect):
        if effect["kind"] == self.fail_on:
            raise RuntimeError(f"effect failed: {effect['kind']}")
        t, kind, value = self._t(effect["ticket"]), effect["kind"], effect["value"]
        if kind in ("stage", "seen"):
            seen, wrote = t.setdefault("seen", []), False
            if kind == "stage" and self.get_stage(t) != value:
                self.set_stage(t, value)
                wrote = True
            ev = effect.get("event") if kind == "stage" else value
            if ev is not None and ev not in seen:
                seen.append(ev)
                wrote = True
            return wrote
        if kind == "park-behind-main-red":  # open or reuse `Main is red in Air: <check>`, block this ticket by it, wait in ready
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
        if kind == "set-field":
            fields = t.setdefault("fields", {})
            if all(fields.get(k) == v for k, v in value.items()):
                return False
            fields.update(value)
            return True
        if kind == "assign":
            people = value if isinstance(value, list) else [value]
            if t.get("assignees", []) == people:
                return False
            t["assignees"] = people
            t.setdefault("log", []).append([kind, json.dumps(value, sort_keys=True)])
            return True
        if kind == "unassign":
            people = t.get("assignees", [])
            if not people:
                return False
            t["assignees"] = []
            t.setdefault("log", []).append([kind, json.dumps(people, sort_keys=True)])
            return True
        if kind == "merge" and len(t.get("prs") or []) > 1:  # the other repositories first, the primary last
            todo = [p for p in t["prs"][1:] + t["prs"][:1] if p.get("pr_state") != "merged"]
            for p in todo:
                p["pr_state"] = "merged"
                t.setdefault("log", []).append([kind, json.dumps({"repo": p["repo"], "head": (effect.get("heads") or {}).get(p["repo"])},
                                                                  sort_keys=True)])
            return bool(todo)
        # label, unlabel, comment, review, create-issues, merge, join: a set of applied values
        log = t.setdefault("log", [])
        entry = [kind, json.dumps(value, sort_keys=True)]
        if kind == "unlabel":
            return self._drop(t, value)
        if entry in log:
            return False
        log.append(entry)
        if kind == "label" and value not in t.setdefault("attrs", []):
            t["attrs"].append(value)
        if kind == "label" and effect.get("on") == "pr" and value not in t.setdefault("pr_labels", []):
            t["pr_labels"].append(value)
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


class TagTracker(MemoryTracker):
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
        self.tickets[n]["comments"].append({"author": self.actor, "author_is_bot": True, "body": body, "at": None})

    def close(self, n):
        t = self.tickets[n]
        t["state"], t["close_reason"] = "closed", "completed"
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
        return sorted(i for i, t in self.tickets.items() if set(attrs) & set(t["attrs"]) and (t["state"] == "open" or not open_only))

    def create_children(self, n, children, ready):
        wrote, prev = False, None
        for c in children:
            c = c if isinstance(c, dict) else {"title": str(c), "body": ""}
            old = next((i for i, t in self.tickets.items() if t["parent"] == n and t["title"] == c["title"]), None)
            if old is None:
                old = self.add(c["title"], c.get("body", ""), [ready], parent=n)
                self.tickets[n]["blocked_by"].append(old)
                wrote = True
            if prev is not None and prev not in self.tickets[old]["blocked_by"]:
                self.tickets[old]["blocked_by"].append(prev)
                wrote = True
            prev = old
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

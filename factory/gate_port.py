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


class CodeHost(Protocol):
    """Where the pull requests live; a tracker's tickets need not be on the same host."""
    def ticket_for_pr(self, number): ...


class MemoryTracker(DescriptorPort):
    """Integer ids, stage in its own field. `fail_on` makes the effect kind raise."""

    def __init__(self, tickets=None, fail_on=None, project=None):
        self.tickets, self.fail_on, self.project = tickets or {}, fail_on, project

    def _t(self, ticket):
        return self.tickets[ticket] if ticket in self.tickets else self.tickets[str(ticket)]  # JSON files key by string

    def ticket_for_pr(self, number):
        return next((int(k) for k, t in self.tickets.items() if t.get("pr") == number), None)

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
        return {**base, **{k: v for k, v in t.items() if k in base or k in ("title", "body", "branch", "reviews", "pr", "ci_failure", "author", "waiting_reviewers", "agent_notes", "pr_labels")}, "stage": self.get_stage(t),
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

    # Small issue operations used by deploy's end-to-end canary. They keep the
    # deploy tests on the same in-memory ticket records as the gate tests.
    def canary_tickets(self):
        import vocab
        return [n for n, t in self.tickets.items()
                if vocab.issue_label("canary") in t.get("labels", []) and t.get("state", "open") != "closed"]

    def close_issue(self, ticket, reason):
        self._t(ticket)["state"] = "closed"

    def add_label(self, ticket, label):
        labels = self._t(ticket).setdefault("labels", [])
        if label not in labels:
            labels.append(label)

    def create_issue(self, title, body, risk="low", stage="triage"):
        number = max((int(n) for n in self.tickets), default=0) + 1
        self.tickets[number] = {"title": title, "body": body, "risk": risk, "stage": stage,
                                "labels": [f"risk:{risk}", f"factory:{stage}"], "state": "open"}
        return number

    def add_to_map(self, ticket):
        self._t(ticket)["parent"] = "map"

    def canary_status(self, ticket):
        t = self._t(ticket)
        return {"stage": t.get("stage", "none"), "summary": t.get("last_summary", ""),
                "error": bool(t.get("node_error"))}

    def record_node_error(self, ticket):
        import vocab
        t = self._t(ticket)
        if vocab.issue_label("canary") in t.get("labels", []):
            t["node_error"] = True
            t["last_summary"] = "[factory] A factory node reported an execution error."


class TagTracker(MemoryTracker):
    """String ids (PROJ-1), stage held as a `stage:<name>` tag among the other tags, as in YouTrack."""

    def get_stage(self, t):
        return next((x[6:] for x in t.get("tags", []) if x.startswith("stage:")), "none")

    def set_stage(self, t, stage):
        t["tags"] = [x for x in t.get("tags", []) if not x.startswith("stage:")] + [f"stage:{stage}"]

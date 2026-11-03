"""The tracker port of the gate (factory/gate.py) and two in-memory fakes.

`Tracker.facts(ticket)` returns a dict with: stage ("none" when unset), head, blockers_open (int),
focus_missing (list), round_complete (bool), round_verdict (changes|approve|None), round_head, fix_rounds, fix_cap, ci, due, seen (event ids).
`Tracker.apply(effect)` takes {ticket, kind, value, event?}, reads first and writes only when the target
state differs; it returns True when it wrote. The stage effect also records `event` in `seen`; so does the `seen` effect, which apply appends to an
outcome without a stage effect.
"""
import json
from typing import Protocol


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


class MemoryTracker:
    """Integer ids, stage in its own field. `fail_on` makes the effect kind raise."""

    def __init__(self, tickets=None, fail_on=None):
        self.tickets, self.fail_on = tickets or {}, fail_on

    def _t(self, ticket):
        return self.tickets[ticket] if ticket in self.tickets else self.tickets[str(ticket)]  # JSON files key by string

    def ticket_for_pr(self, number):
        return next((int(k) for k, t in self.tickets.items() if t.get("pr") == number), None)

    def get_stage(self, t):
        return t.get("stage", "none")

    def set_stage(self, t, stage):
        t["stage"] = stage

    def blocking(self, ticket):
        return self._t(ticket).get("blocking", [])

    def not_planned(self, ticket):
        return (self.tickets.get(ticket) or self.tickets.get(str(ticket)) or {}).get("state_reason") == "not_planned"

    def facts(self, ticket):
        t = self._t(ticket)
        base = {"head": None, "blockers_open": 0, "focus_missing": [], "round_complete": False,
                "round_verdict": None, "round_head": None, "attrs": [], "agent_runs": 0, "run_cap": 8, "handed_to_fix": False,
                "pending_rereviews": [], "fix_rounds": 0, "fix_cap": 3, "ci": "none", "due": False, "conflict": False,
                "pr_state": "none", "risk": "high", "behind": False, "answered": False, "updated_at": None}
        t = {**t, "fix_rounds": fix_round_count(t["reviews"])} if "reviews" in t and "fix_rounds" not in t else t
        return {**base, **{k: v for k, v in t.items() if k in base or k in ("title", "body", "branch", "reviews", "pr", "ci_failure", "author", "waiting_reviewers")}, "stage": self.get_stage(t),
                "seen": list(t.get("seen", []))}

    def stale_facts(self, ticket):
        """facts plus the stale-sweep fields a record may set (since, command, command_at, commit_at, ci_label);
        the recover comments come from the log."""
        t = self._t(ticket)
        sent = [json.loads(v) for k, v in t.get("log", []) if k == "comment"]
        return {**self.facts(ticket), **{k: t.get(k) for k in ("since", "stage_at", "command", "command_at", "commit_at", "ci_label", "command_pending", "pr_state", "issue_state") if k in t},
                "recover_comments": [c for c in sent if c.startswith("[factory] recover:")]}

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
        return True

    def _drop(self, t, value):
        log = t.setdefault("log", [])
        attrs = t.get("attrs", [])
        if value.endswith("*"):  # every label with the prefix
            gone = [x for x in log if x[0] == "label" and json.loads(x[1]).startswith(value[:-1])]
            for x in gone:
                log.remove(x)
            t["attrs"] = [x for x in attrs if not x.startswith(value[:-1])]
            return bool(gone) or len(t["attrs"]) != len(attrs)
        entry = ["label", json.dumps(value, sort_keys=True)]
        present = entry in log
        if present:
            log.remove(entry)
        t["attrs"] = [x for x in attrs if x != value]
        return present or len(t["attrs"]) != len(attrs)


class TagTracker(MemoryTracker):
    """String ids (PROJ-1), stage held as a `stage:<name>` tag among the other tags, as in YouTrack."""

    def get_stage(self, t):
        return next((x[6:] for x in t.get("tags", []) if x.startswith("stage:")), "none")

    def set_stage(self, t, stage):
        t["tags"] = [x for x in t.get("tags", []) if not x.startswith("stage:")] + [f"stage:{stage}"]

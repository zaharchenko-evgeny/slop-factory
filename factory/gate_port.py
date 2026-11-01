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


class MemoryTracker:
    """Integer ids, stage in its own field. `fail_on` makes the effect kind raise."""

    def __init__(self, tickets=None, fail_on=None):
        self.tickets, self.fail_on = tickets or {}, fail_on

    def _t(self, ticket):
        return self.tickets[ticket] if ticket in self.tickets else self.tickets[str(ticket)]  # JSON files key by string

    def get_stage(self, t):
        return t.get("stage", "none")

    def set_stage(self, t, stage):
        t["stage"] = stage

    def facts(self, ticket):
        t = self._t(ticket)
        base = {"head": None, "blockers_open": 0, "focus_missing": [], "round_complete": False,
                "round_verdict": None, "round_head": None, "attrs": [], "agent_runs": 0, "run_cap": 8, "handed_to_fix": False, "fix_rounds": 0, "fix_cap": 3, "ci": "none", "due": False}
        return {**base, **{k: v for k, v in t.items() if k in base or k in ("title", "body", "branch", "reviews", "pr")}, "stage": self.get_stage(t),
                "seen": list(t.get("seen", []))}

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
        if kind == "set-field":
            fields = t.setdefault("fields", {})
            if all(fields.get(k) == v for k, v in value.items()):
                return False
            fields.update(value)
            return True
        # label, unlabel, comment, review, create-issues, merge, join: a set of applied values
        log = t.setdefault("log", [])
        entry = [kind, json.dumps(value, sort_keys=True)]
        if kind == "unlabel":
            return self._drop(t, value)
        if entry in log:
            return False
        log.append(entry)
        return True

    def _drop(self, t, value):
        log = t.setdefault("log", [])
        if value.endswith("*"):  # every label with the prefix
            gone = [x for x in log if x[0] == "label" and json.loads(x[1]).startswith(value[:-1])]
            for x in gone:
                log.remove(x)
            return bool(gone)
        entry = ["label", json.dumps(value, sort_keys=True)]
        if entry not in log:
            return False
        log.remove(entry)
        return True


class TagTracker(MemoryTracker):
    """String ids (PROJ-1), stage held as a `stage:<name>` tag among the other tags, as in YouTrack."""

    def get_stage(self, t):
        return next((x[6:] for x in t.get("tags", []) if x.startswith("stage:")), "none")

    def set_stage(self, t, stage):
        t["tags"] = [x for x in t.get("tags", []) if not x.startswith("stage:")] + [f"stage:{stage}"]

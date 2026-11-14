"""Shared fixtures of the snapshot tests: a read-only fake tracker, facts, and the engine's loaded fixture teams."""
import contextlib
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
import nodes as nodes_mod  # noqa: E402
from ports.records import TicketRecord  # noqa: E402
from ui import sources  # noqa: E402

FIXTURES = ROOT / "factory/tests/fixtures"
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
WRITES = {"apply", "claim", "release", "comment", "close", "assign", "add_attr", "remove_attr", "set_stage",
          "create_children", "park", "add_command", "remove_command", "set_state", "provision"}


@contextlib.contextmanager
def team(name):
    """The loaded (config, nodes) of fixture team `name` (`v2`, `slop`: factory.yaml)."""
    before = cfg._team
    cfg.set_team(FIXTURES / name)
    try:
        yield cfg.load(), nodes_mod.load()
    finally:
        cfg._team = before


def facts(**kw):
    base = {"stage": "ready", "attrs": [], "assignees": [], "blockers_open": 0, "head": None, "pr": None,
            "mergeable": None, "conflict": False, "ci": "none", "reviews": [], "requested_reviewers": [],
            "focus_missing": [], "round_complete": False, "round_verdict": None, "round_head": None,
            "ignored_reviews": [], "waiting_reviewers": [], "fix_rounds": 0, "fix_cap": 3, "due": False, "seen": [],
            "pr_state": "none", "agent_runs": [], "run_cap": 8, "handed_to_fix": False, "risk": "high",
            "behind": False, "issue_state": "open", "updated_at": None, "event_actor": None, "title": "T",
            "author": "a", "branch": None, "comments": [], "pr_labels": []}
    return {**base, **kw}


def labeled(attr, at, actor="bot"):
    return {"kind": "labeled", "value": attr, "actor": actor, "at": at}


class FakeTracker:
    """A tracker that only reads: any write method raises, every call is recorded."""

    def __init__(self, tickets=None, labeled_items=(), fail=None, repo="o/r", poll_fails=False):
        self.tickets = tickets or {}  # {id: {"facts": {...}, "events": [...]}}
        self.labeled_items, self.fail, self.repo, self.poll_fails = list(labeled_items), fail, repo, poll_fails
        self.calls = []

    def sweep_tickets(self, stages, open_only=False, now=None):
        self.calls.append("sweep_tickets")
        if self.fail:
            raise RuntimeError(self.fail)
        return [t for t, v in self.tickets.items() if v["facts"]["stage"] in stages]

    def facts(self, ticket):
        self.calls.append("facts")
        return self.tickets[ticket]["facts"]

    def read(self, subject, want=frozenset()):
        self.calls.append("read")
        return TicketRecord(subject.id, events=self.tickets[subject.id].get("events", []))

    def find(self, query):
        self.calls.append(f"find:{query.kind}")
        if self.poll_fails:
            raise RuntimeError("rate limited")
        if query.kind == "labeled":
            return [i for i in self.labeled_items if (("pr" if i["pr"] else "issue"), i["label"]) in query.values]
        return []

    def __getattr__(self, name):
        if name in WRITES:
            raise AssertionError(f"the snapshot called a write: {name}")
        raise AttributeError(name)


def item(number, label, event=1, pr=False, branch=None):
    return {"number": number, "label": label, "pr": pr, "branch": branch, "event": event}


def source(tracker, identity=("github-issues", "o/r"), envs=("engine",), repository="o/r"):
    return sources.Source(identity, list(envs), tracker, repository)

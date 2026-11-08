"""The `tracker` and `vcs` slots (ADR-0026): a system the gate reads records from and writes effects to.

Events in, records out, effects: `event(payload)` turns a trigger payload into an `Event`, `read(subject, want)`
returns a record (factory/ports/records.py) and `find(query)` lists ids or child records. The core derives every
fact from records (factory/derive.py) and dispatches every write through `effects`, keyed by `(kind, target)`
(factory/effects.py), wrapped in the factory's policies.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

TARGETS = ("ticket", "pr", "repo")


@dataclass(frozen=True)
class Effect:
    """One effect a system can apply. `apply` is a `"module:function"` or a callable `(system, effect, ctx) -> bool`
    that returns whether it wrote and returns False when the target already holds. `remote` effects write to a code
    host and can fail there: the core runs them before any tracker write. `needs` names the facts that must hold
    before the core lets it run."""
    kind: str
    target: str
    apply: str | Callable
    schema: dict | None = None
    remote: bool = False
    needs: tuple = ()

    def __post_init__(self):
        if self.target not in TARGETS:
            raise ValueError(f"effect {self.kind}: target {self.target!r} is not one of {', '.join(TARGETS)}")


@dataclass(frozen=True)
class Event:
    """A trigger payload as the gate reads it: the ticket, the event kind, and the PR it came from, if any."""
    ticket: object
    kind: str
    action: str | None = None
    pr: int | None = None
    repository: str | None = None
    raw: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Subject:
    """What a `read` returns a record of: `ticket` (id), `pr` (the PR of ticket `id` in repository `repo`, a
    descriptor name) or `repo` (`repo`). `scope` narrows the system to one environment's share of it, in the
    system's own terms (`{"label": ...}` on GitHub, `{"field": ..., "value": ...}` on YouTrack); None is all of it."""
    kind: str
    id: object = None
    repo: str | None = None
    scope: dict | None = field(default=None, hash=False)


@dataclass(frozen=True)
class Query:
    """What a `find` lists: `children` (the sub-tickets of `of`), `stage` (ids of tickets holding one of `values`;
    `open_only`) or `blocking` (the tickets `of` blocks). `scope` as on Subject."""
    kind: str
    of: object = None
    values: tuple = ()
    open_only: bool = False
    scope: dict | None = field(default=None, hash=False)


class System(Protocol):
    name: str
    effects: tuple[Effect, ...]

    def event(self, payload: dict) -> Event | None:
        """The event of a trigger payload, else None when it names no ticket."""

    def read(self, subject: Subject, want: frozenset = frozenset()):
        """The record of `subject`: a TicketRecord, a PrRecord (None without a PR) or a RepoRecord. Optional
        fields are filled only when `want` names them."""

    def find(self, query: Query) -> list:
        """The ids or child records `query` names."""

    # Setup hooks, optional until the slices that use them (#1068): `provision(config) -> list[str]` creates the
    # labels or fields, `wake_rules(plan) -> list` the triggers that wake a node, `probes(config) -> list` the
    # checks `factory doctor` runs.

"""The records a system reads (ADR-0026): plain data the core derives every fact from (factory/derive.py).

A system fills the optional fields (`None` by default) only when the read's `want` names them, so a fact that
costs a call is read only on the path that needs it. A field whose reads cost one call per key is a `Lazy` map:
it asks the system for a key on first use, so the core decides which keys are read.
"""
from collections.abc import Mapping
from dataclasses import dataclass, field


class Lazy(Mapping):
    """A read-only {key: value} that calls `get(key)` once per key, on first use."""

    def __init__(self, get, known=None):
        self._get, self._seen = get, dict(known or {})

    def __getitem__(self, key):
        if key not in self._seen:
            self._seen[key] = self._get(key)
        return self._seen[key]

    def __iter__(self):
        return iter(self._seen)

    def __len__(self):
        return len(self._seen)


@dataclass
class TicketRecord:
    """One ticket. `events` oldest first ({kind: labeled|unlabeled|closed|reopened|..., value, actor, at});
    `comments` oldest first ({id?, author, author_is_bot, author_is_factory, body, at}); `attrs` the labels or tags,
    stage ones included; `blockers_open` the ids of the open tickets blocking it; `can_answer` login -> whether
    that person may answer a question the factory asked."""
    id: object
    title: str = ""
    body: str = ""
    state: str = "open"
    close_reason: str | None = None
    author: str | None = None
    updated_at: str | None = None
    attrs: list = field(default_factory=list)
    assignees: list = field(default_factory=list)
    events: list = field(default_factory=list)
    comments: list = field(default_factory=list)
    blockers_open: list = field(default_factory=list)
    can_answer: Mapping = field(default_factory=dict)
    children: list | None = None  # want "children": [{number, state, not_planned, attrs}]
    blocking: list | None = None  # want "blocking": the tickets this one blocks


@dataclass
class PrRecord:
    """A ticket's pull request in one of its repositories (`repo`: the descriptor's name for it).
    `reviews`: {author, state, commit_id, focus, round, outcome, verdict, agent, assoc, submitted_at, bot,
    findings, body}; `comments`: {id, author, author_type, body, at}; `events` like a ticket's; `ci`
    green|red|pending|none at `head`; `commit_date` the head commit's committer date; `can_write` login -> write
    access to the repository; `clean_advance` sha -> whether `head` only merged the base into it since `sha`."""
    repo: str
    number: int
    head: str
    base: str = "main"
    branch: str | None = None
    state: str = "open"
    merged: bool = False
    mergeable: bool | None = None
    labels: list = field(default_factory=list)
    requested_reviewers: list = field(default_factory=list)
    body: str = ""
    author: str = ""
    author_type: str = ""
    updated_at: str | None = None
    reviews: list = field(default_factory=list)
    comments: list = field(default_factory=list)
    events: list = field(default_factory=list)
    ci: str = "none"
    commit_date: str = ""
    can_write: Mapping = field(default_factory=dict)
    clean_advance: Mapping = field(default_factory=dict)
    behind: bool | None = None  # want "behind": the base has commits the head lacks
    ci_failure: dict | None = None  # want "ci_failure": the first failed step at the head


@dataclass
class RepoRecord:
    """One repository of the team (`name` in the descriptor, `full_name` on its host)."""
    name: str
    full_name: str | None = None
    default_branch: str = "main"
    main_green: bool | None = None  # want "main_green": the latest CI run on the default branch passed at its head

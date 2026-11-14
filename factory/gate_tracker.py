"""The gate's tracker port over any system (ADR-0026): facts derived from records, effects through the registry.

`GateTracker(tracker_for_env, vcs_for_env)` resolves the systems of an environment: `tracker_for_env(env)` the system
that holds its tickets, `vcs_for_env(env)` the one that holds its pull requests (the tracker itself when it hosts the
PRs too). It reads the ticket and each of its PRs as records, derives the facts in the core (factory/derive.py) and
applies an effect on the system of its target under the core policies (factory/effects.py).
The tracker gives `rules()` (derive.Rules) and `repositories` ({name: ...}, the primary first), and may give `scope`
(ports/system.py Subject.scope), the share of it the environment owns; the gate's reads carry it.

A team with no environment tracker resolves every environment to the one top-level tracker (`GateTracker.single`),
so `env` is None and no read is scoped. With environment trackers (#1069) each environment's tracker carries its
`scope` and `scopes.ScopedTracker` sends each ticket to its environment's tracker.

An environment's vcs (#1070) is `config.vcs(config, env)`: its own `vcs:` over the top-level one. When that vcs is a
system of its own (it declares `effects`, as the space plugin's does), `OnVcs` puts it beside the tracker: its
`(kind, "pr")` effects and its PR reads go to it, and the facts it owns (`pr_facts`) replace the tracker's (`overlay`).
A vcs that declares no effects (github) leaves its pull requests to the tracker, which hosts them too.
"""
import derive
import effects
from gate_port import ticket_repositories
from ports.system import Query, Subject


class GateTracker:
    def __init__(self, tracker_for_env, vcs_for_env=None, env=None):
        self.tracker_for_env = tracker_for_env
        self.vcs_for_env = vcs_for_env or tracker_for_env
        self.env = env  # the environment of the tickets this port reads; None is the top-level tracker
        self.since = ""  # when a person last set `ready`, from the last `facts`

    @classmethod
    def single(cls, tracker, vcs=None):
        """The port of one tracker that every environment resolves to (and `vcs`, else the tracker, for the PRs)."""
        return cls(lambda env: tracker, (lambda env: vcs) if vcs is not None else None)

    @property
    def tracker(self):
        return self.tracker_for_env(self.env)

    @property
    def vcs(self):
        return self.vcs_for_env(self.env)

    def facts(self, n):
        tracker, vcs = self.tracker, self.vcs
        scope = getattr(tracker, "scope", None)
        ticket = tracker.read(Subject("ticket", n, scope=scope))
        rules = tracker.rules()
        stage = derive.stage(ticket, rules.stages)
        children = tracker.find(Query("children", of=n, scope=scope)) if stage == "split" else None
        names = list(getattr(tracker, "repositories", None) or {"primary": None})
        want = derive.pr_want(stage)  # a costly PR read only on the path that needs it
        # a tracker that links tickets to repositories answers itself (YouTrack `tracker.projects`, #981)
        repos = tracker.ticket_repositories(n, ticket.body) if hasattr(tracker, "ticket_repositories") else \
            ticket_repositories(ticket.body, names, names[0])
        prs = [(name, vcs.read(Subject("pr", n, name), want)) for name in repos]
        f, self.since = derive.facts(ticket, prs, rules, children)
        return f

    def apply(self, e):
        return effects.apply(self.tracker if effects.target(e) == "ticket" else self.vcs, e)

    def supports(self, e):
        system = self.tracker if effects.target(e) == "ticket" else self.vcs
        return effects.spec(system, e) is not None


def overlay(facts, vcs, n):
    """`facts` with the facts `vcs` owns (its `pr_facts`) read from it, when ticket `n` has a pull request there."""
    keys = getattr(vcs, "pr_facts", None)
    if not keys:
        return facts
    own = vcs.facts(n)
    return facts if own.get("pr") is None else {**facts, **{k: own[k] for k in keys if k in own}}


def hosts_prs(vcs):
    """Whether `vcs` is a system of its own that applies its PR effects (it declares `effects`)."""
    return any(x.target == "pr" for x in getattr(vcs, "effects", ()) or ())


class OnVcs(GateTracker):
    """The gate's tracker when the pull requests live on another system (`vcs_for_env(env)`): an effect on a PR
    that the vcs declares goes to it, under the core policies (factory/effects.py), every other to the tracker; a PR
    read goes to it; `facts` are the tracker's with the vcs's own (`overlay`). Anything else is the tracker's."""

    def __init__(self, tracker, vcs_for_env, env=None):
        super().__init__(lambda env: tracker, vcs_for_env, env)

    @classmethod
    def of(cls, tracker, vcs):
        """`tracker` with `vcs` beside it for every environment."""
        return cls(tracker, lambda env: vcs)

    def _on_vcs(self, e):
        return effects.target(e) == "pr" and (e["kind"], "pr") in effects.registry(self.vcs)

    @property
    def effects(self):
        """The tracker's effects, its PR ones replaced by the vcs's (what `order` and `refused` look up)."""
        mine = effects.registry(self.vcs)
        return (*(x for x in getattr(self.tracker, "effects", ()) if (x.kind, x.target) not in mine), *mine.values())

    def facts(self, n):
        return overlay(self.tracker.facts(n), self.vcs, n)

    def read(self, subject, want=frozenset()):
        return (self.vcs if subject.kind == "pr" else self.tracker).read(subject, want)

    def apply(self, e):
        return effects.apply(self.vcs, e) if self._on_vcs(e) else self.tracker.apply(e)

    def supports(self, e):
        if self._on_vcs(e):
            return effects.spec(self.vcs, e) is not None
        return effects.spec(self.tracker, e) is not None

    def __getattr__(self, name):
        if name.startswith("_") or name in ("tracker_for_env", "vcs_for_env", "env"):
            raise AttributeError(name)
        return getattr(self.tracker_for_env(self.env), name)

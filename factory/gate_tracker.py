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
        prs = [(name, vcs.read(Subject("pr", n, name), want)) for name in ticket_repositories(ticket.body, names, names[0])]
        f, self.since = derive.facts(ticket, prs, rules, children)
        return f

    def apply(self, e):
        return effects.apply(self.tracker if effects.target(e) == "ticket" else self.vcs, e)

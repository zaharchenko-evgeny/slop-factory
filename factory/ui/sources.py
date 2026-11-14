"""Ticket sources: one per tracker instance (#1069), read through the engine's tracker adapters and ports.

A `Source` wraps a gate tracker (`sweep_tickets`, `facts`, `read`, and `find` for the poll) with the tracker's
`identity` and the environments that share it. Environments on one tracker instance are one source, so a ticket they
both see is read once; `merge` keeps one ticket per `(identity, ticket id)` and lists every environment. Equal short
IDs on different tracker instances stay distinct. Every read here is a read: no `apply`, `claim` or `comment`.
"""
from dataclasses import dataclass, field

import scopes
from ports.system import Subject


@dataclass
class Source:
    identity: tuple
    environments: list
    tracker: object
    repository: str | None = None
    url_for: object = None  # (ticket) -> url, else a GitHub issue URL of `repository`
    scopes: list = field(default_factory=list)

    @property
    def id(self):
        return "|".join(str(x) for x in self.identity)

    def ticket_url(self, ticket):
        if callable(self.url_for):
            return self.url_for(ticket)
        if self.repository and str(ticket).split("#")[-1].isdigit():
            return f"https://github.com/{self.repository}/issues/{str(ticket).split('#')[-1]}"
        return None


def ticket_ids(source, stage_names):
    """The ticket ids of the tracker holding one of `stage_names`."""
    return list(source.tracker.sweep_tickets(list(stage_names)))


def record(source, ticket):
    """The ticket's record (events, comments, state), or None when the tracker cannot read it."""
    return source.tracker.read(Subject("ticket", ticket, scope=getattr(source.tracker, "scope", None)))


def merge(per_source):
    """[(source, ticket id, environments)] with one entry per (source identity, ticket id): `per_source` is
    [(source, ticket ids)]; two sources of one identity (environments listed apart) fold into one."""
    seen, out = {}, []
    for source, ids in per_source:
        for ticket in ids:
            key = (source.identity, ticket)
            if key in seen:
                seen[key][2][:] = sorted({*seen[key][2], *source.environments})
                continue
            seen[key] = (source, ticket, sorted(source.environments))
            out.append(seen[key])
    return out


def live_sources(config, nodes, run=None):
    """The sources of the installed factory, built by the engine's own constructors: with environment trackers
    (#1069) one per tracker identity (scope-split environments share it), else the team's tracker for every
    environment. `run` is a `gh` runner (surface.gh_runner)."""
    import gate
    runners = {"run": run} if run else {}
    names = list((nodes.get("environments") or {}))
    envs = scopes.project_envs(config)
    if not envs:
        tracker = gate.tracker_of(config, **runners)
        return [Source(scopes.identity(config.get("tracker") or {}), names or ["default"], tracker,
                       (config.get("tracker") or {}).get("repository"))]
    groups = {}
    for env, block in envs.items():
        groups.setdefault(scopes.identity(block), []).append((env, block))
    out = []
    for ident, members in groups.items():
        env, block = members[0]
        tracker = gate.env_systems(config, env, **runners)[0]
        out.append(Source(ident, [e for e, _ in members], tracker, block.get("repository"),
                          scopes=[b.get("scope") for _, b in members]))
    return out

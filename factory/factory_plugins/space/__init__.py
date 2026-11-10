"""The space plugin (ADR-0026): JetBrains Space as the vcs. A team turns it on with `vcs: space` (alias `code_host:`),
top-level or on an environment (`environments.<env>.vcs`); the tickets stay on the tracker it selects.

- `gate`: `SpaceCodeHost` (vcs `space`): the merge request of a ticket, its record and facts, the effects it applies
  on it (`label`, `unlabel`, `review`, `update-branch`, `merge` on `pr`), and `trigger`, the `gate.trigger` hook that
  reads a Space merge request webhook payload.
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="space",
    provides={"vcs": {"space": "factory_plugins.space.gate:SpaceCodeHost"},
              "vcs-contract": {"space": "factory_plugins.space.contract:VCS"}},  # ADR-0027
    hooks={"gate.trigger": "factory_plugins.space.gate:trigger"},
)

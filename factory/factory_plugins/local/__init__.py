"""The local plugin (ADR-0017, ADR-0026): the `local` surface, the factory's nodes on a laptop with the same workflow,
roles and gate as Air. A command that uses it turns it on (`factory deploy --target local`, `factory run --local`), as
does `surface: local`. Its agents run in the agent runner plugins (claude, codex, junie, pi), which a role's harness
turns on.

- `surface`: `LocalSurface`, the deploy checks and record (`deploy`), the poll of the trackers and the run loop (`main`).
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="local",
    provides={"surface": {"local": "factory_plugins.local.surface:LocalSurface"}},
    commands={"run": "factory_plugins.local.surface:main",
              "deploy --target local": "factory_plugins.local.surface:deploy"},
)

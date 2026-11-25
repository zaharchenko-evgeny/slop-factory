"""The host plugin (ADR-0029): the `host` executor, the agent runs on this machine in the worktrees the local surface
makes. It is the default executor and changes nothing for a team that sets none.
- `executor`: `Executor`, a plain `subprocess.run` with an optional timeout.
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="host",
    provides={"executor": {"host": "factory_plugins.host.executor:Executor"}},
)

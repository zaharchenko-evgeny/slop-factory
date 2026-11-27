"""The docker plugin (ADR-0029): the `docker` executor, the agent runs in a container per run with no host mount; the
workspace is built inside it. Turned on by an `executors.<name>` of `type: docker`.
- `executor`: `Executor`, the `docker` CLI calls of prepare, exec, put, collect, export and teardown.
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="docker",
    provides={"executor": {"docker": "factory_plugins.docker.executor:Executor"}},
)

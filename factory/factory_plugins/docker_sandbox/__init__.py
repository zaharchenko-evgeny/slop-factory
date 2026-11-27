"""The docker_sandbox plugin (ADR-0029): the `docker-sandbox` executor, one Docker Sandboxes (`sbx`) microVM per run,
agent-less (`shell`) so every harness runs in it. Optional: `sbx` is closed source and needs a Docker login.
- `executor`: `Executor`, the `sbx` CLI calls (docs/research/docker-sandboxes.md).
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="docker_sandbox",
    provides={"executor": {"docker-sandbox": "factory_plugins.docker_sandbox.executor:Executor"}},
)

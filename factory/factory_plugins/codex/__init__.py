"""The codex plugin (ADR-0026): Codex as the local agent runner (`codex exec`). A team turns it on by naming the
harness `codex` in `models.<alias>.harness`; the local surface turns it on when a role runs in it.

- `runner`: `Runner`, the headless `codex` argv, its skill directory and its PATH probe.
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="codex",
    provides={"agent": {"codex": "factory_plugins.codex.runner:Runner"}},
)

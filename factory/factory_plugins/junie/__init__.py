"""The junie plugin (ADR-0026): Junie as the local agent runner (`junie --task`). A team turns it on by naming the
harness `junie` in `models.<alias>.harness`; the local surface turns it on when a role runs in it.

- `runner`: `Runner`, the headless `junie` argv, its skill directory and its PATH probe.
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="junie",
    provides={"agent": {"junie": "factory_plugins.junie.runner:Runner"}},
)

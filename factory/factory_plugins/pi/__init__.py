"""The pi plugin (ADR-0026): pi as the local agent runner (`pi -p`). A team turns it on by naming the harness `pi` in
`models.<alias>.harness`; the local surface turns it on when a role runs in it.

- `runner`: `Runner`, the headless `pi` argv, its skill directory and its PATH probe.
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="pi",
    provides={"agent": {"pi": "factory_plugins.pi.runner:Runner"}},
)

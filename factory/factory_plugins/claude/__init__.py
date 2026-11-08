"""The claude plugin (ADR-0026): Claude Code as the local agent runner (`claude -p`). A team turns it on by naming the
harness `claude-code` (alias `claude`) in `models.<alias>.harness`; the local surface turns it on when a role runs in it.

- `runner`: `Runner`, the headless `claude` argv, its skill directory and its PATH probe.
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="claude",
    provides={"agent": {"claude-code": "factory_plugins.claude.runner:Runner",
                        "claude": "factory_plugins.claude.runner:Runner"}},
)

"""The facts plugin (ADR-0026): optional analytics that only the `retro` role reads. Using its command turns it on,
whatever the config selects (`factory facts`); a team need not list it.

- `report`: the run facts of tickets that reached `done` (`ticket_facts`, `facts`) and `factory facts --since <ISO time>` (`main`).
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="facts",
    commands={"facts": "factory_plugins.facts.report:main"},
)

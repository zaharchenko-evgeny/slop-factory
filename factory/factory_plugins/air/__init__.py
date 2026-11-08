"""The air plugin (ADR-0014, ADR-0026): the `air` surface, the factory's nodes as Air automations and environments.
A command that uses it turns it on (`factory deploy` when the team names no other surface, `factory deploy --target
air`, `factory contracts`), as does `surface: air`.

- `surface`: `AirSurface` (surface.py), the Air plan, its diff against the live automations and environments, and
  their apply through the air-automations and air-cloud MCP servers (deploy.py: `plan_air`, `read_state`, `diff`,
  `apply_diff`, `deploy`, `main`, the `Mcp` client and the automation REST `Backend`).
- `contracts`: the recordings of the Air services in factory/tests/contracts/ and the fake MCP server on them
  (contracts.py).
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="air",
    provides={"surface": {"air": "factory_plugins.air.surface:AirSurface"}},
    commands={"deploy --target air": "factory_plugins.air.deploy:main",
              "contracts": "factory_plugins.air.contracts:main"},
)

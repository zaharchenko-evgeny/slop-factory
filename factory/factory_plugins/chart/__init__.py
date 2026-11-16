"""The chart plugin (ADR-0014, ADR-0026): development tooling that renders the stage statechart, the Mermaid chart and
the node table of `docs/statechart*.md`. Using its command turns it on, whatever the config selects
(`factory chart`, `nodes.py chart`); a team need not list it.

- `render`: the chart, the table, the generated docs block (`block`) and `factory chart [nodes file]` (`main`).
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="chart",
    commands={"chart": "factory_plugins.chart.render:main"},
)

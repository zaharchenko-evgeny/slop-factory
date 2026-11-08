"""The comments plugin (ADR-0025, ADR-0026): the factory reacts to comments on tickets and PRs. It is off unless the
team's factory.yaml lists `plugins: [comments]`; while it is off the core imports this declaration only.

- `mentions`: `@factory:<agent>` wakes that agent in reply mode (#590): the `mention` guard fact, the generated
  `mentions` router and `reply-<agent>` nodes, and the `on: mention` reply target.
- `asks`: an agent asks another agent in the thread, with a hop budget (#591): the `ask` outcome.
- `node`: one comment node routes person comments and agent asks (ADR-0023): the cursor, the step input and the
  `route`, `reply` and `skip` effects.
- `wake`: the files deploy installs so a comment wakes the comment node.
"""
from plugins import Plugin

NAME = "comments"
EFFECTS = ("route", "reply", "skip")  # the effects of the comment node's outcomes (ADR-0023)

PLUGIN = Plugin(
    name=NAME,
    effects={**{(kind, "ticket"): "factory_plugins.comments.node:moves" for kind in EFFECTS},
             ("comment", "mention"): "factory_plugins.comments.mentions:reply_target"},
    guards={"mention": "factory_plugins.comments.mentions:holds"},
    triggers=("mention",),
    outcomes=("ask",),
    agent_keys=("mentionable",),
    hooks={"definition.load": "factory_plugins.comments.mentions:add_nodes",
           "definition.errors": "factory_plugins.comments.mentions:errors",
           "gate.step_context": "factory_plugins.comments.node:step_context",
           "gate.outcome": "factory_plugins.comments.asks:outcome",
           "deploy.files": "factory_plugins.comments.wake:files"},
)

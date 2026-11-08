"""The notify plugin (ADR-0022, ADR-0026): the gate sends a person an actionable message and keeps a `factory:notify`
marker for each send. It is off unless the team's factory.yaml lists `plugins: [notify]`; a channel is a separate
plugin that provides a `notifier` (notify_email: `email`).

- `notifiers`: the message (`validate_message`, `render`), the marker, the `notify` config and `send_all` over the
  turned-on notifiers.
- `effect`: the `notify` effect on a ticket or its PR: the message from the payload, then the sends and their
  `[factory]` comment (the `gate.send` hook).
- `checks`: `factory validate` of the `notify:` config and the `notify` effects.
- `command`: `factory notify test <login>`.
"""
from plugins import Plugin

NAME = "notify"

PLUGIN = Plugin(
    name=NAME,
    effects={(NAME, target): "factory_plugins.notify.effect:resolve" for target in ("ticket", "pr")},
    commands={NAME: "factory_plugins.notify.command:main"},
    config_keys=(NAME,),
    hooks={"definition.errors": "factory_plugins.notify.checks:errors",
           "gate.send": "factory_plugins.notify.effect:send"},
)

"""The notify_email plugin (ADR-0022, ADR-0026): the `email` notifier, SMTP out; replies come back through
`integrations/notify-relay`. Turning it on turns on `notify`.

- `smtp`: `EmailAdapter`, the `ports.notifier.Notifier` for `notify.adapters.email`.
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="notify_email",
    provides={"notifier": {"email": "factory_plugins.notify_email.smtp:EmailAdapter"}},
    requires=("notify",),
)

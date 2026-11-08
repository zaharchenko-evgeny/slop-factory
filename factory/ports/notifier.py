"""The `notifier` slot (ADR-0022), owned by the notify plugin: a channel that sends a person an actionable message.

A plugin provides one as `provides={"notifier": {<name>: "mod:Class"}}`; `notify.adapters.<name>` in factory.yaml
holds its settings and `notify.people.<login>.via` names it. Secrets are read from the env vars the settings name,
never from the file.
"""
from typing import Protocol


class Notifier(Protocol):
    name: str  # the adapter name in `notify.adapters` and `via`; a person's address for it is `notify.people.<login>.<name>`

    def __init__(self, settings: dict) -> None:
        """`settings` is `notify.adapters.<name>`; raises when the channel cannot be reached (an env var is unset)."""

    @staticmethod
    def validate(settings: dict, person: dict) -> list[str]:
        """`code: text` problems of `settings` and of `person` (`notify.people.<login>` plus its `login`) for this
        channel, found by `factory validate` without sending anything."""

    def send(self, person: dict, message: dict) -> str:
        """Deliver `message` to `person`; returns the `ref` that names the thread on the channel."""

    def ack(self, person: dict, ref: str, text: str) -> None:
        """Optional: tell `person` in thread `ref` that their reply was received or refused. A channel without it
        takes replies on GitHub."""

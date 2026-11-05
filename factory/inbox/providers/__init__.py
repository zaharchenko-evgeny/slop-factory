"""Notification providers: a registry by descriptor `notify.providers[].kind`."""
from typing import Protocol

REGISTRY = {}


class Provider(Protocol):
    def seen(self, key: str) -> bool:
        """Whether a message with this key was already sent."""

    def send(self, message: dict) -> bool:
        """Deliver the message; False when its key was already sent."""


def register(kind):
    def add(factory):
        REGISTRY[kind] = factory
        return factory
    return add


def build(entries, settings, run):
    """One provider per descriptor entry; `factory(settings, run)` builds each."""
    return [(e["kind"], REGISTRY[e["kind"]](settings, run)) for e in entries]


from . import github_inbox, log  # noqa: E402,F401  (register themselves)

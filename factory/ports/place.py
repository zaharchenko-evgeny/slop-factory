"""The static contract of a tracker and a vcs (ADR-0027): what `factory validate` and `factory deploy` ask about a
repository's `tracker:` or `vcs:` block without building the runtime tracker or code host.

A plugin provides one per adapter, as `provides={"tracker-contract": {<adapter>: "mod:obj"}}` (or `vcs-contract`);
the runtime class stays under `tracker` / `vcs`. The engine names no system: it reads only `adapter` and asks these.
"""
from typing import Protocol


class TrackerContract(Protocol):
    LOCAL_KEYS: tuple  # block keys a repository entry never takes from the top-level block (its place: `repository`)

    def place(self, block: dict, repo: str) -> str:
        """Where the tickets of repository `repo` live, scope included: two repositories with one place are an error."""

    def home(self, block: dict, repo: str) -> str | None:
        """The repository whose events carry those tickets (an automation hears only its main repository's events),
        or None when the tracker's events reach any automation (filtered by the trigger)."""

    def check(self, block: dict, where: str) -> list[str]:
        """The config errors of the block."""


class VcsContract(Protocol):
    def home(self, block: dict, repo: str) -> str | None:
        """The repository whose events carry the PRs of `repo`, or None when they arrive another way (a webhook)."""

    def check(self, block: dict, repo: str, where: str) -> list[str]:
        """The config errors of the block for repository `repo`."""

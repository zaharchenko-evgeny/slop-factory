"""The `executor` slot (ADR-0029): where a local node's agent runs (`host`, `docker`, `docker-sandbox`). A runner
(ports/agent.py) builds the argv; an executor runs it. Air has no executor: the platform runs the agent itself.

An executor is built with its instance options (`executors.<name>` of factory.yaml, minus `type`). `isolated` is False
when the workspace is the host's (`host`: worktrees under `.factory/worktrees`) and True when the executor builds it
inside its sandbox (clone from the remote, `git worktree add`, copy-in of the engine, team files and skills).
"""
import shutil
from typing import Protocol

from ports.agent import ProbeResult


class Executor(Protocol):
    name: str
    isolated: bool

    def prepare(self, run_id: str):
        """Start the run's sandbox; the handle every other call takes (None for `host`)."""

    def exec(self, handle, argv: list[str], cwd=None, env: dict | None = None, timeout: int | None = None):
        """Run argv to completion: a `subprocess.CompletedProcess` with str output. A timeout kills the run and
        returns exit code 124."""

    def collect(self, handle, paths: list[str], dest) -> None:
        """Copy the sandbox's run files and logs to the host directory `dest`; also after a failure or timeout."""

    def export(self, handle, repo: str, branch: str, dest):
        """The committed `branch` of `repo` as a git bundle at `dest`, or None when nothing is committed."""

    def teardown(self, handle) -> None:
        """Destroy the sandbox; never raises. `keep: true` leaves it."""

    def probe(self, which=shutil.which) -> list[ProbeResult]:
        """The executor's own prerequisites, in `factory doctor`'s shape."""

    def reconcile(self) -> int:
        """Remove sandboxes left by a crashed host; the count removed."""

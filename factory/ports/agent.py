"""The `agent` slot (ADR-0026): an agent runner, the headless CLI that runs a node's agent on the local surface only
(ADR-0018: on Air the platform runs the agent). A runner plugin (`claude`, `codex`, `junie`, `pi`) provides one per
harness name; `models.<alias>.harness` and the local harness settings name it.
"""
import shutil
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProbeResult:
    """One check of a runner, in `factory doctor`'s shape: `probe`, `result` (`ok` or `missing`) and `detail`."""
    probe: str
    result: str
    detail: str = ""

    @property
    def ok(self):
        return self.result == "ok"


class AgentRunner(Protocol):
    name: str  # the harness name (`claude-code`, `codex`, `junie`, `pi`)
    cli: str  # the binary on PATH
    skill_dir: str  # the project skill directory, relative to the worktree

    def command(self, prompt: str, model: str, write: bool) -> list[str]:
        """The argv of one headless run: `write` nodes get edit tools, read-only nodes none."""

    def probe(self, which=shutil.which) -> ProbeResult:
        """Whether `cli` is on PATH."""


def probe_cli(runner, which=shutil.which):
    """The PATH check every runner makes: its `cli` is found by `which`."""
    if which(runner.cli):
        return ProbeResult(f"harness:{runner.name}", "ok", f"`{runner.cli}` ({runner.name})")
    return ProbeResult(f"harness:{runner.name}", "missing", f"`{runner.cli}` ({runner.name}) is not on PATH")

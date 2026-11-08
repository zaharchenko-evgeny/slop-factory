"""The `surface` slot (ADR-0026): where the factory's nodes run (`air`, `local`). A surface plugin plans the
automations of the node graph, reads what is deployed, diffs the two and applies the difference.
"""
from typing import Protocol


class Surface(Protocol):
    name: str

    def plan(self, nodes: dict, config: dict) -> list:
        """The automations this surface serves for the node graph `nodes`."""

    def read_state(self):
        """What is deployed now; None when nothing is."""

    def diff(self, plan, state) -> dict:
        """What `apply` changes to turn `state` into `plan`."""

    def apply(self, diff) -> None:
        """Make the deployed state match the plan."""

    def outputs(self) -> set:
        """The node `output:` values this surface supports."""

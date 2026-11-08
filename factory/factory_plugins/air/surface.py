"""The `air` surface (ports/surface.py): the Air plan, the live automations and environments, their diff and apply.

Only `outputs()` is answered here; the rest imports factory_plugins/air/deploy.py when called, so `factory validate`
checks a node's `output:` without loading the Air deploy code.
"""

# The `outputBehaviour` values of Air's create_automation (factory/tests/contracts/air-mcp.json)
OUTPUTS = ("read-only", "push-to-source", "push-to-new-branch", "create-pr", "as-instructed")


class AirSurface:
    """The Air automations and environments of the plan, through the air-automations (`automations`) and air-cloud
    (`cloud`) MCP clients, and the automation REST `backend` for additional repositories."""
    name = "air"

    def __init__(self, automations=None, cloud=None, backend=None):
        self.automations, self.cloud, self.backend = automations, cloud, backend
        self.last = None

    def plan(self, nodes, config):
        """The Air plan (`plan_air`): automations, environments, labels and files."""
        from factory_plugins.air.deploy import plan_air
        self.last = plan_air(nodes, config)
        return self.last

    def read_state(self):
        """(automations, environments) in Air under the last plan's prefix."""
        from factory_plugins.air.deploy import read_state, repository_urls
        return read_state(self.automations, self.cloud, self.last["prefix"], repository_urls(self.last),
                          backend=self.backend,
                          additional_names={a["name"] for a in self.last["automations"] if "additionalRepositories" in a})

    def diff(self, plan, state):
        """The creates, updates, replaces and deletes that turn `state` into `plan`."""
        from factory_plugins.air.deploy import diff
        self.last = plan
        return diff(plan, *state)

    def apply(self, diff):
        from factory_plugins.air.deploy import apply_diff, repository_urls
        apply_diff(diff, self.automations, self.cloud, repository_urls(self.last), backend=self.backend,
                   env_repositories={e["name"]: e["repository"] for e in self.last["environments"]})

    def outputs(self):
        return set(OUTPUTS)

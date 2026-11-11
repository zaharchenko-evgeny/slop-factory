"""The `air` surface (ports/surface.py): validation hooks plus the Air plan, live automations and environments.

Air deploy code is imported only when a plan or live state is needed, so `factory validate` does not load it.
"""

# The `outputBehaviour` values of Air's create_automation (factory/tests/contracts/air-mcp.json)
OUTPUTS = ("read-only", "push-to-source", "push-to-new-branch", "create-pr", "as-instructed")
EVENT_OUTPUTS = {
    "GitHub event": ("read-only", "create-pr"),
    "pull_request.*": ("read-only", "push-to-source"),
}


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

    def environment_errors(self, defn):
        """ADR-0027: Air pushes only to an automation's main repository, so an environment has at most one writable
        repository."""
        import definition
        return [f"several-writable: environments.{e}: {', '.join(writable)}: the air surface allows one writable "
                f"repository per environment"
                for e in (defn["factory"].get("environments") or {})
                if len(writable := [r["repo"] for r in definition.repositories(defn, e) if r["write"]]) > 1]

    def trigger_output_errors(self, defn, nodes):
        """Reject output behaviours that Air does not allow for an event trigger."""
        import definition
        errs = []
        for name, node in (defn["workflow"].get("nodes") or {}).items():
            if node.get("enabled") is False:
                continue
            output = node.get("output", "read-only")
            if output not in OUTPUTS:
                continue  # the normal output check reports invalid values
            for copy in definition.copies(defn, name):
                copy_output = output if copy["write"] else "read-only"
                for trigger in (nodes.get("nodes", {}).get(name) or {}).get("triggers") or []:
                    if trigger.startswith(("schedule ", "webhook ", "manual")):
                        continue
                    kind = "pull_request.*" if trigger.startswith("pull_request.") else "GitHub event"
                    allowed = EVENT_OUTPUTS[kind]
                    if copy.get("tracker") and copy["tracker"].get("adapter") == "youtrack" \
                            and trigger.startswith("issues.labeled"):
                        kind = "YouTrack-tag"
                        allowed = EVENT_OUTPUTS["GitHub event"]
                    if copy_output not in allowed:
                        errs.append(f"bad-output: nodes.{name}: {copy_output} on a {kind} trigger "
                                    f"(Air allows: {', '.join(allowed)})")
        return errs

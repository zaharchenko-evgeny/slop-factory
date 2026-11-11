"""The github plugin (ADR-0026): GitHub Issues as the tracker and GitHub as the vcs. It is on by default: a team turns it
on whenever `tracker.adapter` is unset or `github-issues`, or `vcs:` (alias `code_host:`) is unset or `github`.

- `gate`: `GitHubGateTracker` (tracker `github-issues`) and `GitHubCodeHost` (vcs `github`) behind the gate.
- `tracker`: the `gh` runner, its call counter and project-write wrapper, and the label and ticket operations.
- `graphql`: the batched GraphQL reads, in the REST shapes the gate reads.
- `provision`: the repository labels of the deploy's tracker schema and the deploy's runner.
- `wake`: the files that wake a node: the comment wake files (`wake_rules`).
- `probes`: the checks `factory doctor` runs against GitHub.
- `ci_sync`: the `factory ci-sync` command and its `reconcile` pass.
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="github",
    provides={"tracker": {"github-issues": "factory_plugins.github.gate:GitHubGateTracker"},
              "vcs": {"github": "factory_plugins.github.gate:GitHubCodeHost"},
              "tracker-contract": {"github-issues": "factory_plugins.github.contract:ISSUES"},  # ADR-0027
              "vcs-contract": {"github": "factory_plugins.github.contract:VCS"}},
    commands={"ci-sync": "factory_plugins.github.ci_sync:command"},
    hooks={"reconcile.passes": "factory_plugins.github.ci_sync:reconcile_pass",
           "deploy.labels": "factory_plugins.github.provision:reconcile",
           "deploy.provision": "factory_plugins.github.provision:provision",
           "deploy.run": "factory_plugins.github.provision:write_run",
           "deploy.files": "factory_plugins.github.wake:sync_wake_files",
           "doctor.probes": "factory_plugins.github.probes:probes"},
)

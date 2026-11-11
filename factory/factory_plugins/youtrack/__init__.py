"""The youtrack plugin (ADR-0026): YouTrack as the tracker. A team turns it on with `tracker.adapter: youtrack`; the
pull requests stay on the vcs it selects (github).

- `store`: the YouTrack ticket store over the REST API, its `urllib` runner and the effects it declares.
- `effects`: its ticket effect handlers: the stage (a tag, per repository, or the state field), tags, `set-field`.
- `gate`: `YouTrackTracker` (tracker `youtrack`): the vcs's gate tracker over the store, from a tracker config block.
- `provision`: the tags and state-field values `factory deploy` plans and writes, the copies' project filters and the
  `tracker.projects` checks.
- `probes`: the read-only checks `factory doctor` runs against YouTrack.
- `wake`: the comment wake workflow (`comment-wake/`) `factory deploy` uploads while the comments plugin is on.
"""
from plugins import Plugin

PLUGIN = Plugin(
    name="youtrack",
    provides={"tracker": {"youtrack": "factory_plugins.youtrack.gate:YouTrackTracker"},
              "tracker-contract": {"youtrack": "factory_plugins.youtrack.contract:TRACKER"}},  # ADR-0027
    hooks={"deploy.plan": "factory_plugins.youtrack.provision:plan",
           "deploy.repo_tags": "factory_plugins.youtrack.provision:repo_tags",
           "deploy.trigger_filters": "factory_plugins.youtrack.provision:trigger_filters",
           "deploy.errors": "factory_plugins.youtrack.provision:errors",
           "deploy.tracker": "factory_plugins.youtrack.provision:provisioner",
           "deploy.files": "factory_plugins.youtrack.wake:sync_wake_files",
           "tracker.identity": "factory_plugins.youtrack.provision:identity",
           "tracker.owns_id": "factory_plugins.youtrack.provision:owns_id",
           "tracker.block_errors": "factory_plugins.youtrack.provision:block_errors",
           "config.tracker_errors": "factory_plugins.youtrack.provision:tracker_errors",
           "deploy.scope_filter": "factory_plugins.youtrack.provision:scope_filter",
           "doctor.probes": "factory_plugins.youtrack.probes:probes"},
)

"""The YouTrack tracker behind the gate (tracker `youtrack`): the tickets in the YouTrack store, the pull requests on the
selected vcs.

YouTrack holds no pull requests, so the gate tracker is the vcs's, over this plugin's store: the vcs class's
`gate_tracker(descriptor, tickets, run)` builds it (github: `GitHubCodeHost.gate_tracker`). The store declares the
ticket effects it applies itself (store.py `effects`), so the gate dispatches them through effects.py.
"""
from pathlib import Path

import yaml

import config
import definition
import plugins
from factory_plugins.youtrack.store import YouTrackTicketStore, repo_stage_tags


def state_field(c):
    """The custom field the descriptor's `field` state stores name, else None (stages are tags)."""
    return next((s["store"]["field"]["name"] for s in (c.get("states") or {}).values()
                 if isinstance((s.get("store") or {}).get("field"), dict)), None)


def repository_names(c):
    """The names of the descriptor's repositories, the primary first."""
    return [r["name"] for r in sorted(c.get("repositories") or [], key=lambda r: r.get("role") != "primary")]


def full_names(c):
    """{owner/repo: name} of the descriptor's repositories."""
    return {definition.full_name(r["url"]): r["name"] for r in c.get("repositories") or [] if r.get("url")}


def store(c, tracker=None, run=None):
    """The YouTrack store of descriptor `c`, from the tracker config block `tracker` (default `c["tracker"]`)."""
    t = c["tracker"] if tracker is None else tracker
    return YouTrackTicketStore.from_config(t, run, state_field=state_field(c), bots=t.get("bot_logins") or (),
                                           repo_stages=repo_stage_tags({**c, "tracker": t}),
                                           repositories=repository_names(c), full_names=full_names(c))


class YouTrackTracker:
    """The `youtrack` tracker the registry provides: only `from_config`, which returns the gate tracker."""

    @classmethod
    def from_config(cls, path=None, run=None, tracker=None, yt_run=None, descriptor=None):
        """The gate tracker of the descriptor at `path` (else `descriptor`, else the team's): its tickets in YouTrack,
        from the tracker config block `tracker` (default the descriptor's `tracker`; an environment's merged block
        carries its `scope`), its pull requests on the vcs the descriptor selects. `run` is the vcs's runner, `yt_run`
        YouTrack's (tests stub both)."""
        c = yaml.safe_load(Path(path).read_text()) if path else descriptor if descriptor is not None else config.load()
        vcs = config.vcs(c)
        if not (c.get("code_host") or {}).get("repository"):  # the gate tracker's pull requests (GitHubGateTracker)
            raise ValueError("descriptor: code_host.repository is required with tracker.adapter youtrack")
        host = plugins.registry(c).impl("vcs", vcs.get("adapter") or "github")
        if not hasattr(host, "gate_tracker"):
            raise ValueError(f"descriptor: tracker.adapter youtrack needs a vcs that hosts the gate's pull requests, "
                             f"not {vcs.get('adapter')}")
        return host.gate_tracker(c, store(c, tracker, yt_run), **({"run": run} if run is not None else {}))

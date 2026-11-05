#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Read-only live probe: load one YouTrack issue through `YouTrackTicketStore` and print its normalised facts.

Run: YOUTRACK_TOKEN=... uv run scripts/youtrack_probe.py LME-1701
Reads `tracker` (and the state field) from the team's factory.project.yaml; writes nothing.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "factory"))
import config  # noqa: E402
import youtrack_tracker  # noqa: E402


def facts(store, n):
    ticket = store.issue(n)
    return {"issue": n, "state": ticket["state"], "attrs": ticket["attrs"], "ticket": ticket, "events": store.events(n),
            "open_blockers": store.open_blockers(n), "comments": len(store.comments(n))}


def main(argv):
    if len(argv) != 2:
        print("usage: youtrack_probe.py <issue id>", file=sys.stderr)
        return 2
    settings = config.load()
    field = next((s["store"]["field"]["name"] for s in (settings.get("states") or {}).values()
                  if isinstance((s.get("store") or {}).get("field"), dict)), None)
    store = youtrack_tracker.YouTrackTicketStore.from_config(settings["tracker"], state_field=field)
    print(json.dumps(facts(store, argv[1]), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Drive one ticket through the state machine (factory/sm.py) against a tracker adapter.

The adapter supplies the factory operations of docs/agents/issue-tracker.md.
Action results come from the caller; real agent execution is a later slice.

CLI: uv run factory/drive.py branch <ticket> <ticket.json>  prints the ticket's branch name.
"""
import json
import re
import sys
from pathlib import Path

import yaml

import sm

STOPWORDS = {"a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "with", "from", "by"}


def drive(m, tracker, nn, executor, results):
    """Claim ticket `nn`, resume from the stored state, vars and run state (`sm.persisted`),
    apply each result with `sm.step`, write them back after every step, and release the claim
    once the ticket waits on a human or external actor, or when reading the ticket fails.
    `results` items use sm.parse_event's forms. Return (ticket, trace)."""
    tracker.claim(nn, executor)  # raises before any write when another executor holds it
    try:
        ticket = sm.new_ticket(m, {"state": tracker.get_state(nn), "vars": tracker.read_vars(nn)})
    except BaseException:
        tracker.release(nn, executor)
        raise
    trace = []
    try:
        for item in results:
            ticket, rep = sm.step(m, ticket, *sm.parse_event(item))
            trace.append(rep)
            tracker.set_state(nn, ticket["state"])
            tracker.write_vars(nn, sm.persisted(ticket))
            if rep["wait"]:
                break
    finally:
        if m["states"][ticket["state"]]["actor"] != "agent":
            tracker.release(nn, executor)
    return ticket, trace


def slug(title, words=4):
    """A short branch slug from the title: lower-case words without stopwords."""
    kept = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in STOPWORDS]
    return "-".join(kept[:words]) or "ticket"


def branch_name(pattern, n, title, body=""):
    """`pattern` with the ticket's `slug:` line if the body has one, else a slug of the title."""
    m = re.search(r"^slug:[ \t]*([a-z0-9][a-z0-9-]*)[ \t]*$", body or "", re.M)
    return pattern.format(ticket=n, slug=m.group(1) if m else slug(title))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 3 or argv[0] != "branch":
        print(__doc__.splitlines()[-1].strip(), file=sys.stderr)
        return 2
    pattern = yaml.safe_load((sm.ROOT / "factory.yaml").read_text())["branch_pattern"]
    t = json.loads(Path(argv[2]).read_text())
    print(branch_name(pattern, argv[1], t.get("title") or "", t.get("body") or ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

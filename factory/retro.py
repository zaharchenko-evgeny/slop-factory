#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Lesson collection for the nightly `retro` (ADR-0015, ADR-0021): the agent writes lessons, it never counts.

Usage: uv run factory/retro.py collect                     (JSON: the uncollected lesson comments; writes nothing)
       uv run factory/retro.py mark <comment id>...        (marks those comments collected; unlabels tickets with none left)
       Trackers: every one of the run's environment (GitHub issues, YouTrack issues tagged with the label in each scope).
The run facts are the facts plugin's (`factory facts`, ADR-0026).
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg  # noqa: E402
import scopes  # noqa: E402
import vocab  # noqa: E402
from gate_port import LESSON  # noqa: E402

ID = re.compile(r"\d+(-\d+)?")  # a GitHub comment id, or a YouTrack one (`4-12`)


def stores(tracker):
    """The ticket store of every tracker of the run's environment: the GitHub issues of each github-issues repository
    and each YouTrack scope (a `ScopedTracker` holds one tracker per environment or ticket place)."""
    if isinstance(tracker, scopes.ScopedTracker):
        return [tracker.systems(e)[0].tickets for e in tracker.candidates()]
    return [tracker.tickets]


def collect(tracker, label):
    """ADR-0021: the uncollected lesson comments of every ticket labelled `label` in every tracker, as
    [{ticket, url, id, body}] (`ticket` is `#12`'s `12`, `owner/repo#12` or `LME-123`). It only reads: `mark` marks
    them once retro has published them."""
    return [{"ticket": c["ticket"], "url": c["url"], "id": c["id"], "body": c["body"].replace(LESSON, "").strip()}
            for store in stores(tracker) for c in store.lesson_comments(label) if not c["collected"]]


def mark(tracker, label, ids):
    """Mark the lesson comments `ids` collected and take `label` off each ticket that has no uncollected lesson
    comment left. The label comes off first and goes back on when a lesson remains or a write fails, so a lesson
    posted (and labelled) while `mark` runs is never hidden by a stale read. Returns the ids marked. An id that is
    not an uncollected lesson comment of a labelled ticket is left alone."""
    wanted, done = {str(i) for i in ids}, []
    for store in stores(tracker):
        for n in dict.fromkeys(c["ticket"] for c in store.lesson_comments(label)):
            left = False
            store.remove_attr(n, label)
            try:
                for c in store.lesson_comments(label, n):
                    if c["collected"]:
                        continue
                    if str(c["id"]) in wanted:
                        store.mark_lesson(n, c["id"])
                        done.append(c["id"])
                    else:
                        left = True
            except BaseException:
                store.add_attr(n, label)
                raise
            if left:
                store.add_attr(n, label)
    return done


def main(argv):
    import gate
    if argv == ["collect"]:
        print(json.dumps(collect(gate.tracker_of(cfg.load()), vocab.lesson()), indent=2))
        return 0
    if argv[:1] == ["mark"] and len(argv) > 1 and all(ID.fullmatch(a) for a in argv[1:]):
        print(json.dumps(mark(gate.tracker_of(cfg.load()), vocab.lesson(), argv[1:])))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Lesson collection for the nightly `retro` (ADR-0015, ADR-0021): the agent writes lessons, it never counts.

Usage: uv run factory/retro.py collect                     (JSON: the uncollected lesson comments of every environment; writes nothing)
       uv run factory/retro.py mark <comment id>...        (marks those comments collected; unlabels tickets with none left)
A crash exits 1 with `failed: ...` on stderr. The run facts are the facts plugin's (`factory facts`, ADR-0026).
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg  # noqa: E402
import vocab  # noqa: E402

ID = re.compile(r"[\w.-]+")  # a comment id: digits on GitHub, `4-12` on YouTrack
LESSON, COLLECTED = "<!-- factory:lesson -->", "<!-- factory:lesson collected -->"


def collect(tracker, label):
    """ADR-0021: the uncollected lesson comments of every ticket labelled `label`, as [{ticket, url, id, body}].
    It only reads: `mark` marks them once retro has published them. `tracker` is the gate's tracker (a scopes wrapper
    reads every environment's); it is read through its public ticket methods only."""
    out = []
    for n in tracker.tickets_with(label):
        for c in tracker.ticket_comments(n):
            body = c.get("body") or ""
            if LESSON in body:
                out.append({"ticket": n, "url": c.get("url"), "id": c["id"], "body": body.replace(LESSON, "").strip()})
    return out


def mark(tracker, label, ids):
    """Mark the lesson comments `ids` collected and take `label` off each ticket that has no uncollected lesson
    comment left. The label comes off first and goes back on when a lesson remains or a write fails, so a lesson
    posted (and labelled) while `mark` runs is never hidden by a stale read. Returns the ids marked. An id that is
    not an uncollected lesson comment of a labelled ticket is left alone. Ids compare as text: GitHub's are numbers,
    YouTrack's look like `4-12`."""
    wanted, done = {str(i) for i in ids}, []
    for n in tracker.tickets_with(label):
        left = False
        tracker.edit_ticket_attrs(n, remove=[label])
        try:
            for c in tracker.ticket_comments(n):
                body = c.get("body") or ""
                if LESSON not in body:
                    continue
                if str(c["id"]) in wanted:
                    tracker.edit_comment(n, c["id"], body.replace(LESSON, COLLECTED))
                    done.append(c["id"])
                else:
                    left = True
        except BaseException:
            tracker.edit_ticket_attrs(n, add=[label])
            raise
        if left:
            tracker.edit_ticket_attrs(n, add=[label])
    return done


def main(argv):
    if argv == ["collect"] or (argv[:1] == ["mark"] and len(argv) > 1 and all(ID.fullmatch(a) for a in argv[1:])):
        try:
            import gate
            tracker = gate.systems(cfg.load())[0]  # the scopes wrapper when environments have their own trackers
            if argv == ["collect"]:
                print(json.dumps(collect(tracker, vocab.lesson()), indent=2))
            else:
                print(json.dumps(mark(tracker, vocab.lesson(), argv[1:])))
        except Exception as e:  # a crash is a failure the role reports, never "no lessons"
            print(f"failed: factory retro {argv[0]}: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

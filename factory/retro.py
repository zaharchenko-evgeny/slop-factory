#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Lesson collection for the nightly `retro` (ADR-0015, ADR-0021): the agent writes lessons, it never counts.

Usage: uv run factory/retro.py collect                     (JSON: the uncollected lesson comments, GitHub and YouTrack; writes nothing)
       uv run factory/retro.py mark <comment id>...        (marks those comments collected; unlabels tickets with none left)
The run facts are the facts plugin's (`factory facts`, ADR-0026).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg  # noqa: E402
import vocab  # noqa: E402

LESSON, COLLECTED = "<!-- factory:lesson -->", "<!-- factory:lesson collected -->"


def gate_trackers(tracker):
    """Each environment's gate tracker (ADR-0027): a scoped tracker builds one per project environment, and refuses
    private names such as `_list` itself (#1426); any other tracker is its own one."""
    import scopes
    if isinstance(tracker, scopes.ScopedTracker):
        return [tracker.systems(env)[0] for env in tracker.envs]
    return [tracker]


def issue_trackers(tracker):
    """The gate trackers whose tickets are GitHub issues, one per repository."""
    out, seen = [], set()
    for t in gate_trackers(tracker):
        if getattr(t, "tickets", t) is getattr(t, "github", t) and getattr(t, "repo", id(t)) not in seen:
            seen.add(getattr(t, "repo", id(t)))
            out.append(t)
    return out


def youtrack_stores(tracker):
    """The YouTrack ticket stores behind the gate trackers (their `tickets`), one per scope."""
    out = []
    for t in gate_trackers(tracker):
        store = getattr(t, "tickets", None)
        if store is not None and store is not getattr(t, "github", None) and hasattr(store, "lesson_comments"):
            out.append(store)
    return out


def collect(tracker, label):
    """ADR-0021: the uncollected lesson comments of every ticket labelled `label`, as [{ticket, url, id, body}]:
    GitHub issues of every environment, then YouTrack tickets (`tracker: youtrack`; never marked, retro cites
    their link). It only reads: `mark` marks the GitHub ones once retro has published them."""
    out, seen = [], set()
    for t in issue_trackers(tracker):
        for i in t._list(f"issues?state=all&labels={label}&per_page=100"):
            n = i["number"]
            for c in t._list(f"issues/{n}/comments"):
                body = c.get("body") or ""
                if LESSON in body and c["id"] not in seen:
                    seen.add(c["id"])
                    out.append({"ticket": n, "url": c.get("html_url"), "id": c["id"], "body": body.replace(LESSON, "").strip()})
    for store in youtrack_stores(tracker):
        for n in store.tickets_by_stage([label]):
            for c in store.lesson_comments(n):
                if LESSON in c["body"] and c["id"] not in seen:
                    seen.add(c["id"])
                    out.append({"ticket": n, "url": c["url"], "id": c["id"], "body": c["body"].replace(LESSON, "").strip(),
                                "tracker": "youtrack"})
    return out


def mark(tracker, label, ids):
    """Mark the lesson comments `ids` collected and take `label` off each ticket that has no uncollected lesson
    comment left. The label comes off first and goes back on when a lesson remains or a write fails, so a lesson
    posted (and labelled) while `mark` runs is never hidden by a stale read. Returns the ids marked. An id that is
    not an uncollected lesson comment of a labelled ticket is left alone."""
    wanted, done = {int(i) for i in ids}, []
    for t in issue_trackers(tracker):
        done += _mark(t, label, wanted)
    return done


def _mark(tracker, label, wanted):
    """`mark` on one GitHub-issue tracker."""
    done = []
    for i in tracker._list(f"issues?state=all&labels={label}&per_page=100"):
        n, left = i["number"], False
        tracker._edit(n, remove=[label])
        try:
            for c in tracker._list(f"issues/{n}/comments"):
                body = c.get("body") or ""
                if LESSON not in body:
                    continue
                if c["id"] in wanted:
                    tracker._api(f"issues/comments/{c['id']}", "-X", "PATCH", "-f", f"body={body.replace(LESSON, COLLECTED)}")
                    done.append(c["id"])
                else:
                    left = True
        except BaseException:
            tracker._edit(n, add=[label])
            raise
        if left:
            tracker._edit(n, add=[label])
    return done


def main(argv):
    import gate
    if argv == ["collect"]:
        print(json.dumps(collect(gate.tracker_of(cfg.load()), vocab.lesson()), indent=2))
        return 0
    if argv[:1] == ["mark"] and len(argv) > 1 and all(a.isdigit() for a in argv[1:]):
        print(json.dumps(mark(gate.tracker_of(cfg.load()), vocab.lesson(), argv[1:])))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

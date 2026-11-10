#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Lesson collection for the nightly `retro` (ADR-0015, ADR-0021): the agent writes lessons, it never counts.

Usage: uv run factory/retro.py collect   (JSON: the uncollected lesson comments; marks them collected)
The run facts are the facts plugin's (`factory facts`, ADR-0026).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg  # noqa: E402
import vocab  # noqa: E402

LESSON, COLLECTED = "<!-- factory:lesson -->", "<!-- factory:lesson collected -->"


def collect(tracker, label):
    """ADR-0021: the uncollected lesson comments of every ticket labelled `label`, as [{ticket, url, body}]. The
    label goes first, then each comment taken is marked collected, so a lesson posted meanwhile labels the ticket
    again and the next run takes it; nothing is read twice."""
    out = []
    for i in tracker._list(f"issues?state=all&labels={label}&per_page=100"):
        n = i["number"]
        tracker._edit(n, remove=[label])
        for c in tracker._list(f"issues/{n}/comments"):
            body = c.get("body") or ""
            if LESSON not in body:
                continue
            out.append({"ticket": n, "url": c.get("html_url"), "body": body.replace(LESSON, "").strip()})
            tracker._api(f"issues/comments/{c['id']}", "-X", "PATCH", "-f", f"body={body.replace(LESSON, COLLECTED)}")
    return out


def main(argv):
    import gate
    if argv == ["collect"]:
        print(json.dumps(collect(gate.tracker_of(cfg.load()), vocab.lesson()), indent=2))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

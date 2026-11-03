#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Deterministic run facts for the nightly `retro` (ADR-0015): the agent writes lessons, it never counts.

Usage: uv run factory/retro.py facts --since <ISO time>   (JSON: one object per ticket that reached `done` since)
`ticket_facts` is pure: it takes the ticket's and PR's events, reviews and comments as the GitHub API returns them.
"""
import fnmatch
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config as cfg  # noqa: E402

QUIET = ("triage", "ready")  # stage labels a person sets in the normal flow: not an intervention
MARKER = re.compile(r"<!-- factory:review focus=(\S+) .*?verdict=(\w+)")
SIZE = re.compile(r"(?i)size:\s*about\s*([\d,]+)\s*lines")


def person(user, bots=()):
    login = (user or {}).get("login", "")
    return bool(login) and (user or {}).get("type") != "Bot" and not login.endswith("[bot]") and login not in bots


def labeled(events, prefix):
    return [e for e in events if e.get("event") == "labeled" and e["label"]["name"].startswith(prefix)]


def ticket_facts(number, issue_events, pr_events, reviews, comments, bots=(), body="", pr_lines=None):
    """Facts of one ticket, counted across its whole life (a person's `factory:ready` resets nothing)."""
    stage = lambda name: [e for e in issue_events if e.get("event") == "labeled" and e["label"]["name"] == f"factory:{name}"]
    first_ready, done = (stage("ready") or [{}])[0].get("created_at"), (stage("done") or [{}])[-1].get("created_at")
    marked = [(m[1], m[2], r["body"]) for r in reviews if (m := MARKER.search(r.get("body") or ""))]
    by_focus = {}
    for focus, verdict, text in marked:
        c = by_focus.setdefault(focus, {"approve": 0, "changes": 0, "blocking": 0, "changes_without_finding": 0})
        found = len(re.findall(r"\*\*B\d+\*\*", text))
        c[verdict] = c.get(verdict, 0) + 1
        c["blocking"] += found
        c["changes_without_finding"] += verdict == "changes" and not found
    stops = [e for n in ("blocked", "needs-answers") for e in stage(n)]
    def reason(at):  # the first line of the bot's next comment on the ticket
        later = [c for c in comments if not person(c.get("user"), bots) and (c.get("created_at") or "") >= at]
        return ((later[0].get("body") or "").splitlines() or [""])[0] if later else ""
    actor = lambda e: e.get("actor")
    operator = (
        [{"kind": "label", "label": e["label"]["name"], "by": actor(e)["login"], "at": e["created_at"]}
         for e in issue_events + pr_events
         if e.get("event") in ("labeled", "unlabeled") and e["label"]["name"].startswith("factory:")
         and e["label"]["name"][8:] not in QUIET and person(actor(e), bots)]
        + [{"kind": "review", "by": r["user"]["login"], "at": r.get("submitted_at")} for r in reviews if person(r.get("user"), bots)]
        + [{"kind": "comment", "by": c["user"]["login"], "at": c.get("created_at")} for c in comments if person(c.get("user"), bots)])
    estimate = SIZE.search(body or "")
    return {
        "ticket": number,
        "fix_rounds": len(labeled(pr_events, "factory:fix-")),
        "update_rounds": len(labeled(pr_events, "factory:update-")),
        "reviews": by_focus,
        "stops": [{"stage": e["label"]["name"], "at": e["created_at"], "reason": reason(e["created_at"])} for e in stops],
        "operator_interventions": operator,
        "aborted_runs": None,  # Air run status is not readable from GitHub
        "ready_to_done_hours": round((_t(done) - _t(first_ready)) / 3600, 1) if first_ready and done else None,
        "size": {"estimate": int(estimate[1].replace(",", "")) if estimate else None, "actual": pr_lines},
    }


def changed_lines(files, generated_paths=()):
    """The repository's sizing rule over a PR's files: generated and vendored paths are excluded, a whole deleted
    file counts as one line, removed lines of a remaining file count as modified."""
    total = 0
    for f in files:
        name = f["filename"]
        if name.startswith("skills/vendor/") or any(fnmatch.fnmatchcase(name, g) for g in generated_paths):
            continue
        total += 1 if f.get("status") == "removed" else f.get("additions", 0) + f.get("deletions", 0)
    return total


def _t(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def facts(tracker, since):
    """Facts of every ticket whose `factory:done` label was added at or after `since`."""
    done = tracker.stages["done"]
    out = []
    for i in tracker._list(f"issues?state=all&labels={done}&since={since}&per_page=100"):
        n = i["number"]
        if "pull_request" in i:
            continue
        events = tracker._list(f"issues/{n}/events")
        if not any(e.get("event") == "labeled" and e["label"]["name"] == done and e["created_at"] >= since for e in events):
            continue
        pr = tracker._pr(n)
        pr_events = tracker._list(f"issues/{pr['number']}/events") if pr else []
        reviews = tracker._list(f"pulls/{pr['number']}/reviews") if pr else []
        comments = tracker._list(f"issues/{n}/comments")
        lines = changed_lines(tracker._list(f"pulls/{pr['number']}/files?per_page=100"),
                              cfg.load().get("limits", {}).get("generated_paths", [])) if pr else None
        out.append(ticket_facts(n, events, pr_events, reviews, comments, {tracker.bot, *tracker.trusted},
                                i.get("body") or "", lines))
    return out


def main(argv):
    if len(argv) != 3 or argv[0] != "facts" or argv[1] != "--since":
        print(__doc__)
        return 2
    from gate_github import GitHubGateTracker
    print(json.dumps(facts(GitHubGateTracker.from_config(), argv[2]), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

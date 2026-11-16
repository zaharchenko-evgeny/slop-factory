"""Run facts for the nightly `retro` (ADR-0015, ADR-0026): the agent writes lessons, it never counts.

Usage: factory facts --since <ISO time>   (JSON: `tickets`, one object per ticket that reached `done` since,
                                           and `reviews`, the per-focus totals over them)
`ticket_facts` is pure: it takes the ticket's and PR's events, reviews and comments as the GitHub API returns them.
"""
import fnmatch
import json
import re

import config as cfg
import vocab

QUIET = ("triage", "ready")  # stage labels a person sets in the normal flow: not an intervention
REGRESSION = re.compile(r"(?i)regression|broke")
MARKER = re.compile(r"<!-- factory:review focus=(\S+) .*?verdict=(\w+)")
SIZE = re.compile(r"(?i)size:\s*about\s*([\d,]+)\s*lines")


def person(user, bots=()):
    login = (user or {}).get("login", "")
    return bool(login) and (user or {}).get("type") != "Bot" and not login.endswith("[bot]") and login not in bots


def labeled(events, prefix):
    return [e for e in events if e.get("event") == "labeled" and e["label"]["name"].startswith(prefix)]


def references(text, numbers):
    """The numbers of `numbers` that `text` mentions as `#N` or as an issue or pull request URL."""
    found = re.findall(r"(?<![\w/])#(\d+)\b|/(?:issues|pull)/(\d+)\b", text or "")
    return {int(a or b) for a, b in found} & set(numbers)


def escaped(numbers, later):
    """How many `later` tickets ({number, body, labels}) report a bug in work numbered `numbers` (the ticket and its PR):
    a line that names it with `regression` or `broke`, or a `bug` label with a link to it."""
    return sum(1 for t in later
               if any(references(line, numbers) and REGRESSION.search(line) for line in (t.get("body") or "").splitlines())
               or ("bug" in t.get("labels", ()) and references(t.get("body"), numbers)))


def ticket_facts(number, issue_events, pr_events, reviews, comments, bots=(), body="", pr_lines=None,
                 pr_number=None, merged=False, later=()):
    """Facts of one ticket, counted across its whole life (a person's `factory:ready` resets nothing)."""
    stage = lambda name: [e for e in issue_events if e.get("event") == "labeled" and e["label"]["name"] == f"factory:{name}"]
    first_ready, done = (stage("ready") or [{}])[0].get("created_at"), (stage("done") or [{}])[-1].get("created_at")
    marked = [(m[1], m[2], r["body"], r.get("commit_id")) for r in reviews if (m := MARKER.search(r.get("body") or ""))]
    by_focus = {}
    for i, (focus, verdict, text, head) in enumerate(marked):
        c = by_focus.setdefault(focus, {"reviews": 0, "approve": 0, "changes": 0, "blocking": 0, "changes_without_finding": 0,
                                        "confirmed": 0, "withdrawn": 0, "escaped": 0})
        found = len(re.findall(r"\*\*B\d+\*\*", text))
        c["reviews"] += 1
        c[verdict] = c.get(verdict, 0) + 1
        c["blocking"] += found
        c["changes_without_finding"] += verdict == "changes" and not found
        nxt = next(((v, h) for f, v, _, h in marked[i + 1:] if f == focus), None)  # the same focus's next review
        if verdict == "changes" and nxt and nxt[1] != head:
            c["confirmed"] += found  # a new head followed: the developer acted on it
        elif verdict == "changes" and nxt and nxt[0] == "approve":
            c["withdrawn"] += found  # approved at the same head: a false alarm
    if merged:
        for c in by_focus.values():
            c["escaped"] = escaped({number, pr_number} - {None}, later) if c["approve"] else 0
    stops = [e for n in ("blocked", "needs-answers") for e in stage(n)]
    def reason(at):  # the first line of the bot's next comment on the ticket
        later = [c for c in comments if not person(c.get("user"), bots) and (c.get("created_at") or "") >= at]
        return ((later[0].get("body") or "").splitlines() or [""])[0] if later else ""
    actor = lambda e: e.get("actor")
    operator = (
        [{"kind": "label", "label": e["label"]["name"], "by": actor(e)["login"], "at": e["created_at"]}
         for e in issue_events + pr_events
         if e.get("event") in ("labeled", "unlabeled") and e["label"]["name"].startswith(vocab.stage_prefix())
         and e["label"]["name"][len(vocab.stage_prefix()):] not in QUIET and person(actor(e), bots)]
        + [{"kind": "review", "by": r["user"]["login"], "at": r.get("submitted_at")} for r in reviews if person(r.get("user"), bots)]
        + [{"kind": "comment", "by": c["user"]["login"], "at": c.get("created_at")} for c in comments if person(c.get("user"), bots)])
    estimate = SIZE.search(body or "")
    return {
        "ticket": number,
        "fix_rounds": len(labeled(pr_events, vocab.prefix("fix-<tier>"))),
        "update_rounds": len(labeled(pr_events, vocab.prefix("update-<tier>"))),
        "reviews": by_focus,
        "stops": [{"stage": e["label"]["name"], "at": e["created_at"], "reason": reason(e["created_at"])} for e in stops],
        "operator_interventions": operator,
        "aborted_runs": None,  # Air run status is not readable from GitHub
        "ready_to_done_hours": round((_t(done) - _t(first_ready)) / 3600, 1) if first_ready and done else None,
        "size": {"estimate": int(estimate[1].replace(",", "")) if estimate else None, "actual": pr_lines},
    }


def review_totals(tickets):
    """The per-focus counters of `ticket_facts` summed over `tickets`."""
    totals = {}
    for t in tickets:
        for focus, c in t["reviews"].items():
            sums = totals.setdefault(focus, dict.fromkeys(c, 0))
            for k, v in c.items():
                sums[k] += v
    return totals


def changed_lines(files, generated_paths=()):
    """The repository's sizing rule over a PR's files: generated and vendored paths are excluded, a whole deleted
    file counts as one line, removed lines of a remaining file count as modified."""
    total = 0
    for f in files:
        name = f["filename"]
        if any(fnmatch.fnmatchcase(name, g) for g in generated_paths):
            continue
        total += 1 if f.get("status") == "removed" else f.get("additions", 0) + f.get("deletions", 0)
    return total


def _t(iso):
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def facts(tracker, since):
    """Facts of every GitHub-issue ticket whose `factory:done` label was added at or after `since`, in every
    environment (#1426: a scoped tracker refuses `_list` itself)."""
    import retro
    out = []
    for t in retro.issue_trackers(tracker):
        out += _facts(t, since)
    return {"tickets": out, "reviews": review_totals(out)}


def _facts(tracker, since):
    """`facts` on one GitHub-issue tracker: the ticket facts."""
    done = tracker.stages["done"]
    out = []
    later = [{"number": i["number"], "body": i.get("body") or "", "created_at": i["created_at"],
              "labels": [x["name"] for x in i.get("labels") or []]}
             for i in tracker._list(f"issues?state=all&since={since}&per_page=100") if "pull_request" not in i]
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
        finished = max(e["created_at"] for e in events if e.get("event") == "labeled" and e["label"]["name"] == done)
        out.append(ticket_facts(n, events, pr_events, reviews, comments, {tracker.bot, *tracker.trusted},
                                i.get("body") or "", lines, pr and pr["number"], bool(pr and pr.get("merged")),
                                [t for t in later if t["created_at"] > finished and t["number"] != n]))
    return out


def main(argv):
    import gate
    if len(argv) != 2 or argv[0] != "--since":
        print(__doc__)
        return 2
    print(json.dumps(facts(gate.tracker_of(cfg.load()), argv[1]), indent=2))
    return 0

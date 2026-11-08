"""The Space code host behind the gate's vcs slot (factory/ports/system.py): the merge request of a ticket, its record
and facts (the keys of gate_port.PR_DEFAULTS), and the effects it applies on it.

`api(path)` returns the decoded JSON of `GET <path>` on the Space instance (project `vcs.project`, alias
`code_host.project`). A merge request is `{number, title, state: open|merged|closed, branch, head, ci:
success|failed|running|none, labels: [str], reviews: [{author, verdict: approve|changes, commit_id, submitted_at,
body}]}`.

Optional keys: `dry_run: {state: success|failed|running}` (the TeamCity dry run; it decides `ci` when present, else
`ci` is used), `role: owner|qa` on a review, and `behind` (the base has commits the head lacks). They surface as the
facts `dry_run` and, per review, `role`; `behind: false` makes `update-branch` a no-op.

Events: `space_event(payload)` reads a Space webhook payload `{eventType: "space.merge_request.<action>", data:
{action, merge_request: {number, title, branch}, project: {key}}}` into `{number, action, project, ticket}`;
`trigger(payload)` is the plugin's `gate.trigger` hook over it and `SpaceCodeHost.event` the Vcs port's.

Write side: `write(method, path, body)` performs the request and returns the decoded JSON; it raises on an HTTP error.
`SpaceCodeHost.effects` declares `label`, `unlabel`, `review`, `update-branch` and `merge` on `pr`; the gate applies
them through factory/effects.py. Each acts on the ticket's open merge request, reading first and writing only on a
difference; it returns True when it wrote, and raises RuntimeError(`<kind>: ...`) when there is no merge request or
the API fails.
"""
import json
import os
import re
import urllib.request

import config
import effects as effects_mod
from gate_port import PR_DEFAULTS, ticket_from_ref
from ports.records import PrRecord
from ports.system import Effect, Event

CI = {"success": "green", "failed": "red", "running": "pending"}
MARKER = re.compile(r"<!-- factory:review focus=(\S+) round=(\d+) verdict=(approve|changes) -->")


def is_payload(d):
    """Whether `d` is a Space merge request payload."""
    event_name = d.get("eventType") or d.get("event_name")
    return str(event_name or "").startswith("space.") or isinstance(d.get("merge_request"), dict) \
        or isinstance((d.get("data") or {}).get("merge_request"), dict)


def space_event(d):
    """The merge request an event names: `{number, action, project, ticket}`; ticket is None when no branch or title names one."""
    data = d.get("data") if isinstance(d.get("data"), dict) else d
    mr = data.get("merge_request")
    if not isinstance(mr, dict) or not isinstance(mr.get("number"), int) or mr["number"] < 1:
        raise ValueError("payload has no merge request number")
    action = data.get("action") or str(d.get("eventType") or "").rpartition(".")[2] or "event"
    return {"number": mr["number"], "action": action, "project": (data.get("project") or {}).get("key"),
            "ticket": ticket_from_ref(mr.get("branch")) or ticket_from_ref(mr.get("title"))}


def trigger(d):
    """The `gate.trigger` hook: `{kind, number, action, repository, ticket}` of a Space merge request payload, None for
    any other payload; ValueError when it names no merge request."""
    if not is_payload(d):
        return None
    ev = space_event(d)
    return {"kind": "merge_request", "number": ev["number"], "action": ev["action"], "repository": ev["project"],
            "ticket": ev["ticket"]}


def _open(host, e):
    """(merge request, its API path) of the effect's ticket; None for a `merge` of a merge request already merged."""
    kind, mr = e["kind"], host.merge_request(e["ticket"])
    if mr is None or mr["state"] != "open":
        if kind == "merge" and mr is not None and mr["state"] == "merged":
            return None
        raise RuntimeError(f"{kind}: no open merge request for ticket {e['ticket']}")
    return mr, host._mr_path(mr["number"])


def _fx_label(host, e, ctx):
    mr, path = _open(host, e)
    kind, value = e["kind"], e.get("value")
    labels = list(mr.get("labels") or [])
    gone = [x for x in labels if x.startswith(value[:-1])] if value.endswith("*") else [x for x in labels if x == value]
    if kind == "label" and value not in labels:
        host._send(kind, "POST", f"{path}/labels", {"label": value})
        return True
    for x in gone if kind == "unlabel" else []:
        host._send(kind, "DELETE", f"{path}/labels/{x}")
    return bool(gone) and kind == "unlabel"


def _fx_review(host, e, ctx):
    mr, path = _open(host, e)
    head = e.get("head") or mr["head"]
    if head != mr["head"]:
        return False  # the head moved since the gates judged it: the next sweep reads it again
    verdict = "approve" if str(e.get("value")).startswith("approve") else "changes"
    marker = f"<!-- factory:review focus={e.get('focus')} round={e.get('round') or 1} verdict={verdict} -->"
    if any(r["commit_id"] == head and marker in (r.get("body") or "") for r in mr.get("reviews") or []):
        return False
    lines = [f"**{x['id']}** {x['file']} \u2014 {x['finding']}" for x in e.get("findings") or []]
    body = "\n\n".join(x for x in (f"[factory] {e.get('summary') or ''}".strip(), "\n".join(lines), marker) if x)
    host._send("review", "POST", f"{path}/reviews", {"commitId": head, "verdict": verdict, "body": body})
    return True


def _fx_branch(host, e, ctx):
    """`update-branch` and `merge`, pinned to the head the gates judged."""
    found = _open(host, e)
    if found is None:
        return False  # merged already
    (mr, path), kind = found, e["kind"]
    head = e.get("head") or mr["head"]
    if head != mr["head"]:
        return False  # the head moved since the gates judged it: the next sweep reads it again
    if kind == "update-branch" and "behind" in mr and not mr["behind"]:
        return False  # the base has nothing the head lacks
    host._send(kind, "POST", f"{path}/{'merge' if kind == 'merge' else 'update-branch'}",
               {"expectedHead": head, **({"strategy": "merge"} if kind == "merge" else {})})
    return True


class SpaceCodeHost:
    name = "space"
    # the facts a Space merge request owns: they replace the tracker's when the ticket has one (gate_tracker.overlay)
    pr_facts = ("pr", "head", "branch", "pr_state", "ci", "dry_run", "pr_labels", "reviews", "round_complete",
                "round_verdict", "round_head")
    effects = (
        Effect("label", "pr", _fx_label), Effect("unlabel", "pr", _fx_label),
        Effect("review", "pr", _fx_review, remote=True),
        Effect("update-branch", "pr", _fx_branch, remote=True), Effect("merge", "pr", _fx_branch, remote=True),
    )
    trigger = staticmethod(trigger)

    def __init__(self, project, api, write=None):
        self.project, self.api, self.write = project, api, write

    @classmethod
    def from_config(cls, path=None, descriptor=None, http=None, env=os.environ, **_):
        """The host of the vcs block (`vcs:`, alias `code_host:`) `{adapter: space, url, project}` of `descriptor`
        (else the team's); the bearer token is the env SPACE_TOKEN. `http(method, path, body)` replaces the urllib
        runner (tests stub it); other runners (the tracker's `run`) are not Space's and are ignored."""
        import yaml
        from pathlib import Path
        c = descriptor if descriptor is not None else yaml.safe_load(Path(path).read_text()) if path else config.load()
        h = config.vcs(c)
        if not h.get("url") or not h.get("project"):
            raise ValueError("descriptor: vcs adapter space needs vcs.url and vcs.project (alias code_host)")
        if not env.get("SPACE_TOKEN"):
            raise ValueError("SPACE_TOKEN is not set")

        def write(method, path, body=None):
            req = urllib.request.Request(f"{h['url'].rstrip('/')}/api/http/{path}", method=method,
                                         data=None if body is None else json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {env['SPACE_TOKEN']}",
                                                  "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
            return json.loads(raw) if raw else None
        write = http or write
        return cls(h["project"], lambda path: write("GET", path), write)

    def _merge_requests(self):
        return self.api(f"projects/key:{self.project}/code-reviews")["data"]

    def ticket_for_pr(self, number, repository=None):
        mr = self.api(f"projects/key:{repository or self.project}/code-reviews/number:{number}")
        return ticket_from_ref(mr.get("branch")) or ticket_from_ref(mr.get("title"))

    def event(self, payload):
        """The Event of a Space merge request payload (the Vcs port), else None when it names no factory ticket."""
        try:
            ev = space_event(payload)
        except ValueError:
            return None
        return None if ev["ticket"] is None else Event(ev["ticket"], "merge_request", ev["action"], ev["number"],
                                                         ev["project"], payload)

    def merge_request(self, ticket):
        """The ticket's merge request: the open one with the highest number, else the latest closed or merged."""
        mine = [m for m in self._merge_requests() if (ticket_from_ref(m.get("branch")) or ticket_from_ref(m.get("title"))) == ticket]
        return max(mine, key=lambda m: (m["state"] == "open", m["number"]), default=None)

    @staticmethod
    def _reviews(mr):
        """The reviews of `mr` as the records give them; a review with the factory's marker is an agent review."""
        out = []
        for r in mr.get("reviews") or []:
            m = MARKER.search(r.get("body") or "")
            out.append({"author": r.get("author"), "verdict": r.get("verdict"), "commit_id": r.get("commit_id"),
                        "submitted_at": r.get("submitted_at"), "body": r.get("body") or "", "agent": bool(m),
                        "focus": m[1] if m else None, "round": int(m[2]) if m else None,
                        **({"role": r["role"]} if r.get("role") else {})})
        return out

    def read(self, subject, want=frozenset()):
        """The PrRecord of ticket `subject.id`'s merge request (None without one); Space holds no tickets."""
        if subject.kind != "pr":
            raise ValueError(f"space: holds pull requests only, not a {subject.kind}")
        mr = self.merge_request(subject.id)
        if mr is None:
            return None
        dry_run = (mr.get("dry_run") or {}).get("state")
        return PrRecord(repo=subject.repo or "primary", number=mr["number"], head=mr["head"], branch=mr.get("branch"),
                        state="open" if mr["state"] == "open" else "closed", merged=mr["state"] == "merged",
                        labels=list(mr.get("labels") or []), reviews=self._reviews(mr),
                        ci=CI.get(dry_run or mr.get("ci"), "none"),
                        behind=bool(mr["behind"]) if "behind" in want and "behind" in mr else None)

    def facts(self, ticket):
        mr = self.merge_request(ticket)
        if mr is None:
            return dict(PR_DEFAULTS)
        reviews = [{"author": r["author"], "verdict": r["verdict"], "commit_id": r["commit_id"],
                    "submitted_at": r.get("submitted_at"), **({"role": r["role"]} if r.get("role") else {})}
                   for r in mr.get("reviews") or []]
        dry_run = (mr.get("dry_run") or {}).get("state")
        latest = {}  # a reviewer's later review wins
        for r in sorted(reviews, key=lambda r: r.get("submitted_at") or ""):
            latest[r["author"]] = r
        current = [r for r in latest.values() if r["commit_id"] == mr["head"]]
        complete = bool(current)  # Space has no review focuses: any review of the head closes the round
        verdict = None if not complete else "changes" if any(r["verdict"] == "changes" for r in current) else "approve"
        return {**PR_DEFAULTS, "round_complete": complete, "round_verdict": verdict, "round_head": mr["head"] if complete else None, "pr": mr["number"], "head": mr["head"], "branch": mr.get("branch"), "pr_state": mr["state"],
                "ci": CI.get(dry_run or mr.get("ci"), "none"), "dry_run": dry_run, "pr_labels": list(mr.get("labels") or []), "reviews": reviews}

    def _mr_path(self, number):
        return f"projects/key:{self.project}/code-reviews/number:{number}"

    def _send(self, kind, method, path, body=None):
        try:
            return self.write(method, path, body)
        except Exception as err:
            raise RuntimeError(f"{kind}: {err}") from err

    def apply(self, e):
        """Apply effect `e` on the ticket's merge request (every effect here targets it) through factory/effects.py."""
        return effects_mod.apply(self, {**e, "on": "pr"})

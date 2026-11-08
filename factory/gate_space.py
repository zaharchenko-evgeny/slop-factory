"""Read side of the Space code host: the merge request of a ticket and its facts (the keys of gate_port.PR_DEFAULTS).

`api(path)` returns the decoded JSON of `GET <path>` on the Space instance (project `code_host.project`).
A merge request is `{number, title, state: open|merged|closed, branch, head, ci: success|failed|running|none,
labels: [str], reviews: [{author, verdict: approve|changes, commit_id, submitted_at}]}`.

Optional keys: `dry_run: {state: success|failed|running}` (the TeamCity dry run; it decides `ci` when present, else
`ci` is used) and `role: owner|qa` on a review. They surface as the facts `dry_run` and, per review, `role`.

Events: `space_event(payload)` reads a Space webhook payload `{eventType: "space.merge_request.<action>", data:
{action, merge_request: {number, title, branch}, project: {key}}}` into `{number, action, project, ticket}`.

Write side: `write(method, path, body)` performs the request and returns the decoded JSON; it raises on an HTTP error.
`SpaceCodeHost.apply(effect)` carries out the effects `review`, `label`, `unlabel`, `update-branch` and `merge` on the
ticket's open merge request, reading first and writing only on a difference; it returns True when it wrote, and
raises RuntimeError(`<kind>: ...`) when there is no merge request or the API fails.
"""
import json
import os
import urllib.request

from gate_port import PR_DEFAULTS, ticket_from_ref

CI = {"success": "green", "failed": "red", "running": "pending"}


def space_event(d):
    """The merge request an event names: `{number, action, project, ticket}`; ticket is None when no branch or title names one."""
    data = d.get("data") if isinstance(d.get("data"), dict) else d
    mr = data.get("merge_request")
    if not isinstance(mr, dict) or not isinstance(mr.get("number"), int) or mr["number"] < 1:
        raise ValueError("payload has no merge request number")
    action = data.get("action") or str(d.get("eventType") or "").rpartition(".")[2] or "event"
    return {"number": mr["number"], "action": action, "project": (data.get("project") or {}).get("key"),
            "ticket": ticket_from_ref(mr.get("branch")) or ticket_from_ref(mr.get("title"))}


class SpaceCodeHost:
    def __init__(self, project, api, write=None):
        self.project, self.api, self.write = project, api, write

    def _merge_requests(self):
        return self.api(f"projects/key:{self.project}/code-reviews")["data"]

    def ticket_for_pr(self, number, repository=None):
        mr = self.api(f"projects/key:{repository or self.project}/code-reviews/number:{number}")
        return ticket_from_ref(mr.get("branch")) or ticket_from_ref(mr.get("title"))

    def event(self, payload):
        """The Event of a Space merge request payload (the Vcs port), else None when it names no factory ticket."""
        from ports.system import Event
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
        kind, value, mr = e["kind"], e.get("value"), None
        if kind not in ("review", "label", "unlabel", "update-branch", "merge"):
            raise ValueError(f"unknown effect: {kind}")
        mr = self.merge_request(e["ticket"])
        if mr is None or mr["state"] != "open":
            if kind == "merge" and mr is not None and mr["state"] == "merged":
                return False
            raise RuntimeError(f"{kind}: no open merge request for ticket {e['ticket']}")
        path, head = self._mr_path(mr["number"]), e.get("head") or mr["head"]
        if kind in ("label", "unlabel"):
            labels = list(mr.get("labels") or [])
            gone = [x for x in labels if x.startswith(value[:-1])] if value.endswith("*") else [x for x in labels if x == value]
            if kind == "label" and value not in labels:
                self._send(kind, "POST", f"{path}/labels", {"label": value})
                return True
            for x in gone if kind == "unlabel" else []:
                self._send(kind, "DELETE", f"{path}/labels/{x}")
            return bool(gone) and kind == "unlabel"
        if head != mr["head"]:
            return False  # the head moved since the gates judged it: the next sweep reads it again
        if kind == "review":
            verdict = "approve" if str(value).startswith("approve") else "changes"
            focus = e.get("focus")
            marker = f"<!-- factory:review focus={focus} round={e.get('round') or 1} verdict={verdict} -->"
            if any(r["commit_id"] == head and marker in (r.get("body") or "") for r in mr.get("reviews") or []):
                return False
            lines = [f"**{x['id']}** {x['file']} \u2014 {x['finding']}" for x in e.get("findings") or []]
            body = "\n\n".join(x for x in (f"[factory] {e.get('summary') or ''}".strip(), "\n".join(lines), marker) if x)
            self._send(kind, "POST", f"{path}/reviews", {"commitId": head, "verdict": verdict, "body": body})
            return True
        self._send(kind, "POST", f"{path}/{'merge' if kind == 'merge' else 'update-branch'}",
                   {"expectedHead": head, **({"strategy": "merge"} if kind == "merge" else {})})
        return True


def from_config(c, env=os.environ):
    """The SpaceCodeHost of `code_host: {adapter: space, url, project}`; the bearer token is the env SPACE_TOKEN."""
    h = c.get("code_host") or {}
    if not h.get("url") or not h.get("project"):
        raise ValueError("descriptor: code_host.adapter space needs code_host.url and code_host.project")
    if not env.get("SPACE_TOKEN"):
        raise ValueError("SPACE_TOKEN is not set")

    def write(method, path, body=None):
        req = urllib.request.Request(f"{h['url'].rstrip('/')}/api/http/{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {env['SPACE_TOKEN']}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        return json.loads(raw) if raw else None
    return SpaceCodeHost(h["project"], lambda path: write("GET", path), write)

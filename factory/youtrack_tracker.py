"""The YouTrack ticket store (the TicketStore port, factory/gate_port.py) over the REST API, on `urllib`.

Attrs are tag names. Every request goes through one runner `run(method, path, body=None) -> parsed JSON`, so tests
stub it; fields are always requested with `fields=`. A tag missing in YouTrack is created by setup, never here.
Links follow YouTrack's defaults: `Depend` (INWARD: this ticket depends on the listed ones, OUTWARD: they depend
on it) and `Subtask` (INWARD: the listed ticket is the parent, OUTWARD: the listed tickets are the children).
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import github_graphql

ISSUE_FIELDS = ("idReadable,summary,description,resolved,updated,reporter(login),tags(id,name),"
                "customFields(name,value(name,login)),links(direction,linkType(name),issues(idReadable,resolved,tags(name)))")
CHILD_FIELDS = "idReadable,resolved,tags(name),customFields(name,value(name))"
ACTIVITY_FIELDS = "timestamp,author(login),category(id),added(name),removed(name),field(name)"
ACTIVITY_CATEGORIES = "TagsCategory,CustomFieldCategory,IssueResolvedCategory"
COMMENT_FIELDS = "text,created,author(login)"
TOP = "$top=1000"
CHILD_MARK = "<!-- factory:child of={n} -->"


class MissingTag(RuntimeError):
    """The tag does not exist in YouTrack: setup creates tags, the gate never does."""


def urllib_runner(base_url, token):
    def run(method, path, body=None):
        req = urllib.request.Request(base_url.rstrip("/") + path, method=method, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"},
            data=None if body is None else json.dumps(body).encode())
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
        return json.loads(raw) if raw.strip() else None
    return run


def iso(ms):
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class YouTrackTicketStore:
    """`state_field` names the single-value custom field of a `field` stage store (`{name: State}`); without it
    stages are tags. `done_state` is the State a closed ticket gets; `bots` the logins that count as agents;
    `writers` the logins that may answer a question the factory asked."""

    def __init__(self, run, state_field=None, done_state="Done", project=None, bots=(), writers=(), assignee_field="Assignee"):
        self.run, self.state_field, self.done_state, self.project = run, state_field, done_state, project
        self.bots, self.writers, self.assignee_field = set(bots), set(writers), assignee_field

    @classmethod
    def from_config(cls, tracker, run=None, **kw):
        """From the descriptor's `tracker`: `url`, `token_env` (default YOUTRACK_TOKEN), `done_state`, `project`."""
        run = run or urllib_runner(tracker["url"], os.environ[tracker.get("token_env", "YOUTRACK_TOKEN")])
        return cls(run, done_state=tracker.get("done_state", "Done"), project=tracker.get("project"), **kw)

    # --- reads
    def _issue(self, n):
        return self.run("GET", f"/api/issues/{n}?fields={ISSUE_FIELDS}")

    @staticmethod
    def _field(raw, name):
        value = next((f.get("value") for f in raw.get("customFields") or [] if f.get("name") == name), None)
        return [value] if isinstance(value, dict) else value or []

    def _attrs(self, raw):
        attrs = [t["name"] for t in raw.get("tags") or []]
        return attrs + [v["name"] for v in self._field(raw, self.state_field)] if self.state_field else attrs

    def issue(self, n):
        raw = self._issue(n)
        return {"title": raw.get("summary") or "", "body": raw.get("description") or "",
                "state": "closed" if raw.get("resolved") else "open", "close_reason": None,
                "author": (raw.get("reporter") or {}).get("login"), "updated_at": iso(raw.get("updated")),
                "attrs": self._attrs(raw), "assignees": [u["login"] for u in self._field(raw, self.assignee_field)]}

    def events(self, n):
        """Tag and field changes as labeled/unlabeled; a resolve or an unresolve as closed/reopened."""
        out = []
        for a in self.run("GET", f"/api/issues/{n}/activities?categories={ACTIVITY_CATEGORIES}&fields={ACTIVITY_FIELDS}&{TOP}"):
            base = {"actor": (a.get("author") or {}).get("login"), "at": iso(a.get("timestamp"))}
            category = (a.get("category") or {}).get("id")
            if category == "IssueResolvedCategory":
                out.append({"kind": "closed" if a.get("added") is True or a.get("removed") is False else "reopened",
                            "value": None, **base})
            elif category == "TagsCategory" or (category == "CustomFieldCategory" and self.state_field
                                                and (a.get("field") or {}).get("name") == self.state_field):
                for kind, key in (("unlabeled", "removed"), ("labeled", "added")):
                    out += [{"kind": kind, "value": x["name"], **base} for x in a.get(key) or []]
        return out

    def comments(self, n):
        return [{"author": (login := (c.get("author") or {}).get("login", "")),
                 "author_is_bot": login in self.bots or login.endswith("[bot]"), "body": c.get("text") or "",
                 "at": iso(c.get("created")) or ""}
                for c in self.run("GET", f"/api/issues/{n}/comments?fields={COMMENT_FIELDS}&{TOP}")]

    def _links(self, n, link, direction):
        return [i for l in self._issue(n).get("links") or []
                if l["linkType"]["name"] == link and l["direction"] == direction for i in l.get("issues") or []]

    def open_blockers(self, n):
        return [i["idReadable"] for i in self._links(n, "Depend", "INWARD") if not i.get("resolved")]

    def blocking(self, n):
        return [{"number": i["idReadable"], "state": "closed" if i.get("resolved") else "open"}
                for i in self._links(n, "Depend", "OUTWARD")]

    def parent(self, n):
        return next((i["idReadable"] for i in self._links(n, "Subtask", "INWARD")), None)

    def children(self, n):
        return [{"number": i["idReadable"], "state": "closed" if i.get("resolved") else "open", "not_planned": False,
                 "attrs": self._attrs(i)} for i in self._links(n, "Subtask", "OUTWARD")]

    def tickets_by_stage(self, attrs, open_only=False, now=None):
        """Ids of the tickets holding one of `attrs`: the open ones and, unless `open_only`, those resolved in the
        last `github_graphql.CLOSED_HOURS`."""
        if not attrs:
            return []
        query = " or ".join(f"tag: {{{a}}}" for a in attrs)
        if not open_only:
            query = f"({query}) and (#Unresolved or resolved date: {{minus {github_graphql.CLOSED_HOURS}h}} .. Now)"
        else:
            query = f"({query}) and #Unresolved"
        found = self.run("GET", f"/api/issues?query={urllib.parse.quote(query)}&fields=idReadable&{TOP}")
        return sorted(i["idReadable"] for i in found)

    def can_answer(self, login):
        return login in self.writers

    # --- writes
    def _tag_id(self, name):
        tag = next((t for t in self.run("GET", f"/api/tags?fields=id,name&query={urllib.parse.quote(name)}&{TOP}")
                    if t["name"] == name), None)
        if tag is None:
            raise MissingTag(f"tag {name!r} does not exist in YouTrack: run setup to create it")
        return tag["id"]

    def edit_attrs(self, n, add=(), remove=()):
        """Tags `remove`d and `add`ed; writes only the ones that differ and returns whether it wrote."""
        have = {t["name"]: t["id"] for t in self._issue(n).get("tags") or []}
        wrote = False
        for name in dict.fromkeys(remove):
            if name in have:
                self.run("DELETE", f"/api/issues/{n}/tags/{have[name]}")
                wrote = True
        for name in dict.fromkeys(add):
            if name not in have:
                self.run("POST", f"/api/issues/{n}/tags?fields=name", {"id": self._tag_id(name)})
                wrote = True
        return wrote

    def add_attr(self, n, attr):
        return self.edit_attrs(n, [attr])

    def remove_attr(self, n, *attrs):
        return self.edit_attrs(n, remove=attrs)

    def set_field(self, n, name, value, kind="StateIssueCustomField"):
        """Set the single-value field `name` to `value` (a name; None clears); False when it already holds it."""
        current = [v.get("name") or v.get("login") for v in self._field(self._issue(n), name)]
        if current == ([] if value is None else [value]):
            return False
        value = None if value is None else {"login": value} if "User" in kind else {"name": value}
        self.run("POST", f"/api/issues/{n}?fields=idReadable", {"customFields": [{"name": name, "$type": kind, "value": value}]})
        return True

    def set_stage(self, n, stage, have):
        """Stage `stage` replaces the stage attrs `have`: tags, or the one value of the state field."""
        if self.state_field:
            return self.set_field(n, self.state_field, stage)
        return self.edit_attrs(n, [stage], [x for x in have if x != stage])

    def assign(self, n, people):
        """The Assignee field holds the first of `people` (it holds one user); [] clears it."""
        return self.set_field(n, self.assignee_field, people[0] if people else None, "SingleUserIssueCustomField")

    def comment(self, n, body):
        self.run("POST", f"/api/issues/{n}/comments?fields=id", {"text": body})

    def close(self, n):
        self.set_field(n, self.state_field or "State", self.done_state)

    def _command(self, n, query):
        self.run("POST", f"/api/issues/{n}/execute", {"query": query})

    def _create(self, title, body, tags=()):
        issue = self.run("POST", "/api/issues?fields=idReadable", {
            "summary": title, "description": body, **({"project": {"shortName": self.project}} if self.project else {})})
        self.edit_attrs(issue["idReadable"], tags)
        return issue["idReadable"]

    def create_children(self, n, children, ready):
        """Per child: reuse the subtask of that title, then add each link that is missing (a retry converges). The
        child is chained behind the previous one and blocks the parent; `ready` is set last, once the links exist."""
        subs = {c["summary"]: c["idReadable"] for c in self.run(
            "GET", f"/api/issues?query={urllib.parse.quote(f'subtask of: {n}')}&fields=idReadable,summary&{TOP}")}
        blockers, wrote, prev = set(self.open_blockers_all(n)), False, None
        for c in children:
            c = c if isinstance(c, dict) else {"title": str(c), "body": ""}
            child = subs.get(c["title"])
            if child is None:
                child = self._create(c["title"], f"{c.get('body', '')}\n\n{CHILD_MARK.format(n=n)}")
                self._command(child, f"subtask of {n}")
                wrote = True
            if prev and prev not in self.open_blockers_all(child):
                self._command(child, f"depends on {prev}")
                wrote = True
            if child not in blockers:
                self._command(n, f"depends on {child}")
                wrote = True
            wrote = self.edit_attrs(child, [ready]) or wrote
            prev = child
        return wrote

    def open_blockers_all(self, n):
        """Ids of every ticket `n` depends on, resolved or not."""
        return [i["idReadable"] for i in self._links(n, "Depend", "INWARD")]

    def park(self, n, check, triage):
        """Open or reuse the one `Main is red in Air: <check>` ticket, then block ticket `n` by it."""
        title = f"Main is red in Air: {check}"
        found = self.run("GET", f"/api/issues?query={urllib.parse.quote(f'#Unresolved summary: {{{title}}}')}"
                                f"&fields=idReadable,summary&{TOP}")
        red = next((i["idReadable"] for i in found if i["summary"] == title), None)
        if red is None:
            red = self._create(title, f"[factory] The required check `{check}` fails identically on unchanged `main` in Air "
                                      f"(found while working on {n}). Fix main; the tickets blocked by this one restart "
                                      "when it closes.", [triage])
        if red in self.open_blockers_all(n):
            return False
        self._command(n, f"depends on {red}")
        return True

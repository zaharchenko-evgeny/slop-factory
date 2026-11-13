"""The YouTrack ticket store (the TicketStore port, factory/gate_port.py) over the REST API, on `urllib`.

Attrs are tag names. Every request goes through one runner `run(method, path, body=None) -> parsed JSON`, so tests
stub it; fields are always requested with `fields=`. A tag missing in YouTrack is created by setup, never here.
Links follow YouTrack's defaults: `Depend` (INWARD: this ticket depends on the listed ones, OUTWARD: they depend
on it) and `Subtask` (INWARD: the listed ticket is the parent, OUTWARD: the listed tickets are the children).
The store declares the ticket effects it applies itself (`effects`, handlers in factory_plugins/youtrack/effects.py)
and the descriptor store kinds it holds states and commands in.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import effects
from gate_port import CLOSED_HOURS, child_links, child_order
from ports.system import Effect
from scopes import matches

ISSUE_FIELDS = ("idReadable,summary,description,resolved,updated,reporter(login),tags(id,name),project(shortName),"
                "customFields(name,value(name,login)),links(direction,linkType(name),issues(idReadable,resolved,tags(name)))")
CHILD_FIELDS = "idReadable,resolved,tags(name),project(shortName),customFields(name,value(name))"
ACTIVITY_FIELDS = "timestamp,author(login),category(id),added(name),removed(name),field(name)"
ACTIVITY_CATEGORIES = "TagsCategory,CustomFieldCategory,IssueResolvedCategory"
COMMENT_FIELDS = "text,created,author(login)"
TOP = "$top=1000"
CHILD_MARK = "<!-- factory:child of={n} -->"


class MissingTag(RuntimeError):
    """The tag does not exist in YouTrack: setup creates tags, the gate never does."""


class MissingField(RuntimeError):
    """The custom field does not exist in the project: an administrator creates fields, deploy never does."""


class MissingRepository(RuntimeError):
    """The ticket has no repository field value, or one that is not a repository of the definition."""


REPO_STAGES = ("ready", "changes-requested")  # the stages that wake one copy per repository


def repo_stage_tags(config):
    """The stage tags that carry a repository suffix (`<tag>-<repo>`): those of `REPO_STAGES` in a YouTrack definition
    with `tracker.repository_field`; none otherwise."""
    tracker = config.get("tracker") or {}
    if tracker.get("adapter") != "youtrack" or not tracker.get("repository_field"):
        return []
    tags = [((config.get("states") or {}).get(s) or {}).get("store", {}).get("tag") for s in REPO_STAGES]
    return [t for t in tags if isinstance(t, str)]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The token must not follow a redirect to another origin: a redirect is an error."""

    def redirect_request(self, *args, **kw):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def urllib_runner(base_url, token):
    def run(method, path, body=None):
        req = urllib.request.Request(base_url.rstrip("/") + path, method=method, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json"},
            data=None if body is None else json.dumps(body).encode())
        try:
            with _OPENER.open(req, timeout=60) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:  # say which request failed (#1323); still an HTTPError for the callers
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                detail = ""
            e.msg = f"{e.msg} ({method} {path.split('?')[0]}: {detail})"
            raise
        return json.loads(raw) if raw.strip() else None
    return run


def iso(ms):
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class YouTrackTicketStore:
    """`state_field` names the single-value custom field of a `field` stage store (`{name: State}`); without it
    stages are tags. `done_state` is the State a closed ticket gets; `bots` the logins that count as agents;
    `writers` the logins that may answer a question the factory asked. With `repository_field`, the stage tags in
    `repo_stages` are stored as `<tag>-<repo>` (the repository is the field's value, one of `repositories`) and read
    back as `<tag>`. `scope` (`{field, value[, type]}`, #1069) is the environment's share of the project: its ticket
    queries hold only the tickets whose field holds the value, and a ticket it creates gets the value."""

    state_kinds, command_kinds = ("field", "tag"), ("tag",)  # a state is a field value or a tag, a command a tag
    # (system, effect, ctx) -> wrote, on the gate tracker that holds this store as its `tickets`: a stage is a tag (per
    # repository with `repository_field`) or the state field's value; a label and a `set-field` are tags.
    effects = (
        Effect("stage", "ticket", "factory_plugins.youtrack.effects:stage"),
        Effect("label", "ticket", "factory_plugins.youtrack.effects:tag"),
        Effect("unlabel", "ticket", "factory_plugins.youtrack.effects:tag"),
        Effect("set-field", "ticket", "factory_plugins.youtrack.effects:set_field"),
        Effect("field", "ticket", "factory_plugins.youtrack.effects:field"),
    )

    def __init__(self, run, state_field=None, done_state="Done", project=None, bots=(), writers=(), assignee_field="Assignee",
                 repository_field=None, repo_stages=(), repositories=(), scope=None, projects=None, full_names=None,
                 state_mirror=None, state_mirror_field="State", repository_scopes=None):
        self.run, self.state_field, self.done_state, self.project = run, state_field, done_state, project
        self.scope = scope
        self.bots, self.writers, self.assignee_field = set(bots), set(writers), assignee_field
        self.repository_field, self.repo_stages, self.repositories = repository_field, tuple(repo_stages), tuple(repositories)
        self.projects = dict(projects or {})  # linked YouTrack project -> owner/repo (short names are case-sensitive)
        # owner/repo (lower case) -> repository name; without it a repository's name is its repo part
        self.full_names = {k.lower(): v for k, v in (full_names or {}).items()}
        # #1260: {stage: value} the gate also writes to `state_mirror_field` (State) beside the stage tag
        self.state_mirror, self.state_mirror_field = dict(state_mirror or {}), state_mirror_field
        # owner/repo -> its scope on this tracker (#1259): a child named for that repository is created in it
        self.repository_scopes = dict(repository_scopes or {})

    @classmethod
    def from_config(cls, tracker, run=None, **kw):
        """From the descriptor's `tracker`: `url`, `token_env` (default YOUTRACK_TOKEN), `done_state`, `project`."""
        run = run or urllib_runner(tracker["url"], os.environ[tracker.get("token_env", "YOUTRACK_TOKEN")])
        return cls(run, done_state=tracker.get("done_state", "Done"), project=tracker.get("project"),
                   repository_field=tracker.get("repository_field"), scope=tracker.get("scope"), projects=tracker.get("projects"),
                   state_mirror=tracker.get("state_mirror"), state_mirror_field=tracker.get("state_mirror_field", "State"),
                   repository_scopes=tracker.get("repository_scopes"), **kw)

    # --- reads
    def _issue(self, n):
        return self.run("GET", f"/api/issues/{n}?fields={ISSUE_FIELDS}")

    @staticmethod
    def _field(raw, name):
        value = next((f.get("value") for f in raw.get("customFields") or [] if f.get("name") == name), None)
        return [value] if isinstance(value, dict) else value or []

    def _linked(self, raw):
        """(repository name, reason) of an issue from its project with `tracker.projects`: the name of the repository its
        project links to; the reason names the project when it is not linked or links no repository of the team."""
        project = (raw.get("project") or {}).get("shortName")
        if project not in self.projects:
            return None, f"project {project!r} is not linked to a repository: link it in `tracker.projects` ({', '.join(self.projects)})"
        full = str(self.projects[project])
        name = self.full_names.get(full.lower(), full.rsplit("/", 1)[-1])
        if self.repositories and name not in self.repositories:
            return None, f"project {project!r} links {full}, not one of {', '.join(self.repositories)}"
        return name, None

    def _repo(self, raw):
        """The repository name of an issue: its linked project's with `tracker.projects`, else the `repository_field`
        value; None without either."""
        if self.projects:
            return self._linked(raw)[0]
        return next((v.get("name") for v in self._field(raw, self.repository_field)), None) if self.repository_field else None

    def repository(self, n):
        """(repository name, reason) of ticket `n`: from its linked project with `tracker.projects`, else the repository
        field; the reason names the project or the field when it is unknown, empty or holds an unknown value."""
        if self.projects:
            return self._linked(self._issue(n))
        repo = self._repo(self._issue(n))
        if repo and (not self.repositories or repo in self.repositories):
            return repo, None
        return None, (f"field {self.repository_field!r} is empty: set it to one of {', '.join(self.repositories)}" if not repo else
                      f"field {self.repository_field!r} holds {repo!r}, not one of {', '.join(self.repositories)}")

    def ticket_repositories(self, n):
        """The repository names ticket `n` changes when `tracker.projects` links its project: its one repository; None
        without linked projects (the gate reads the ticket's `repositories:` line). An unlinked project raises
        ValueError, as an unknown repository on that line does."""
        if not self.projects:
            return None
        repo, reason = self.repository(n)
        if reason:
            raise ValueError(f"ticket {n}: {reason}")
        return [repo]

    def in_scope(self, n):
        """Whether ticket `n`'s scope field holds the scope's value (every ticket without a scope)."""
        if not self.scope:
            return True
        field = self.scope["field"]
        return matches(self.scope, fields={field: [v.get("name") for v in self._field(self._issue(n), field)]})

    def _scoped(self, query):
        """`query` narrowed to the scope: `(<query>) and <field>: {<value>}`."""
        return f"({query}) and {self.scope['field']}: {{{self.scope['value']}}}" if self.scope else query

    def _attrs(self, raw):
        repo = self._repo(raw)
        attrs = [next((s for s in self.repo_stages if t["name"] == f"{s}-{repo}"), t["name"]) for t in raw.get("tags") or []]
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

    def last_field_change(self, n, name):
        """(value, login) of the latest change of custom field `name` on ticket `n`, else (None, None)."""
        found = [a for a in self.run("GET", f"/api/issues/{n}/activities?categories=CustomFieldCategory&fields={ACTIVITY_FIELDS}&{TOP}")
                 if (a.get("field") or {}).get("name") == name]
        if not found:
            return None, None
        a = max(found, key=lambda x: x.get("timestamp") or 0)
        added = a.get("added") or []
        return (added[0].get("name") if added else None), (a.get("author") or {}).get("login")

    def field_value(self, n, name):
        """The value (name or login) of single-value custom field `name` of ticket `n`, else None."""
        return next((v.get("name") or v.get("login") for v in self._field(self._issue(n), name)), None)

    def comments(self, n):
        return [{"author": (login := (c.get("author") or {}).get("login", "")),
                 "author_is_bot": login in self.bots or login.endswith("[bot]"),
                 "author_is_factory": login in self.bots, "body": c.get("text") or "",
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
        last `gate_port.CLOSED_HOURS`."""
        if not attrs:
            return []
        attrs = [x for a in attrs for x in ([f"{a}-{r}" for r in self.repositories] if a in self.repo_stages else [a])]
        query = " or ".join(f"tag: {{{a}}}" for a in attrs)
        if not open_only:
            query = f"({query}) and (#Unresolved or resolved date: {{minus {CLOSED_HOURS}h}} .. Now)"
        else:
            query = f"({query}) and #Unresolved"
        found = self.run("GET", f"/api/issues?query={urllib.parse.quote(self._scoped(query))}&fields=idReadable&{TOP}")
        return sorted(i["idReadable"] for i in found)

    def can_answer(self, login):
        return login in self.writers

    # --- tag inventory (deploy)
    def list_tags(self):
        """{name: id} of the tags the token can see."""
        out, skip = {}, 0
        while True:  # a large instance has many more tags than one page (youtrack.jetbrains.com: ~14,000)
            page = self.run("GET", f"/api/tags?fields=id,name&{TOP}&$skip={skip}") or []
            out.update({t["name"]: t["id"] for t in page})
            if len(page) < 1000:
                return out
            skip += len(page)

    def used_outside_project(self, name):
        """Whether an issue outside the configured project carries the tag; True when no project is configured, as
        ownership cannot then be established. Tags are account-wide, so only a tag no other project uses is safe to delete."""
        if not self.project:
            return True
        query = urllib.parse.quote(f"tag: {{{name}}} and project: -{{{self.project}}}")
        return bool(self.run("GET", f"/api/issues?query={query}&fields=idReadable&$top=1"))

    def create_tag(self, name):
        self.run("POST", "/api/tags?fields=id,name", {"name": name})

    def delete_tag(self, name, owned_prefixes):
        """Delete a tag only inside the factory's namespaces; anything else is someone else's and never removed."""
        if not name.startswith(tuple(owned_prefixes)):
            raise ValueError(f"tag {name!r} is not factory-owned")
        self.run("DELETE", f"/api/tags/{self.list_tags()[name]}")

    # --- state field inventory (deploy)
    def _field_bundle(self, name):
        """The state bundle id and its values {value: {id, resolved}} of the project's field `name`; MissingField when absent."""
        q = urllib.parse.quote
        project = next((p for p in self.run("GET", f"/api/admin/projects?fields=id,shortName&query={q(self.project or '')}&{TOP}")
                        if p["shortName"] == self.project), None)
        if project is None:
            raise MissingField(f"project {self.project!r} does not exist in YouTrack")
        for c in self.run("GET", f"/api/admin/projects/{project['id']}/customFields?fields=field(name),bundle(id,values(id,name,isResolved))&{TOP}"):
            if c["field"]["name"] == name:
                bundle = c.get("bundle") or {}
                return bundle.get("id"), {v["name"]: {"id": v["id"], "resolved": v.get("isResolved")} for v in bundle.get("values") or []}
        raise MissingField(f"field {name!r} does not exist in project {self.project!r}: create it in YouTrack, deploy never does")

    def list_field_values(self, name):
        """{value: {id, resolved}} of the values the field `name` offers; `resolved` is None for a bundle without the flag."""
        return self._field_bundle(name)[1]

    def create_field_value(self, name, value, resolved=None):
        bundle = self._field_bundle(name)[0]
        self.run("POST", f"/api/admin/customFieldSettings/bundles/state/{bundle}/values?fields=id,name",
                 {"name": value, **({} if resolved is None else {"isResolved": resolved})})

    def update_field_value(self, name, value, resolved):
        bundle, have = self._field_bundle(name)
        self.run("POST", f"/api/admin/customFieldSettings/bundles/state/{bundle}/values/{have[value]['id']}?fields=id,name",
                 {"isResolved": resolved})

    def delete_field_value(self, name, value, owned_prefixes):
        """Delete a value only inside the factory's namespaces; anything else is someone else's and never removed."""
        if not value.startswith(tuple(owned_prefixes)):
            raise ValueError(f"field value {value!r} is not factory-owned")
        bundle, have = self._field_bundle(name)
        self.run("DELETE", f"/api/admin/customFieldSettings/bundles/state/{bundle}/values/{have[value]['id']}")

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

    def mirror_state(self, n, stage):
        """The mirrored State of `stage` (`state_mirror`), written only when the field holds another value; nothing
        when stages are the state field itself or the stage is not mapped."""
        value = self.state_mirror.get(stage)
        return bool(value) and not self.state_field and self.set_field(n, self.state_mirror_field, value)

    def set_stage(self, n, stage, have):
        """Stage `stage` replaces the stage attrs `have`: tags, or the one value of the state field."""
        if self.state_field:
            return self.set_field(n, self.state_field, stage)
        if self.repo_stages:
            repo, reason = self.repository(n)
            if reason and stage in self.repo_stages:
                raise MissingRepository(reason)
            tag = lambda x: f"{x}-{repo}" if repo and x in self.repo_stages else x
            return self.edit_attrs(n, [tag(stage)], [tag(x) for x in have if tag(x) != tag(stage)])
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

    def scope_of(self, repo):
        """The scope a ticket for repository `repo` (a name or owner/repo) gets: that repository's
        (`repository_scopes`), else this store's own."""
        want = str(repo or "").lower()
        return next((s for full, s in self.repository_scopes.items()
                     if want and want in (full.lower(), full.lower().rsplit("/", 1)[-1])), self.scope)

    def _create(self, title, body, tags=(), repo=None, scope=None):
        issue = self.run("POST", "/api/issues?fields=idReadable", {
            "summary": title, "description": body, **({"project": {"shortName": self.project}} if self.project else {})})
        self.edit_attrs(issue["idReadable"], tags)
        scope = scope or self.scope
        if scope:  # a ticket the factory creates stays in its environment, or goes to its repository's
            self.set_field(issue["idReadable"], scope["field"], scope["value"], scope.get("type", "SingleEnumIssueCustomField"))
        if repo:
            self.set_field(issue["idReadable"], self.repository_field, repo)
        return issue["idReadable"]

    def create_children(self, n, children, ready):
        """Per child: reuse the subtask of that title, then add each link that is missing (a retry converges). The
        child depends on the children of the repositories it is `after` (`gate_port.child_links`) and blocks the
        parent; its repository field is its `repo`; `ready` is set last, once the links exist."""
        subs = {c["summary"]: c["idReadable"] for c in self.run(
            "GET", f"/api/issues?query={urllib.parse.quote(f'subtask of: {n}')}&fields=idReadable,summary&{TOP}")}
        blockers, wrote, made, links = set(self.open_blockers_all(n)), False, {}, child_links(children)
        for k in child_order(links):
            c = children[k]
            c = c if isinstance(c, dict) else {"title": str(c), "body": ""}
            repo = c.get("repo") if self.repository_field else None
            child = subs.get(c["title"])
            scope = self.scope_of(c.get("repo")) if c.get("repo") else None
            if child is None:
                child = self._create(c["title"], f"{c.get('body', '')}\n\n{CHILD_MARK.format(n=n)}",
                                     **({"repo": repo} if repo else {}), **({"scope": scope} if scope else {}))
                self._command(child, f"subtask of {n}")
                wrote = True
            elif repo:
                wrote = self.set_field(child, self.repository_field, repo) or wrote
            made[k] = child
            have = self.open_blockers_all(child)
            for j in links[k]:
                if j in made and made[j] not in have:
                    self._command(child, f"depends on {made[j]}")
                    wrote = True
            if child not in blockers:
                self._command(n, f"depends on {child}")
                wrote = True
            wrote = self.set_stage(child, ready, []) or wrote
        return wrote

    def open_blockers_all(self, n):
        """Ids of every ticket `n` depends on, resolved or not."""
        return [i["idReadable"] for i in self._links(n, "Depend", "INWARD")]

    def park(self, n, check, triage):
        """Open or reuse the one `Main is red in Air: <check>` ticket, then block ticket `n` by it."""
        title = f"Main is red in Air: {check}"
        found = self.run("GET", f"/api/issues?query={urllib.parse.quote(self._scoped(f'#Unresolved summary: {{{title}}}'))}"
                                f"&fields=idReadable,summary&{TOP}")
        red = next((i["idReadable"] for i in found if i["summary"] == title), None)
        if red is None:
            red = self._create(title, effects.tagged(
                f"The required check `{check}` fails identically on unchanged `main` in Air "
                f"(found while working on {n}). Fix main; the tickets blocked by this one restart "
                "when it closes."), [triage])
        if red in self.open_blockers_all(n):
            return False
        self._command(n, f"depends on {red}")
        return True

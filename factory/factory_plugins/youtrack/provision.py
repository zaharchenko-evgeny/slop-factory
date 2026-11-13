"""What `factory deploy` sets up in YouTrack (ADR-0026 `provision`): the tags and the state-field values the
definition's state and command stores name. Deploy creates missing tags and values, and removes only factory-owned
ones no longer required (a tag still used by another project's issues stays); it never creates a custom field.

Hooks: `deploy.plan` adds the plan keys (`tags`, `fields`, their owned prefixes, `tag_tracker`), `deploy.repo_tags`
names the per-repository stage tags a trigger listens to, `deploy.trigger_filters` limits a repository's copy to the
YouTrack project linked to it (#981), `deploy.errors` checks `tracker.projects`, and `deploy.tracker` returns the
`Provisioner` that the Air deploy (`factory_plugins.air.deploy.deploy`) drives between its label steps.
"""
import config as config_mod
import definition
import deploy as core
import scopes
from factory_plugins.youtrack.store import MissingField, YouTrackTicketStore, palette_id, repo_stage_tags


def youtrack(config):
    return (config.get("tracker") or {}).get("adapter") == "youtrack"


def tracker_block(config):
    """The YouTrack tracker block the definition keeps tickets in: the top-level one, else the first `ticket_places`
    entry on YouTrack; None when no ticket lives in YouTrack."""
    if youtrack(config):
        return config["tracker"]
    place = next((p for p in config.get("ticket_places") or [] if scopes.adapter(scopes.merged(config, p.get("tracker") or {})) == "youtrack"), None)
    return scopes.merged(config, place["tracker"]) if place else None


def identity(block):
    """Hook `tracker.identity`: (adapter, url, project) of a YouTrack block; None for another adapter."""
    if scopes.adapter(block) == "youtrack":
        return "youtrack", str(block.get("url") or "").rstrip("/"), block.get("project")


def owns_id(block, ticket):
    """Hook `tracker.owns_id`: whether `ticket` is `<project>-<n>` of a YouTrack block; None for another adapter."""
    if scopes.adapter(block) == "youtrack":
        return isinstance(ticket, str) and (not block.get("project") or ticket.startswith(f"{block['project']}-"))


def block_errors(block):
    """Hook `tracker.block_errors`: the errors of an environment's merged YouTrack block; None for another adapter."""
    if scopes.adapter(block) == "youtrack":
        return [] if block.get("url") and block.get("project") else \
            ["youtrack needs `url` and `project` (here or in the top-level tracker)"]


def tracker_errors(descriptor, t, where):
    """Hook `config.tracker_errors`: a youtrack tracker names its url and project and needs `vcs.repository` (alias
    `code_host.repository`) for the pull requests; None for another adapter."""
    if t.get("adapter") == "youtrack":
        need = [f"{where}.tracker.{k}: required with adapter youtrack" for k in ("url", "project") if k not in t]
        if not config_mod.vcs(descriptor).get("repository"):
            need.append(f"{where}.code_host.repository: required with tracker.adapter youtrack")
        return need


def scope_filter(trig, block):
    """Hook `deploy.scope_filter`: a YouTrack event trigger also matches the scope field's value."""
    scope = block.get("scope") or {}
    if trig.get("source") == "youtrack" and scope.get("field"):
        return {**trig, "filters": {**trig.get("filters", {}), scope["field"]: scope["value"]}}
    return trig


def repo_tags(config: dict) -> list[str]:
    """Hook `deploy.repo_tags`: the stage tags stored per repository (`<tag>-<repo>`)."""
    return repo_stage_tags(config)


def linked_project(config: dict, repo: dict | None) -> str | None:
    """The YouTrack project `tracker.projects` links to repository `repo` (repository names compare case-insensitively,
    project short names are case-sensitive), else None."""
    projects = (config.get("tracker") or {}).get("projects") or {}
    want = definition.full_name(repo["url"]).lower() if repo else None
    return next((p for p, r in projects.items() if str(r).lower() == want), None)


def trigger_filters(config: dict, scope: dict | None) -> dict:
    """Hook `deploy.trigger_filters`: {source: filters} that repository `scope`'s copy adds to its ticket triggers: Air's
    project selector `projectShortName` of the project linked to it (the value is case-sensitive, AIRA-790)."""
    project = linked_project(config, scope)
    return {"youtrack": {"projectShortName": project}} if project else {}


def errors(nodes: dict, config: dict) -> list[str]:
    """Hook `deploy.errors`: per `tracker.projects` entry, `unknown-repository` when it names no repository,
    `duplicate-repository` when another project already links its repository (Air ANDs a trigger's filters, so one
    trigger cannot select two projects), and `unlinked-project` when no planned copy listens to it: the projects the
    YouTrack triggers of a node's copies are limited to (ADR-0024: one copy per listened repository, `trigger_filters`)."""
    projects = (config.get("tracker") or {}).get("projects") or {}
    repos = {definition.full_name(r["url"]).lower() for r in config.get("repositories") or []}
    errs, linked = [], {}
    for project, repo in projects.items():
        if str(repo).lower() not in repos:
            errs.append(f"unknown-repository: tracker.projects.{project}: {repo} is not a repository")
        elif str(repo).lower() in linked:
            errs.append(f"duplicate-repository: tracker.projects.{project}: {repo} is already linked to {linked[str(repo).lower()]}")
        else:
            linked[str(repo).lower()] = project
    listened, defn = set(), core.team_definition() if linked else None
    trackers = scopes.project_envs(config)  # #1069: a default-environment node runs once per project, not per repository
    default_env = definition.default_environment(defn) if trackers and defn else None
    for name, n in (nodes["nodes"] if defn else {}).items():
        if n.get("enabled") is False or n["role"] == "human" or trackers and n["env"] == default_env:
            continue
        sources = {core.trigger(t, None).get("source") for t in n.get("triggers") or []
                   if not str(t).startswith(("schedule ", "webhook ")) and str(t) != "manual"}
        copies = definition.node_listens(defn, name)
        listened |= {f["projectShortName"] for repo in (copies if len(copies) > 1 else [])
                     for source, f in trigger_filters(config, {"url": repo}).items() if source in sources}
    return errs + [f"unlinked-project: tracker.projects.{p}: no node listens to project {p} ({projects[p]})"
                   for p in linked.values() if p not in listened]


def _store_kinds(config):
    return {k for x in [*(config.get("states") or {}).values(), *(config.get("commands") or {}).values()] for k in x["store"]}


def youtrack_tags(nodes: dict, config: dict) -> list[str]:
    """The tags a YouTrack definition needs: those of its stores and, when it stores in tags, the ticket attributes."""
    stored = store_tags(nodes, config)
    return sorted({*stored, *(core.attribute_labels(config) if stored and "tag" in _store_kinds(config) else ())})


def store_tag_sources(nodes: dict, config: dict) -> dict:
    """{tag: label it comes from} for the tags a YouTrack definition stores its stages and commands in, `<tier>` and
    `<focus>` expanded: a repo-suffixed tag comes from `<tag>`, an expanded one from its template. None for other adapters."""
    if not tracker_block(config):
        return {}
    tiers = sorted({x for n in nodes["nodes"].values() for x in n.get("tiers") or []}) or ["cheap", "mid", "strong"]
    focuses = sorted({x for n in nodes["nodes"].values() for x in n.get("focuses") or []})
    per_repo, out = repo_stage_tags(config), {}
    for x in [*(config.get("states") or {}).values(), *(config.get("commands") or {}).values()]:
        tag = x["store"].get("tag")
        if tag in per_repo:  # one tag per repository
            out.update({f"{tag}-{r['name']}": tag for r in config["repositories"]})
        elif isinstance(tag, str):
            names = [tag.replace("<tier>", t) for t in tiers] if "<tier>" in tag else \
                [tag.replace("<focus>", f) for f in focuses] if "<focus>" in tag else [tag]
            out.update({n: tag for n in names})
    return out


def store_tags(nodes: dict, config: dict) -> list[str]:
    """The tags a YouTrack definition stores its stages and commands in, `<tier>` and `<focus>` expanded; none for other adapters."""
    return sorted(store_tag_sources(nodes, config))


def tag_specs(nodes: dict, config: dict, tags: list[str]) -> dict:
    """{tag: hex color} of the planned tags that have one, from `deploy.label_specs` (so `labels.overrides` applies): the
    tag's own label first, then the label it comes from; a `<focus>` command template takes the command group color."""
    specs, sources = core.label_specs(nodes, config), store_tag_sources(nodes, config)
    command = ((config.get("labels") or {}).get("colors") or {}).get("groups", {}).get("command")
    commands = {x["store"].get("tag") for x in (config.get("commands") or {}).values()}
    out = {}
    for tag in tags:
        source = sources.get(tag)
        color = (specs.get(tag) or specs.get(source) or {}).get("color") or (command if source in commands else None)
        if color:
            out[tag] = str(color).lower()
    return out


def youtrack_fields(config: dict) -> dict:
    """{field: {value: resolved}} the state and command stores of a YouTrack definition keep in custom fields;
    a `final` state is resolved. Empty for other adapters."""
    if not tracker_block(config):
        return {}
    out = {}
    for x in [*(config.get("states") or {}).values(), *(config.get("commands") or {}).values()]:
        f = x["store"].get("field")
        if isinstance(f, dict):
            out.setdefault(f["name"], {})[f["value"]] = x.get("kind") == "final"
    return out


def plan(nodes: dict, config: dict) -> dict:
    """Hook `deploy.plan`: the tag and field plan; empty (the plan unchanged) without tags or fields."""
    tags, fields = youtrack_tags(nodes, config), youtrack_fields(config)
    owned = {t.partition(":")[0] + ":" for t in store_tags(nodes, config) if ":" in t}  # not the attribute tags
    tag_plan = {"tags": tags, "owned_tag_prefixes": sorted(owned), "tag_specs": tag_specs(nodes, config, tags)} if tags else {}
    field_plan = {"fields": fields, "owned_field_prefixes": sorted(
        {v.partition(":")[0] + ":" for f in fields.values() for v in f if ":" in v})} if fields else {}
    return {**tag_plan, **field_plan, "tag_tracker": tracker_block(config)} if tags or fields else {}


def tag_store(plan: dict, run=None):
    return YouTrackTicketStore.from_config(plan["tag_tracker"], run)


def reconcile_fields(plan: dict, run=None) -> list[tuple]:
    """(action, field, value, resolved) per required field value to create, per one whose resolved flag differs
    (`update`), and per factory-owned value no longer required (`remove`). Values outside the factory's namespaces are
    never listed for removal. A missing field raises `store.MissingField`."""
    out, owned = [], tuple(plan.get("owned_field_prefixes") or [])
    for name, want in (plan.get("fields") or {}).items():
        have = tag_store(plan, run).list_field_values(name)
        for value, resolved in want.items():
            if value not in have:
                out.append(("create", name, value, resolved))
            elif have[value]["resolved"] is not None and have[value]["resolved"] != resolved:
                out.append(("update", name, value, resolved))
        out += [("remove", name, v, None) for v in sorted(have) if owned and v.startswith(owned) and v not in want]
    return out


def reconcile_tags(plan: dict, run=None) -> list[tuple]:
    """(action, tag[, color id]) per required YouTrack tag to create, per factory-owned tag whose color differs from the
    planned one (`recolor`), and per factory-owned tag no longer required to remove; tags outside the factory's
    namespaces, or still used by another project's issues, are never listed for removal. A tag with no planned color
    is created without one and never recolored."""
    if not plan.get("tags"):
        return []
    store = tag_store(plan, run)
    have = store.list_tags(colors=True)
    owned = tuple(plan.get("owned_tag_prefixes") or [])
    want = {t: palette_id(c) for t, c in (plan.get("tag_specs") or {}).items()}
    return [("create", t, want.get(t)) for t in plan["tags"] if t not in have] + \
        [("recolor", t, want[t]) for t in plan["tags"] if t in have and owned and t.startswith(owned) and want.get(t)
         and have[t][1] != want[t]] + \
        [("remove", t, None) for t in sorted(have) if owned and t.startswith(owned) and t not in plan["tags"]
         and not store.used_outside_project(t)]


class Provisioner:
    """The YouTrack changes of one deploy: read at construction (`error` names a missing field, and nothing is
    written then); `dry_run`, `write` (creates, updates), `refusals` (what a removal needs `--allow-delete` for) and
    `remove` return the lines to print, in the deploy's order."""

    def __init__(self, plan, run=None):
        self.plan, self.run, self.error = plan, run, None
        self.tags, self.fields = reconcile_tags(plan, run), []
        try:
            self.fields = reconcile_fields(plan, run)
        except MissingField as e:
            self.error = str(e)

    def dry_run(self):
        return [f"{action} tag {tag} in YouTrack" for action, tag, _ in self.tags] + \
            [f"{action} {name} value {value} in YouTrack" for action, name, value, _ in self.fields]

    def write(self):
        lines = []
        for action, tag, color in self.tags:  # a missing tag fails every handoff that adds it
            if action == "create":
                tag_store(self.plan, self.run).create_tag(tag, color)
                lines.append(f"created tag {tag} in YouTrack")
            elif action == "recolor":
                tag_store(self.plan, self.run).recolor_tag(tag, color, self.plan["owned_tag_prefixes"])
                lines.append(f"recolored tag {tag} in YouTrack")
        for action, name, value, resolved in self.fields:
            if action == "create":
                tag_store(self.plan, self.run).create_field_value(name, value, resolved)
            elif action == "update":
                tag_store(self.plan, self.run).update_field_value(name, value, resolved)
            else:
                continue
            lines.append(f"{action}d {name} value {value} in YouTrack")
        return lines

    def refusals(self):
        return [f"refused: would remove tag {t} in YouTrack" for a, t, _ in self.tags if a == "remove"] + \
            [f"refused: would remove {name} value {value} in YouTrack" for a, name, value, _ in self.fields if a == "remove"]

    def remove(self):
        lines = []
        for tag in [t for a, t, _ in self.tags if a == "remove"]:
            tag_store(self.plan, self.run).delete_tag(tag, self.plan["owned_tag_prefixes"])
            lines.append(f"removed tag {tag} in YouTrack")
        for _, name, value, _ in [c for c in self.fields if c[0] == "remove"]:
            tag_store(self.plan, self.run).delete_field_value(name, value, self.plan["owned_field_prefixes"])
            lines.append(f"removed {name} value {value} in YouTrack")
        return lines


def provisioner(plan: dict, run=None):
    """Hook `deploy.tracker`: the Provisioner of a plan with YouTrack tags or fields, else None."""
    return Provisioner(plan, run) if plan.get("tag_tracker") else None

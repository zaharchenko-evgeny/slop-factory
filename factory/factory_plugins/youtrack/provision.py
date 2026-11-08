"""What `factory deploy` sets up in YouTrack (ADR-0026 `provision`): the tags and the state-field values the
definition's state and command stores name. Deploy creates missing tags and values, and removes only factory-owned
ones no longer required (a tag still used by another project's issues stays); it never creates a custom field.

Hooks: `deploy.plan` adds the plan keys (`tags`, `fields`, their owned prefixes, `tag_tracker`), `deploy.repo_tags`
names the per-repository stage tags a trigger listens to, and `deploy.tracker` returns the `Provisioner` that
`deploy.deploy` drives between its label steps.
"""
from factory_plugins.youtrack.store import MissingField, YouTrackTicketStore, repo_stage_tags


def youtrack(config):
    return (config.get("tracker") or {}).get("adapter") == "youtrack"


def repo_tags(config: dict) -> list[str]:
    """Hook `deploy.repo_tags`: the stage tags stored per repository (`<tag>-<repo>`)."""
    return repo_stage_tags(config)


def youtrack_tags(nodes: dict, config: dict) -> list[str]:
    """The tags a YouTrack definition stores its stages and commands in, `<tier>` and `<focus>` expanded; none for other adapters."""
    if not youtrack(config):
        return []
    tiers = sorted({x for n in nodes["nodes"].values() for x in n.get("tiers") or []}) or ["cheap", "mid", "strong"]
    focuses = sorted({x for n in nodes["nodes"].values() for x in n.get("focuses") or []})
    per_repo, out = repo_stage_tags(config), set()
    for x in [*(config.get("states") or {}).values(), *(config.get("commands") or {}).values()]:
        tag = x["store"].get("tag")
        if tag in per_repo:  # one tag per repository
            out |= {f"{tag}-{r['name']}" for r in config["repositories"]}
        elif isinstance(tag, str):
            out |= {tag.replace("<tier>", t) for t in tiers} if "<tier>" in tag else \
                {tag.replace("<focus>", f) for f in focuses} if "<focus>" in tag else {tag}
    return sorted(out)


def youtrack_fields(config: dict) -> dict:
    """{field: {value: resolved}} the state and command stores of a YouTrack definition keep in custom fields;
    a `final` state is resolved. Empty for other adapters."""
    if not youtrack(config):
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
    tag_plan = {"tags": tags, "owned_tag_prefixes": sorted({t.partition(":")[0] + ":" for t in tags if ":" in t})} if tags else {}
    field_plan = {"fields": fields, "owned_field_prefixes": sorted(
        {v.partition(":")[0] + ":" for f in fields.values() for v in f if ":" in v})} if fields else {}
    return {**tag_plan, **field_plan, "tag_tracker": config["tracker"]} if tags or fields else {}


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
    """(action, tag) per required YouTrack tag to create and per factory-owned tag no longer required to remove;
    tags outside the factory's namespaces, or still used by another project's issues, are never listed for removal."""
    if not plan.get("tags"):
        return []
    store = tag_store(plan, run)
    have = store.list_tags()
    owned = tuple(plan.get("owned_tag_prefixes") or [])
    return [("create", t) for t in plan["tags"] if t not in have] + \
        [("remove", t) for t in sorted(have) if owned and t.startswith(owned) and t not in plan["tags"]
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
        return [f"{action} tag {tag} in YouTrack" for action, tag in self.tags] + \
            [f"{action} {name} value {value} in YouTrack" for action, name, value, _ in self.fields]

    def write(self):
        lines = []
        for action, tag in self.tags:  # a missing tag fails every handoff that adds it
            if action == "create":
                tag_store(self.plan, self.run).create_tag(tag)
                lines.append(f"created tag {tag} in YouTrack")
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
        return [f"refused: would remove tag {t} in YouTrack" for a, t in self.tags if a == "remove"] + \
            [f"refused: would remove {name} value {value} in YouTrack" for a, name, value, _ in self.fields if a == "remove"]

    def remove(self):
        lines = []
        for tag in [t for a, t in self.tags if a == "remove"]:
            tag_store(self.plan, self.run).delete_tag(tag, self.plan["owned_tag_prefixes"])
            lines.append(f"removed tag {tag} in YouTrack")
        for _, name, value, _ in [c for c in self.fields if c[0] == "remove"]:
            tag_store(self.plan, self.run).delete_field_value(name, value, self.plan["owned_field_prefixes"])
            lines.append(f"removed {name} value {value} in YouTrack")
        return lines


def provisioner(plan: dict, run=None):
    """Hook `deploy.tracker`: the Provisioner of a plan with YouTrack tags or fields, else None."""
    return Provisioner(plan, run) if plan.get("tag_tracker") else None

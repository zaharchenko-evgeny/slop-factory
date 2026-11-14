"""Project descriptor loading and validation (a compiled team definition, ADR-0021, or a `factory.project.yaml`)."""
import json
import re
from pathlib import Path

import yaml

import config

STORE_KINDS = ("label", "pr_label", "field", "tag", "pr")
PERSONA = re.compile(r"[a-z][a-z0-9-]*")  # the handle in `@factory:<persona>`
NO_AGENT_ROLES = ("script", "human")


def _read(team_root):
    import definition
    if definition.present(team_root):  # ADR-0021: the definition compiled into this shape
        return definition.to_project(definition.load(team_root))[0]
    return yaml.safe_load((Path(team_root) / config.PROJECT_DESCRIPTOR).read_text()) or {}


def load(team_root):
    """Return the validated descriptor merged over engine/defaults.yaml; errors name the path and key."""
    errors = validate(team_root)
    if errors:
        raise ValueError("; ".join(errors))
    defaults = yaml.safe_load((config.engine_root() / "engine/defaults.yaml").read_text()) or {}
    return config.merge(defaults, _read(team_root))


def store_kind(store):
    """Return the sole declared store kind or reject an ambiguous store."""
    if not isinstance(store, dict) or len(store) != 1 or next(iter(store), None) not in STORE_KINDS:
        raise ValueError(f"store must contain exactly one of {', '.join(STORE_KINDS)}")
    return next(iter(store))


def _store_value(store):
    kind = store_kind(store)
    if kind == "field":
        return store[kind]["value"]
    return store[kind]


def _repo_key(url):
    return url.rstrip("/").removesuffix(".git") if isinstance(url, str) else None


def validate(team_root):
    """Return errors for a project's factory.project.yaml descriptor."""
    root = Path(team_root)
    try:
        descriptor = _read(root)
    except (OSError, yaml.YAMLError) as exc:
        return [f"{config.PROJECT_DESCRIPTOR}: {exc}"]
    schema = json.loads((config.engine_root() / "engine/project.schema.json").read_text())
    errors = []
    config._check(schema, descriptor, config.PROJECT_DESCRIPTOR, errors)
    if not isinstance(descriptor, dict):
        return errors
    errors += config.tracker_errors(descriptor, config.PROJECT_DESCRIPTOR)
    import scopes
    errors += scopes.errors(descriptor, config.PROJECT_DESCRIPTOR)  # environments with their own trackers (#1069)
    errors += scopes.vcs_errors(descriptor, config.PROJECT_DESCRIPTOR)  # every vcs named is provided (#1070)
    repositories = descriptor.get("repositories")
    if isinstance(repositories, list):
        primaries = [r.get("name") for r in repositories if isinstance(r, dict) and r.get("role") == "primary"]
        if len(primaries) != 1:
            errors.append(f"{config.PROJECT_DESCRIPTOR}.repositories: exactly one repository needs role primary, "
                          f"found {len(primaries)}")
        team = _repo_key(descriptor.get("team_repository"))
        for i, r in enumerate(repositories):
            if team and isinstance(r, dict) and _repo_key(r.get("url")) == team and r.get("role") != "project":
                errors.append(f"{config.PROJECT_DESCRIPTOR}.repositories[{i}].role: the team repository "
                              f"{r.get('name')} must have role project, found {r.get('role')}")
    environments = descriptor.get("environments")
    for env, entry in (environments.items() if isinstance(environments, dict) else ()):
        variables = entry.get("variables") if isinstance(entry, dict) else None
        for name in (variables if isinstance(variables, dict) else ()):
            if str(name).startswith("FACTORY_"):
                errors.append(f"{config.PROJECT_DESCRIPTOR}.environments.{env}.variables.{name}: "
                              "names starting with FACTORY_ are reserved for the factory")
    targets = descriptor.get("targets")
    local = targets.get("local") if isinstance(targets, dict) else None
    poll = local.get("poll_seconds") if isinstance(local, dict) else None
    if isinstance(poll, int) and poll < 10:
        errors.append(f"{config.PROJECT_DESCRIPTOR}.targets.local.poll_seconds: must be at least 10")
    for section in ("states", "commands"):
        entries = descriptor.get(section)
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            if not isinstance(entry, dict) or "store" not in entry:
                continue
            try:
                store_kind(entry["store"])
            except ValueError as exc:
                errors.append(f"{config.PROJECT_DESCRIPTOR}.{section}.{name}.store: {exc}")
    return errors


def node_agents(descriptor, nodes):
    """({node: agent}, errors) for every node that runs an agent (ADR-0020). An agent is its `agents:` entry with
    its `id`; a node without `agent:` gets its role's implicit agent: the `roles:` entry, its id and persona the role."""
    agents = descriptor.get("agents") or {}
    roles = descriptor.get("roles") if isinstance(descriptor.get("roles"), dict) else {}
    out, errors = {}, []
    for aid, agent in agents.items():
        if not PERSONA.fullmatch(str(agent.get("persona"))):
            errors.append(f"bad-persona: agents.{aid}: {agent.get('persona')!r} is not a lowercase slug")
    for name, n in ((k, v) for k, v in nodes.items() if isinstance(v, dict)):
        aid, role = n.get("agent"), n.get("role")
        if role in NO_AGENT_ROLES:
            if aid is not None:
                errors.append(f"agent-on-{role}: {name}: a {role} node runs no agent")
        elif aid is None:
            out[name] = {**(roles.get(role) or {}), "id": role, "persona": role, "role": role}
        elif aid not in agents:
            errors.append(f"unknown-agent: {name}: {aid}")
        elif agents[aid].get("role") != role:
            errors.append(f"agent-role: {name}: agent {aid} is a {agents[aid].get('role')}, the node a {role}")
        else:
            out[name] = {**agents[aid], "id": aid}
            errors += [f"unknown-mode: {name}: agent {aid} has no mode {f}" for f in n.get("focuses") or []
                       if f not in (agents[aid].get("modes") or {})]
    owners = {}
    for agent in [*({**a, "id": aid} for aid, a in agents.items()), *out.values()]:
        owners.setdefault(agent.get("persona"), set()).add(agent["id"])
    errors += [f"duplicate-persona: {p}: agents {', '.join(sorted(ids))}" for p, ids in owners.items() if len(ids) > 1]
    return out, errors

"""Project descriptor loading and compatibility with the engine's legacy config shape."""
import json
from pathlib import Path

import yaml

import config

STORE_KINDS = ("label", "pr_label", "field", "tag", "pr")


def load(team_root):
    """Load a project's descriptor without applying engine defaults."""
    return yaml.safe_load((Path(team_root) / config.PROJECT_DESCRIPTOR).read_text()) or {}


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


def validate(team_root):
    """Return errors for a project's factory.project.yaml descriptor."""
    root = Path(team_root)
    try:
        descriptor = load(root)
    except (OSError, yaml.YAMLError) as exc:
        return [f"{config.PROJECT_DESCRIPTOR}: {exc}"]
    schema = json.loads((config.engine_root() / "engine/project.schema.json").read_text())
    errors = []
    config._check(schema, descriptor, config.PROJECT_DESCRIPTOR, errors)
    if not isinstance(descriptor, dict):
        return errors
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


def legacy_config(descriptor=None):
    """Render project states and commands into the config shape existing engine code reads."""
    descriptor = load(config.team_root()) if descriptor is None else descriptor
    defaults = yaml.safe_load((config.engine_root() / "engine/defaults.yaml").read_text()) or {}
    result = config.merge(defaults, descriptor)
    tracker = dict(result.get("tracker") or {})
    stages, kinds = {}, {}
    for name, state in (descriptor.get("states") or {}).items():
        if not isinstance(state, dict) or "store" not in state:
            continue
        try:
            kind = store_kind(state["store"])
        except ValueError:
            continue
        if kind in ("label", "field", "tag"):
            stages[name] = _store_value(state["store"])
            if state.get("kind") is not None:
                kinds[name] = state["kind"]
    tracker["stages"] = stages
    tracker["stage_kinds"] = kinds
    tracker["commands"] = {
        name: command["store"] for name, command in (descriptor.get("commands") or {}).items()
        if isinstance(command, dict) and isinstance(command.get("store"), dict)
    }
    result["tracker"] = tracker
    return result
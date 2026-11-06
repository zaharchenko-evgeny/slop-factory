"""The one config loader (ADR-0015): engine defaults overridden by the team descriptor.

ENGINE_ROOT holds the engine and its template; TEAM_ROOT holds all runtime roles, skills, and project configuration.
"""
import json
import os
import sys
from pathlib import Path

import yaml

DESCRIPTOR = "factory.descriptor.yaml"
PROJECT_DESCRIPTOR = "factory.project.yaml"
NODES = "engine/nodes.yaml"
_HERE = Path(__file__).resolve().parents[1]


def _find_engine():
    """FACTORY_ENGINE, else this checkout, else the assets an installed tool ships under its prefix."""
    if os.environ.get("FACTORY_ENGINE"):
        return Path(os.environ["FACTORY_ENGINE"])
    installed = Path(sys.prefix) / "share/factory-engine"
    return _HERE if (_HERE / NODES).exists() or not installed.exists() else installed


ENGINE_ROOT = _find_engine()
_team = None


def set_team(path):
    global _team
    _team = Path(path).resolve() if path else None


def team_root():
    if _team:
        return _team
    if os.environ.get("FACTORY_TEAM"):
        return Path(os.environ["FACTORY_TEAM"]).resolve()
    home = Path.home() / "factory-team"  # ADR-0019: never a project checkout, which may be the team repository
    if home.is_dir():
        return home.resolve()
    cwd = Path.cwd()
    if any((d / f).exists() for d in (cwd, ENGINE_ROOT) for f in (PROJECT_DESCRIPTOR, DESCRIPTOR)) \
            or all((cwd / f).is_file() for f in ("factory.yaml", "workflow.yaml")):  # ADR-0021
        return cwd.resolve()
    raise SystemExit("factory: no team repository (set FACTORY_TEAM or run the installed factory)")


def engine_root():
    return ENGINE_ROOT


def resolve(rel):
    """Resolve runtime files only from the team repository."""
    return team_root() / rel


def shown(rel):
    """The resolved path as given to agents: relative to the working directory when inside it."""
    p = resolve(rel)
    try:
        return str(p.relative_to(Path.cwd()))
    except ValueError:
        return str(p)


def pop_team(argv):
    """Apply and strip a `--team DIR` option; returns the remaining arguments."""
    argv = list(argv)
    if "--team" in argv:
        i = argv.index("--team")
        set_team(argv[i + 1] if i + 1 < len(argv) else None)
        del argv[i:i + 2]
    return argv


def merge(defaults, descriptor):
    """The descriptor overrides the defaults key by key, one level deep."""
    out = dict(defaults)
    for k, v in descriptor.items():
        out[k] = {**out[k], **v} if isinstance(out.get(k), dict) and isinstance(v, dict) else v
    return out


def descriptor():
    return yaml.safe_load((team_root() / DESCRIPTOR).read_text()) or {}


def load():
    root = team_root()
    if (root / PROJECT_DESCRIPTOR).is_file():
        import project
        return project.legacy_config(project.load(root))
    defaults = yaml.safe_load((engine_root() / "engine/defaults.yaml").read_text()) or {}
    return merge(defaults, descriptor())


def _check(schema, value, where, errs, root_schema=None):
    root_schema = schema if root_schema is None else root_schema
    if "$ref" in schema:
        target = root_schema
        for key in schema["$ref"].removeprefix("#/").split("/"):
            target = target[key]
        _check(target, value, where, errs, root_schema)
        return
    t = schema.get("type")
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int, "number": (int, float)}
    if t:
        types = t if isinstance(t, list) else [t]
        valid = any(kind in kinds and isinstance(value, kinds[kind])
                    and (kind == "boolean" or not isinstance(value, bool)) for kind in types)
        if not valid:
            errs.append(f"{where}: must be {' or '.join(types)}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{where}: {value!r} is not one of {', '.join(schema['enum'])}")
    if "oneOf" in schema:
        matches = 0
        for option in schema["oneOf"]:
            option_errors = []
            _check(option, value, where, option_errors, root_schema)
            matches += not option_errors
        if matches != 1:
            errs.append(f"{where}: must match exactly one schema")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errs.append(f"{where}.{key}: required")
        if "properties" in schema or "additionalProperties" in schema:
            for k, v in value.items():
                if k in schema.get("properties", {}):
                    _check(schema["properties"][k], v, f"{where}.{k}", errs, root_schema)
                else:
                    extra = schema.get("additionalProperties")
                    if extra is False:
                        errs.append(f"unknown-key: {where}.{k}")
                    elif isinstance(extra, dict):
                        _check(extra, v, f"{where}.{k}", errs, root_schema)
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            _check(schema["items"], v, f"{where}[{i}]", errs, root_schema)


def tracker_errors(descriptor, where):
    """Cross-field errors of `tracker` the schema cannot say: a youtrack tracker names its url and project and needs
    `code_host.repository` for the pull requests; a github-issues tracker needs `repository`."""
    t = descriptor.get("tracker") if isinstance(descriptor, dict) else None
    if not isinstance(t, dict):
        return []
    if t.get("adapter") == "youtrack":
        need = [f"{where}.tracker.{k}: required with adapter youtrack" for k in ("url", "project") if k not in t]
        if not ((descriptor.get("code_host") or {}).get("repository")):
            need.append(f"{where}.code_host.repository: required with tracker.adapter youtrack")
        return need
    if t.get("adapter") == "github-issues" and "repository" not in t:
        return [f"{where}.tracker.repository: required with adapter github-issues"]
    return []


def mcp_errors(roles, connectors, nodes):
    """`unknown-mcp-connector` errors: a connector named by a role, a mode or a node and missing from
    `targets.air.mcp_connectors` (`connectors`)."""
    known, errs = set(connectors or []), []
    named = []
    for role, r in (roles or {}).items():
        r = r if isinstance(r, dict) else {}
        named.append((f"roles.{role}", r.get("mcp")))
        named += [(f"roles.{role}.modes.{m}", (v or {}).get("mcp")) for m, v in (r.get("modes") or {}).items()]
    named += [(f"nodes.{n}", (v or {}).get("mcp")) for n, v in ((nodes or {}).get("nodes") or {}).items()]
    for where, names in named:
        errs += [f"unknown-mcp-connector: {where}: {c}" for c in names or [] if c not in known]
    return errs


def validate():
    """Error lines for the team descriptor against engine/descriptor.schema.json."""
    if (team_root() / PROJECT_DESCRIPTOR).is_file():
        import project
        return project.validate(team_root())
    schema = json.loads((engine_root() / "engine/descriptor.schema.json").read_text())
    errs = []
    _check(schema, descriptor(), DESCRIPTOR, errs)
    return errs + tracker_errors(descriptor(), DESCRIPTOR)

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
    if (_HERE / NODES).exists():
        return _HERE
    installed = Path(sys.prefix) / "share/factory-engine"
    if installed.exists():
        return installed
    try:  # an overlay install (`uv run --with <wheel>`): the data files sit beside the package, not under sys.prefix
        from importlib import metadata
        hit = next((f for f in metadata.files("slop-factory") or [] if str(f).endswith("share/factory-engine/" + NODES)), None)
        if hit:
            return Path(hit.locate()).resolve().parents[len(Path(NODES).parts) - 1]
    except metadata.PackageNotFoundError:
        pass
    return _HERE


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


def is_project(root=None):
    """A project team: `factory.yaml` + `workflow.yaml` (ADR-0021)."""
    root = Path(root or team_root())
    return all((root / f).is_file() for f in ("factory.yaml", "workflow.yaml"))


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
    defaults = yaml.safe_load((engine_root() / "engine/defaults.yaml").read_text()) or {}
    import definition
    if definition.present(root):
        return merge(defaults, definition.config(definition.load(root)))
    if (root / PROJECT_DESCRIPTOR).is_file() and not (root / DESCRIPTOR).is_file():
        raise SystemExit(f"factory: {root / PROJECT_DESCRIPTOR} is the old project layout; the engine reads "
                         "factory.yaml + workflow.yaml (ADR-0021)")
    return merge(defaults, descriptor())


def owner(cfg=None):
    """The GitHub login a person-wait is assigned to: `owner`, or the v1 descriptor's `notify.github_user`."""
    cfg = load() if cfg is None else cfg
    return cfg.get("owner") or (cfg.get("notify") or {}).get("github_user")


def _vcs_block(value):
    """A `vcs:` value as a block: a mapping as it is, a name as `{adapter: name}`."""
    return {"adapter": value} if isinstance(value, str) else dict(value) if isinstance(value, dict) else {}


def vcs(cfg, env=None):
    """The `vcs:` block that hosts the pull requests (`code_host:` is its old name); {} when neither is set. With
    `env`, environment `env`'s own `vcs:` (#1070) overrides it: unset keys come from the top-level block when the
    adapters match, else only the environment's keys count."""
    top = (cfg or {}).get("vcs") or (cfg or {}).get("code_host") or {}
    envs = (cfg or {}).get("environments")
    own = ((envs.get(env) if isinstance(envs, dict) else None) or {}).get("vcs") if env else None
    if not own or not isinstance(own, (str, dict)):
        return _vcs_block(top) if isinstance(top, str) else top
    top, own = _vcs_block(top), _vcs_block(own)
    if (own.get("adapter") or top.get("adapter") or "github") == (top.get("adapter") or "github"):
        return {**top, **own}
    return own


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
    `vcs.repository` (alias `code_host.repository`) for the pull requests; a github-issues tracker needs `repository`."""
    t = descriptor.get("tracker") if isinstance(descriptor, dict) else None
    if not isinstance(t, dict):
        return []
    if t.get("adapter") == "youtrack":
        need = [f"{where}.tracker.{k}: required with adapter youtrack" for k in ("url", "project") if k not in t]
        if not vcs(descriptor).get("repository"):
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
    if is_project():
        import project
        return project.validate(team_root())
    schema = json.loads((engine_root() / "engine/descriptor.schema.json").read_text())
    errs = []
    _check(schema, descriptor(), DESCRIPTOR, errs)
    return errs + tracker_errors(descriptor(), DESCRIPTOR)

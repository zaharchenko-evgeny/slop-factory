"""The one config loader (ADR-0015): engine defaults overridden by the team descriptor.

ENGINE_ROOT holds the engine (code, roles, skills, workflows, defaults); TEAM_ROOT holds the team's
`factory.descriptor.yaml` and any file that replaces an engine one.
"""
import json
import os
import sys
from pathlib import Path

import yaml

DESCRIPTOR = "factory.descriptor.yaml"
_HERE = Path(__file__).resolve().parents[1]


def _find_engine():
    """FACTORY_ENGINE, else this checkout, else the assets an installed tool ships under its prefix."""
    if os.environ.get("FACTORY_ENGINE"):
        return Path(os.environ["FACTORY_ENGINE"])
    installed = Path(sys.prefix) / "share/factory-engine"
    return _HERE if (_HERE / "workflows/nodes.yaml").exists() or not installed.exists() else installed


ENGINE_ROOT = _find_engine()
_team = None


def set_team(path):
    global _team
    _team = Path(path).resolve() if path else None


def team_root():
    return _team or Path(os.environ.get("FACTORY_TEAM") or Path.cwd()).resolve()


def engine_root():
    return ENGINE_ROOT


def resolve(rel):
    """TEAM_ROOT/rel when it exists, else ENGINE_ROOT/rel. A file is replaced whole."""
    mine = team_root() / rel
    return mine if mine.exists() else engine_root() / rel


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
    defaults = yaml.safe_load((engine_root() / "engine/defaults.yaml").read_text()) or {}
    return merge(defaults, descriptor())


def _check(schema, value, where, errs):
    t = schema.get("type")
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool, "integer": int, "number": (int, float)}
    if t in kinds and (not isinstance(value, kinds[t]) or (t != "boolean" and isinstance(value, bool))):
        errs.append(f"{where}: must be {t}")
        return
    if isinstance(value, dict) and ("properties" in schema or "additionalProperties" in schema):
        for k, v in value.items():
            if k in schema.get("properties", {}):
                _check(schema["properties"][k], v, f"{where}.{k}", errs)
            else:
                extra = schema.get("additionalProperties")
                if extra is False:
                    errs.append(f"unknown-key: {where}.{k}")
                elif isinstance(extra, dict):
                    _check(extra, v, f"{where}.{k}", errs)
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            _check(schema["items"], v, f"{where}[{i}]", errs)


def validate():
    """Error lines for the team descriptor against engine/descriptor.schema.json."""
    schema = json.loads((engine_root() / "engine/descriptor.schema.json").read_text())
    errs = []
    _check(schema, descriptor(), DESCRIPTOR, errs)
    return errs

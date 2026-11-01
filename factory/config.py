"""The one config loader (ADR-0015): engine defaults overridden by the team descriptor.

ENGINE_ROOT holds the engine (code, roles, skills, workflows, defaults); TEAM_ROOT holds the team's
`factory.descriptor.yaml` and any file that replaces an engine one. Until the engine has its own
repository both are this repository.
"""
import json
import os
from pathlib import Path

import yaml

DESCRIPTOR = "factory.descriptor.yaml"
_HERE = Path(__file__).resolve().parents[1]
ENGINE_ROOT = Path(os.environ.get("FACTORY_ENGINE") or _HERE)
_team = None


def set_team(path):
    global _team
    _team = Path(path).resolve() if path else None


def team_root():
    return _team or Path(os.environ.get("FACTORY_TEAM") or Path.cwd()).resolve()


def engine_root():
    """ENGINE_ROOT, or the team root when the engine files are not installed beside the code."""
    return ENGINE_ROOT if (ENGINE_ROOT / "workflows/nodes.yaml").exists() else team_root()


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
    if isinstance(value, dict) and "properties" in schema:
        for k, v in value.items():
            if k in schema["properties"]:
                _check(schema["properties"][k], v, f"{where}.{k}", errs)
            elif schema.get("additionalProperties") is False:
                errs.append(f"unknown-key: {where}.{k}")


def validate():
    """Error lines for the team descriptor against engine/descriptor.schema.json."""
    schema = json.loads((engine_root() / "engine/descriptor.schema.json").read_text())
    errs = []
    _check(schema, descriptor(), DESCRIPTOR, errs)
    return errs

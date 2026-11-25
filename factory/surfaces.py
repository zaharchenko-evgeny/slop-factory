"""Named surface instances (ADR-0029): `surfaces:` of factory.yaml, with `targets.air` and `targets.local` as aliases of
the instances `air` and `local`, and the git-ignored machine override `.factory/local.yaml`."""
from pathlib import Path

import yaml

LOCAL_FILE = Path(".factory/local.yaml")


def instances(config):
    """{name: {type, ...}}: `surfaces:` entries, then `targets.air` / `targets.local` as `air` / `local` when not named."""
    out = {k: dict(v) for k, v in (config.get("surfaces") or {}).items() if isinstance(v, dict)}
    for name in ("air", "local"):
        if name not in out and isinstance((config.get("targets") or {}).get(name), dict):
            out[name] = dict(config["targets"][name])
    return {k: {"type": k, **v} for k, v in out.items()}


def selected(config, requested=None):
    """The instance names to deploy: `requested`, else `surface:` (a name or a list), else `air` when it is set."""
    if requested:
        return list(requested)
    value = config.get("surface")
    if value:
        return list(value) if isinstance(value, list) else [value]
    return ["air"] if "air" in instances(config) else []


def types(config, names=None):
    """The surface plugin types of the selected instances (a name that is no instance is its own type)."""
    inst = instances(config)
    return sorted({(inst.get(n) or {"type": n})["type"] for n in (names if names is not None else selected(config))})


def apply_override(config, path=None):
    """Merge `.factory/local.yaml`'s `surfaces:` and `executors:` into `config` by name, key by key. An Air surface is
    never overridden: Air is deployed from the team repository only."""
    path = path or LOCAL_FILE
    over = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    base = instances(config)
    for name, vals in (over.get("surfaces") or {}).items():
        if (base.get(name) or vals or {}).get("type", name) == "air" or (vals or {}).get("type") == "air":
            continue
        config.setdefault("surfaces", {})[name] = {**base.get(name, {}), **(vals or {})}
    for name, vals in (over.get("executors") or {}).items():
        config.setdefault("executors", {})[name] = {**(config.get("executors") or {}).get(name, {}), **(vals or {})}
    return config


def executor_type(config, name):
    return ((config.get("executors") or {}).get(name) or {}).get("type", name)


def serves(instance, node_name):
    """True when the instance's optional `nodes:` filter lets it serve node `node_name`."""
    listed = instance.get("nodes")
    return listed is None or node_name in listed


def errors(config, nodes=None, names=None):
    """`air-surface-has-<key>`, `unknown-executor`, `bad-max-parallel`, `host-parallel` for every instance;
    `unknown-surface` and, with the node graph, `node-on-two-surfaces` for the selection."""
    errs, inst, known = [], instances(config), set(config.get("executors") or {}) | {"host"}
    for name, i in inst.items():
        if i["type"] == "air":
            errs += [f"air-surface-has-{k}: surfaces.{name}: Air runs the agent itself (ADR-0018), drop `{k}`"
                     for k in ("harness", "executor", "max_parallel") if k in i]
        if i["type"] != "local":
            continue
        ex = i.get("executor", "host")
        if ex not in known:
            errs.append(f"unknown-executor: surfaces.{name}: {ex} (known: {', '.join(sorted(known))})")
        mp = i.get("max_parallel", 1)
        if not isinstance(mp, int) or isinstance(mp, bool) or mp < 1:
            errs.append(f"bad-max-parallel: surfaces.{name}: {mp!r} must be a positive integer")
        elif mp > 1 and executor_type(config, ex) == "host":
            errs.append(f"host-parallel: surfaces.{name}: max_parallel {mp} needs an isolated executor, `host` runs one at a time")
    sel = selected(config, names)
    errs += [f"unknown-surface: {n}: no `surfaces.{n}` or `targets.{n}`" for n in sel if n not in inst and n not in ("air", "local")]
    if nodes:
        for node, spec in nodes["nodes"].items():
            if spec.get("enabled") is False or spec.get("role") == "human":
                continue
            serving = [n for n in sel if serves(inst.get(n) or {}, node)]
            if len(serving) > 1:
                errs.append(f"node-on-two-surfaces: nodes.{node}: {', '.join(serving)}")
    return errs

"""The team definition in the layout of ADR-0021: `factory.yaml` (what the factory has), `workflow.yaml` (how it
works) and `roles/<role>/role.md` (instructions).

`load(root)` reads the files; `errors(defn)` lists every problem; `warnings(defn)` lists what is legal but likely
wrong; `settings(defn, node)` is a node's merged settings; `render(defn, node)` renders its role body and prompt.
Nothing else in the engine reads this layout yet (#669 compiles it into the config and the node graph).
"""
from pathlib import Path

import yaml

FACTORY = "factory.yaml"
WORKFLOW = "workflow.yaml"
ROLES = "roles"
NO_ROLE = ("script", "human")  # harnesses that run no role


def present(root):
    """True when `root` holds a definition in this layout."""
    root = Path(root)
    return (root / FACTORY).is_file() and (root / WORKFLOW).is_file()


def load(root):
    root = Path(root)
    factory = yaml.safe_load((root / FACTORY).read_text()) or {}
    workflow = yaml.safe_load((root / WORKFLOW).read_text()) or {}
    bodies = {p.parent.name: p.read_text() for p in sorted((root / ROLES).glob("*/role.md"))}
    return {"root": root, "factory": factory, "workflow": workflow, "bodies": bodies}


def merge(base, over):
    """`over` on top of `base`, deep for dicts; a `None` value clears the key."""
    out = dict(base or {})
    for k, v in (over or {}).items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def _parts(defn, name):
    """(node, agent, role, environment) of node `name`; missing parts are `{}`."""
    f, w = defn["factory"], defn["workflow"]
    node = (w.get("nodes") or {}).get(name) or {}
    agent = (f.get("agents") or {}).get(node.get("agent")) or {}
    role = (f.get("roles") or {}).get(agent.get("role")) or {}
    env_name = node.get("environment") or (w.get("settings") or {}).get("environment")
    env = (f.get("environments") or {}).get(env_name) or {}
    return node, agent, role, env


def settings(defn, name):
    """Merged settings of a node: defaults -> environment -> role -> agent -> workflow settings -> node."""
    node, agent, role, env = _parts(defn, name)
    out = (defn["factory"].get("defaults") or {}).get("settings") or {}
    for layer in (env.get("settings"), role.get("settings"), agent.get("settings"),
                  defn["workflow"].get("settings"), node.get("settings")):
        out = merge(out, layer)
    return out


def _jinja(defn):
    import jinja2  # only this layout needs it; the old loaders run with pyyaml alone
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(defn["root"])), undefined=jinja2.StrictUndefined,
                              keep_trailing_newline=True)


def render(defn, name):
    """(role body, prompt) of node `name`, rendered with its merged settings. Raises on an undefined name."""
    node, agent, role, env = _parts(defn, name)
    ctx = {"settings": settings(defn, name), "node": {**node, "name": name},
           "agent": {**agent, "name": node.get("agent")}, "role": role, "environment": env,
           "factory": {k: v for k, v in defn["factory"].items() if k != "environments"}}
    j = _jinja(defn)
    body = defn["bodies"].get(agent.get("role"), "") if agent.get("role") else ""
    return j.from_string(body).render(ctx), j.from_string(node.get("prompt") or "").render(ctx)


def _skill_errors(where, skill, known):
    if isinstance(skill, str):
        return [] if skill in known else [f"unknown-skill: {where}: {skill}"]
    if isinstance(skill, dict) and len(skill) == 1:
        (name, spec), = skill.items()
        return _skill_spec_errors(f"{where}: {name}", spec)
    return [f"bad-skill: {where}: {skill!r}"]


def _skill_spec_errors(where, spec):
    spec = spec or {}
    if "source" in spec:
        return [] if spec.get("sha256") and spec.get("path") else [f"unpinned-skill: {where}: a sourced skill needs path and sha256"]
    return [] if spec.get("path") else [f"bad-skill: {where}: needs `path` or `source`"]


def errors(defn):
    f, w = defn["factory"], defn["workflow"]
    errs = []
    models = f.get("models") or {}
    skills = f.get("skills") or {}
    roles = f.get("roles") or {}
    agents = f.get("agents") or {}
    envs = f.get("environments") or {}
    hooks = f.get("webhooks") or {}
    nodes = w.get("nodes") or {}
    errs += [e for n, s in skills.items() for e in _skill_spec_errors(f"skills.{n}", s)]
    for s in (f.get("defaults") or {}).get("skills") or []:
        errs += _skill_errors("defaults.skills", s, skills)
    for r, spec in roles.items():
        if r not in defn["bodies"]:
            errs.append(f"missing-role-body: roles.{r}: no {ROLES}/{r}/role.md")
        for s in (spec or {}).get("skills") or []:
            errs += _skill_errors(f"roles.{r}", s, skills)
    errs += [f"unlisted-role: {ROLES}/{r}: not in `roles`" for r in defn["bodies"] if r not in roles]
    for a, spec in agents.items():
        spec = spec or {}
        if spec.get("harness") not in NO_ROLE and spec.get("role") not in roles:
            errs.append(f"unknown-role: agents.{a}: {spec.get('role')}")
        if spec.get("model") is not None and spec["model"] not in models:
            errs.append(f"unknown-model: agents.{a}: {spec['model']}")
        if spec.get("harness") != "human" and spec.get("model") is None and (f.get("defaults") or {}).get("model") is None:
            errs.append(f"no-model: agents.{a}")
    for e, spec in envs.items():
        repos = (spec or {}).get("repositories") or []
        declared = set(((f.get("defaults") or {}).get("env") or {})) | set((spec or {}).get("env") or {})
        if sum(1 for r in repos if r.get("main")) != 1:
            errs.append(f"main-repository: environments.{e}: needs exactly one `main: true`")
        for r in repos:
            if r.get("write") and not r.get("main") and not r.get("token_env"):
                errs.append(f"write-token: environments.{e}: {r.get('repo')} is writable without `token_env`")
            if r.get("token_env") and r["token_env"] not in declared:
                errs.append(f"undeclared-env: environments.{e}: {r['token_env']}")
    default_env = (w.get("settings") or {}).get("environment")
    for name, n in nodes.items():
        where = f"nodes.{name}"
        if n.get("agent") not in agents:
            errs.append(f"unknown-agent: {where}: {n.get('agent')}")
            continue
        human = (agents[n["agent"]] or {}).get("harness") == "human"
        env = n.get("environment") or default_env
        if not human and env not in envs:
            errs.append(f"unknown-environment: {where}: {env}")
        if n.get("model") is not None and n["model"] not in models:
            errs.append(f"unknown-model: {where}: {n['model']}")
        if not n.get("triggers"):
            errs.append(f"no-trigger: {where}")
        for t in n.get("triggers") or []:
            if isinstance(t, dict) and "webhook" in t and t["webhook"] not in hooks:
                errs.append(f"unknown-webhook: {where}: {t['webhook']}")
        outcomes = n.get("outcomes") or {}
        if not outcomes:
            errs.append(f"no-outcome: {where}")
        for o, oc in outcomes.items():
            oc = oc or {}
            if oc.get("else") is not None and oc["else"] not in outcomes:
                errs.append(f"unknown-else: {where}.{o}: {oc['else']}")
        if not human and n.get("enabled") is not False and env in envs:
            try:
                render(defn, name)
            except __import__("jinja2").TemplateError as e:
                errs.append(f"render: {where}: {e}")
    return errs


def warnings(defn):
    """A label an outcome adds that no node triggers on (a person may add it, so it is not an error)."""
    nodes = (defn["workflow"].get("nodes") or {}).values()
    triggered = {t["label"] for n in nodes for t in n.get("triggers") or [] if isinstance(t, dict) and "label" in t}
    added = {e["label"] for n in nodes for oc in (n.get("outcomes") or {}).values()
             for e in (oc or {}).get("effects") or [] if isinstance(e, dict) and "label" in e}
    return [f"unwatched-label: {label}: no node triggers on it" for label in sorted(added - triggered)]

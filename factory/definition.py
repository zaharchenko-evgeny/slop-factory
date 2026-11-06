"""The team definition in the layout of ADR-0021: `factory.yaml` (what the factory has), `workflow.yaml` (how it
works) and `roles/<role>/role.md` (instructions).

`load(root)` reads the files; `errors(defn)` lists every problem; `warnings(defn)` lists what is legal but likely
wrong; `settings(defn, node)` is a node's merged settings; `render(defn, node)` renders its role body and prompt.
Nothing else in the engine reads this layout yet (#669 compiles it into the config and the node graph).
"""
import re
from pathlib import Path

import yaml

FACTORY = "factory.yaml"
WORKFLOW = "workflow.yaml"
ROLES = "roles"
NO_ROLE = ("script", "human")  # harnesses that run no role
STARTUP = ".air/cloud/startup.sh"  # Air runs it from the checkout; it runs `factory setup` (ADR-0021)


def present(root):
    """True when `root` holds a definition in this layout."""
    root = Path(root)
    return (root / FACTORY).is_file() and (root / WORKFLOW).is_file()


def _keys(x):
    """YAML 1.1 reads the key `on` (an effect's `on: pr`) as True; give it back its name."""
    if isinstance(x, dict):
        return {("on" if k is True else k): _keys(v) for k, v in x.items()}
    return [_keys(v) for v in x] if isinstance(x, list) else x


def load(root):
    root = Path(root)
    factory = _keys(yaml.safe_load((root / FACTORY).read_text()) or {})
    workflow = _keys(yaml.safe_load((root / WORKFLOW).read_text()) or {})
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


def node_name(step, node=None, tier=None, focus=None):
    """The definition's node for an engine node and its variant: `implement` + tier `cheap` -> `implement-cheap`."""
    if step in FAMILIES and (tier or focus):
        return f"{step}-{tier or focus}"
    return step


def role_skills(defn, role):
    """{name: spec} of every skill an agent of `role` gets: defaults, then the role's (named or inline)."""
    f = defn["factory"]
    known, out = f.get("skills") or {}, {}
    for s in [*((f.get("defaults") or {}).get("skills") or []), *(((f.get("roles") or {}).get(role) or {}).get("skills") or [])]:
        if isinstance(s, str):
            out[s] = known[s]
        else:
            out.update(s)
    return out


def skill_lines(defn, role):
    """One line per skill of the role, for the agent's prompt; setup installs them under ~/.agents/skills."""
    return "\n".join(f"- {name}: `~/.agents/skills/{name}/SKILL.md`" for name in role_skills(defn, role))


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
    def watched(label):  # `factory:fix-<tier>`, `factory:implement-<next tier>`: a variant the engine picks
        prefix = re.split(r"<[a-z ]+>|\*", label)[0]
        return label in triggered or (prefix != label and any(t.startswith(prefix) for t in triggered))
    return [f"unwatched-label: {label}: no node triggers on it" for label in sorted(added) if not watched(label)]


# --- Compile into the shapes the engine reads (#669) ---------------------------------------------
# The gate's delivery logic names its steps (`implement`, `fix`, `update`, `review`) and fills their variant
# (`<tier>` from complexity, `<focus>` from risk). A node `<step>-<variant>` is that step for one variant;
# the variants of a step compile into one engine node whose `tiers` or `focuses` are the variants.
FAMILIES = {"implement": "tier", "fix": "tier", "update": "tier", "review": "focus"}
TIER_ORDER = ["cheap", "mid", "strong"]
PR_COMMANDS = {"fix", "update", "review", "rereview", "adopt"}  # labels the engine keeps on the PR
TRIGGER_WAKES = {("pr", "reviewed"): "pr reviewed", ("pr", "pushed"): "pr pushed", ("pr", "opened"): "pr opened",
                 ("pr", "merged"): "state pr-merged", ("ticket", "closed"): "ticket closed",
                 ("ticket", "commented"): "ticket commented"}


def family(name):
    """(step, variant) of a node named `<step>-<variant>`, else (name, None)."""
    step, _, variant = name.partition("-")
    return (step, variant) if step in FAMILIES and variant else (name, None)


def _command(label, prefix):
    """The engine command a label is: (`implement-<tier>` with fill `cheap`) or a plain name."""
    name = label[len(prefix):] if label.startswith(prefix) else label
    step, _, variant = name.partition("-")
    if step in FAMILIES or step == "rereview":
        kind = "focus" if step in ("review", "rereview") else "tier"
        return f"{step}-<{kind}>", variant
    return name, None


def _model_id(models, alias):
    """`harness/model` of an alias: `{harness, model}` or that string."""
    m = models.get(alias) or {}
    if isinstance(m, str):
        return m
    return f"{m.get('harness')}/{m.get('model')}" if m else alias


def team_url(root):
    """The URL of the repository that holds the definition (ADR-0021: implicit): line 2 of a copy's SOURCE, else
    the origin of a git checkout whose top level is `root`."""
    import subprocess
    root = Path(root)
    source = root / "SOURCE"
    if source.is_file() and len(source.read_text().split()) > 1:
        return source.read_text().split()[1]
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
        if top.returncode or Path(top.stdout.strip()).resolve() != root.resolve():
            return None
        url = subprocess.run(["git", "-C", str(root), "remote", "get-url", "origin"], capture_output=True, text=True)
        return url.stdout.strip().removesuffix(".git") or None if url.returncode == 0 else None
    except OSError:
        return None


def to_project(defn):
    """(project descriptor, format-2 workflow) of the definition, for `project.legacy_config` and
    `workflow.to_nodes`. The definition must have no `errors`."""
    f, w = defn["factory"], defn["workflow"]
    nodes, agents, envs = w.get("nodes") or {}, f.get("agents") or {}, f.get("environments") or {}
    models = f.get("models") or {}
    defaults = f.get("defaults") or {}
    dset, wset = defaults.get("settings") or {}, w.get("settings") or {}
    stages = w.get("stages") or {}
    prefix = stages.get("label_prefix", "factory:")
    default_env = wset.get("environment")

    # Repositories: every listed one; the primary is the main repository of the default environment.
    primary = next(r["repo"] for r in envs[default_env]["repositories"] if r.get("main"))
    names, repositories = {}, []
    for e in envs.values():
        for r in e.get("repositories") or []:
            if r["repo"] not in names:
                names[r["repo"]] = r["repo"].split("/")[-1]
                repositories.append({"name": names[r["repo"]], "url": f"https://github.com/{r['repo']}",
                                     "default_branch": r.get("default_branch", "main"),
                                     "role": "primary" if r["repo"] == primary else "project"})
    environments = {}
    for en, e in envs.items():
        repos = sorted(e.get("repositories") or [], key=lambda r: not r.get("main"))
        secrets = sorted(k for k, v in {**(defaults.get("env") or {}), **(e.get("env") or {})}.items()
                         if (v or {}).get("secret") and k != "GH_TOKEN")
        environments[en] = {"repositories": [names[r["repo"]] for r in repos], "secrets": secrets,
                            "domains": list(e.get("domains") or []), "vm": e.get("vm", "default"),
                            "startup": STARTUP}

    # Stages: every name the nodes use, plus the engine's PR facts.
    used = set()
    for n in nodes.values():
        used |= {t["stage"] for t in n.get("triggers") or [] if isinstance(t, dict) and "stage" in t}
        if (n.get("when") or {}).get("stage"):
            used.add(n["when"]["stage"])
        for oc in (n.get("outcomes") or {}).values():
            used |= {e["stage"] for e in (oc or {}).get("effects") or [] if "stage" in e}
            st = ((oc or {}).get("if") or {}).get("stage")
            used |= set(st if isinstance(st, list) else [st] if st and not str(st).startswith("!") else [])
    used.discard("none")
    kind = {s: k for k in ("final", "waiting") for s in stages.get(k) or []}
    states = {s: {"kind": kind.get(s, "active"), "store": {"label": f"{prefix}{s}"}} for s in sorted(used)}
    states["pr-merged"] = {"kind": "final", "store": {"pr": "merged"}}

    # Commands: every label a trigger or an effect names.
    labels, on_pr = set(), set()
    for n in nodes.values():
        labels |= {t["label"] for t in n.get("triggers") or [] if isinstance(t, dict) and "label" in t}
        for oc in (n.get("outcomes") or {}).values():
            for e in (oc or {}).get("effects") or []:
                if "label" in e or "unlabel" in e:
                    labels.add(e.get("label") or e.get("unlabel"))
                    if e.get("on") == "pr":
                        on_pr.add(_command(e.get("label") or e.get("unlabel"), prefix)[0])
    commands = {}
    for label in labels:
        cmd, _ = _command(label, prefix)
        if cmd in commands:
            continue
        store = "pr_label" if cmd.partition("-")[0] in PR_COMMANDS or cmd in on_pr else "label"
        commands[cmd] = {"store": {store: f"{prefix}{cmd}"}}
    if "review-<focus>" in commands:
        commands.setdefault("rereview-<focus>", {"store": {"pr_label": f"{prefix}rereview-<focus>"}})

    # Models: a tier per implement/fix/update variant, the alias for everything else.
    tier_models, errors_ = {}, []
    for name, n in nodes.items():
        step, variant = family(name)
        if FAMILIES.get(step) == "tier" and n.get("enabled") is not False:
            alias = n.get("model") or agents[n["agent"]].get("model") or defaults.get("model")
            if tier_models.setdefault(variant, alias) != alias:
                raise ValueError(f"tier {variant}: nodes give it models {tier_models[variant]} and {alias}")
    air_models = {a: _model_id(models, a) for a in models}
    air_models.update({t: _model_id(models, a) for t, a in tier_models.items()})

    def role_of(agent_name):
        a = agents[agent_name]
        return {"script": "script", "human": "human"}.get(a.get("harness")) or a["role"]

    roles = {}
    for name, n in nodes.items():
        step, variant = family(name)
        a = agents[n["agent"]]
        role = role_of(n["agent"])
        if role in ("script", "human"):
            continue
        entry = roles.setdefault(role, {})
        alias = n.get("model") or a.get("model") or defaults.get("model")
        if FAMILIES.get(step) == "focus":
            entry.setdefault("modes", {})[variant] = {"tier": alias}
        elif FAMILIES.get(step) != "tier":
            entry.setdefault("tier", alias)
    for role, entry in roles.items():
        if "modes" in entry:
            entry.setdefault("tier", next(iter(entry["modes"].values()))["tier"])

    # Nodes: one engine node per step; its variants are its tiers or focuses.
    wf_nodes, schedules = {}, {}
    for name, n in nodes.items():
        step, variant = family(name)
        if step in wf_nodes:  # a later variant of the same step
            wf_nodes[step]["tiers" if FAMILIES[step] == "tier" else "focuses"].append(variant)
            continue
        wf_nodes[step] = _node(name, n, agents, role_of, prefix, schedules, envs, default_env, sorted(used),
                               {c for c, v in commands.items() if "pr_label" in v["store"]})
        if variant:
            wf_nodes[step]["tiers" if FAMILIES[step] == "tier" else "focuses"] = [variant]
    for node in wf_nodes.values():
        if "tiers" in node:
            node["tiers"].sort(key=TIER_ORDER.index)

    join = nodes.get("join") or {}
    merge_settings = (nodes.get("merge") or {}).get("settings") or {}
    notify = (nodes.get("notify") or {}).get("settings") or {}
    fix_rounds = next((v["settings"]["fix_rounds"] for k, v in nodes.items()
                       if family(k)[0] == "fix" and "fix_rounds" in (v.get("settings") or {})), None)
    team = team_url(defn["root"])
    descriptor = {
        "engine": 1,
        **({"team_repository": team} if team else {}),
        "project": {"name": f.get("name")},
        "tracker": {**(f.get("tracker") or {}),
                    **({"lesson_label": (dset.get("knowledge") or {})["lesson_label"]}
                       if (dset.get("knowledge") or {}).get("lesson_label") else {})},
        "repositories": repositories,
        "environments": environments,
        "workflow": WORKFLOW,
        "states": states,
        "commands": commands,
        "branch_pattern": dset.get("branch_pattern"),
        "commit_prefix": dset.get("commit_prefix"),
        "targets": {"air": {"bot_login": dset.get("bot_login"), "models": air_models}},
        "roles": roles,
        "limits": {**(wset.get("limits") or {}), **({"fix_rounds": fix_rounds} if fix_rounds else {})},
        "gates": {"auto_merge": {"enabled": bool(merge_settings.get("auto_merge", True)),
                                 "max_risk": merge_settings.get("max_risk", "high")},
                  "confirm_external_writes": dset.get("confirm_external_writes", True),
                  "confirm_external_writes_scope": dset.get("confirm_external_writes_scope") or []},
        "reviews": {"trusted_bots": [], **({"required": join["settings"]["required"]}
                                          if (join.get("settings") or {}).get("required") else {})},
        **({"notify": {"github_user": notify["github_user"]}} if notify.get("github_user") else {}),
    }
    return descriptor, {"format": 2, "adapters": {"ticket": (f.get("tracker") or {}).get("adapter", "github-issues"),
                                                  "pr": "github"},
                        "schedules": schedules,
                        "webhooks": {k: {"description": (v or {}).get("description", k)}
                                     for k, v in (f.get("webhooks") or {}).items()},
                        "nodes": wf_nodes}


def _wake(t, name, prefix, schedules, env):
    if t == "manual":
        return "manual"
    (k, v), = t.items()
    if k == "stage":
        return f"state {v}"
    if k == "label":
        cmd, fill = _command(v, prefix)
        if fill and family(name)[1]:  # a variant node wakes on its step's template: the engine fills it
            return f"command {cmd}"
        return f"command {cmd.split('<')[0]}{fill}" if fill else f"command {cmd}"
    if k == "schedule":
        key = name if name not in schedules or schedules[name]["cron"] == v else f"{name}-{len(schedules)}"
        schedules[key] = {"cron": v}
        return f"schedule {key}"
    if k == "webhook":
        return f"webhook {v}"
    if k == "ci":
        return f"run {(env.get('ci') or {}).get('workflow', 'quality-gates')}"
    return TRIGGER_WAKES[(k, v)]


def _effect(e, prefix):
    out = {k: v for k, v in e.items() if k == "on"}
    key = next(k for k in e if k != "on")
    if key == "stage":
        return {"state": e["stage"], **out}
    if key in ("label", "unlabel"):
        cmd, fill = _command(e[key], prefix)
        ref = f"{cmd.split('<')[0]}{fill}" if fill else cmd
        return {("command" if key == "label" else "uncommand"): ref}
    return dict(e)


def _node(name, n, agents, role_of, prefix, schedules, envs, default_env, all_stages, pr_cmds):
    env_name = n.get("environment") or default_env
    a = agents[n["agent"]]
    role = role_of(n["agent"])
    when = dict(n.get("when") or {})
    when = ({"state" if k == "stage" else k: v for k, v in when.items()} if when else {"always": True})
    if role == "human":
        when = {"actor": "human"}
    pr_woken = any(isinstance(t, dict) and next(iter(t)) in ("pr", "ci") or
                   (isinstance(t, dict) and "label" in t and _command(t["label"], prefix)[0] in pr_cmds)
                   for t in n.get("triggers") or [])
    out = {"env": env_name} if role != "human" else {}
    out["wakes_on"] = [_wake(t, name, prefix, schedules, envs.get(env_name) or {}) for t in n.get("triggers") or []]
    out["when"] = when
    out["role"] = role
    if role == "script":
        out["run"] = n.get("run") or a.get("run")
    if role not in ("script", "human") and not family(name)[1]:
        out["tier"] = n.get("model") or a.get("model")
    if role != "human":
        out["output"] = n.get("output", "read-only")
    if pr_woken and "ci" not in when:
        out["ci-independent"] = True
    if n.get("enabled") is False:
        out["enabled"] = False
    outcomes = {}
    for o, oc in (n.get("outcomes") or {}).items():
        oc = dict(oc or {})
        guard = dict(oc.pop("if", None) or {})
        res = {}
        st = guard.pop("stage", None)
        if st is not None:
            if isinstance(st, str) and st.startswith("!"):
                res["from"] = [s for s in all_stages if s != st[1:]]
            else:
                res["from"] = st if isinstance(st, list) else [st]
        if "ci" in guard:
            ci = guard.pop("ci")
            res["ci"] = ci if isinstance(ci, list) else [ci]
        if guard:
            res["if"] = guard  # risk, complexity: the gate checks them (gate.guard_holds)
        for k in ("else", "enabled"):
            if k in oc:
                res[k] = oc[k]
        if "effects" in oc:
            res["effects"] = [_effect(e, prefix) for e in oc["effects"] if next(k for k in e if k != "on") != "close"]
        outcomes[o] = res
    out["outcomes"] = outcomes
    return out

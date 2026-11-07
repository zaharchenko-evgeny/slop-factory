"""The team definition in the layout of ADR-0021: `factory.yaml` (what the factory has), `workflow.yaml` (how it
works) and `roles/<role>/role.md` (instructions).

`load(root)` reads the files; `errors(defn)` lists every problem; `warnings(defn)` lists what is legal but likely
wrong; `settings(defn, node)` is a node's merged settings; `render(defn, node)` renders its role body.
Nothing else in the engine reads this layout yet (#669 compiles it into the config and the node graph).
"""
import re
from pathlib import Path

import yaml

FACTORY = "factory.yaml"
WORKFLOW = "workflow.yaml"
ROLES = "roles"
NO_ROLE = ("script", "human")  # harnesses that run no role
GUARDS = ("stage", "label", "pr", "ci", "review", "blockers", "children", "ticket", "mention")  # the facts a guard reads (gate.GUARD_KEYS)
# Nodes planned once per code repository when `write_mode` is `per-repo`: Air pushes only to an automation's own repository.
PER_REPO = ("implement", "fix", "update", "review", "join", "merge", "finish")
TIER_SUFFIXES = ("-small", "-medium", "-large", "-low", "-high")  # a tiered node: `implement-medium`, `review-high`
STARTUP = ".air/cloud/startup.sh"  # Air runs it from the checkout; it runs `factory setup` (ADR-0021)


def per_repo_node(name):
    """True when a node (or its tiered form, `implement-medium`) is planned once per code repository."""
    return name.removesuffix(next((t for t in TIER_SUFFIXES if name.endswith(t)), "")) in PER_REPO


def present(root):
    """True when `root` holds a definition in this layout."""
    root = Path(root)
    return (root / FACTORY).is_file() and (root / WORKFLOW).is_file()


def full_name(url):
    """`owner/repo` of a repository URL or name."""
    return "/".join(str(url).removesuffix(".git").rstrip("/").split("/")[-2:])


def code_repos(defn):
    """The code repositories of a definition, in order: those its environments list, less the team repository (the
    one that holds the definition) and the knowledge base."""
    f = defn["factory"]
    knowledge = (((f.get("defaults") or {}).get("settings") or {}).get("knowledge") or {}).get("repo")
    skip = {full_name(u) for u in (team_url(defn["root"]), knowledge) if u}
    repos = [r["repo"] for e in (f.get("environments") or {}).values() for r in (e or {}).get("repositories") or []]
    return [r for r in dict.fromkeys(repos) if full_name(r) not in skip]


def per_repo_envs(defn):
    """{code repository: the first environment where it is `main`} for the repositories that have one."""
    out = {}
    for name, e in (defn["factory"].get("environments") or {}).items():
        for r in (e or {}).get("repositories") or []:
            if r.get("main"):
                out.setdefault(r["repo"], name)
    return {r: out[r] for r in code_repos(defn) if r in out}


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
    defn = {"root": root, "factory": factory, "workflow": workflow, "bodies": bodies}
    _add_mentions(defn)
    return defn


ROUTER = "mentions"  # the generated node that routes @factory:<agent> to that agent's reply step (#590)


def _add_mentions(defn):
    """#590: an agent with `mentionable: true` can be asked with @factory:<agent>. Add, in memory, one router node
    (a comment or review -> the asked agent's reply label, by the `mention` fact) and a `reply-<agent>` node per
    mentionable agent (its role in reply mode, read-only, any stage). Both are marked `generated`."""
    f, w = defn["factory"], defn["workflow"]
    agents, nodes = f.setdefault("agents", {}), w.setdefault("nodes", {})
    prefix = (w.get("stages") or {}).get("label_prefix", "factory:")
    askable = [a for a, spec in agents.items() if (spec or {}).get("mentionable")]
    if not askable or ROUTER in nodes:
        return
    pairs = neighbours(defn)
    for n in nodes.values():  # an `ask` outcome (#591): the agents its agent may ask, the deploy-time allowlist
        if n.get("agent") in askable and n.get("mention") is not False and not n.get("generated") \
                and not all(isinstance(t, dict) and "schedule" in t for t in n.get("triggers") or [None]):
            to = sorted(b for b in askable if frozenset((n["agent"], b)) in pairs)
            n.setdefault("outcomes", {}).setdefault("ask", {"to": to, "effects": [{"comment": "text"}]})
    agents.setdefault("mention-router", {"run": "decide", "model": (f.get("defaults") or {}).get("model"),
                                         "access": "read-only", "generated": True})
    nodes[ROUTER] = {"agent": "mention-router", "generated": True,
                     "triggers": [{"ticket": "commented"}, {"pr": "commented"}, {"pr": "reviewed"}],
                     "outcomes": {**{f"reply-{a}": {"if": {"mention": a}, "effects": [{"label": f"{prefix}reply-{a}"}]}
                                     for a in askable}, "none": {}}}
    for a in askable:
        nodes.setdefault(f"reply-{a}", {
            "agent": a, "generated": True, "triggers": [{"label": f"{prefix}reply-{a}"}], "output": "read-only",
            "settings": {"mode": "reply"},
            "outcomes": {"replied": {"effects": [{"unlabel": f"{prefix}reply-{a}"}, {"comment": "reply", "on": "mention"}]}}})


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


def environment_secrets(defn, env_name):
    """{name: description} of the secrets `env_name` needs: those of the defaults and the environment, and those of
    the role, agent and node of every node that runs in it. GH_TOKEN is Air's own and is left out."""
    f, w = defn["factory"], defn["workflow"]
    layers = [(f.get("defaults") or {}), ((f.get("environments") or {}).get(env_name) or {})]
    for name in w.get("nodes") or {}:
        node, agent, role, _ = _parts(defn, name)
        if (node.get("environment") or (w.get("settings") or {}).get("environment")) == env_name:
            layers += [role, agent, node]
    out = {}
    for layer in layers:
        for k, v in (layer.get("env") or {}).items():
            if (v or {}).get("secret") and k != "GH_TOKEN":
                out[k] = (v or {}).get("description") or out.get(k) or ""
    return dict(sorted(out.items()))


def settings(defn, name):
    """Merged settings of a node: defaults -> environment -> role -> agent -> workflow settings -> node."""
    node, agent, role, env = _parts(defn, name)
    out = (defn["factory"].get("defaults") or {}).get("settings") or {}
    for layer in (env.get("settings"), role.get("settings"), agent.get("settings"),
                  defn["workflow"].get("settings"), node.get("settings")):
        out = merge(out, layer)
    return out


AIR_CONNECTORS = ("confluence", "datadog", "figma", "gitlab", "google_calendar", "grafana", "jira", "linear", "sentry",
                  "slack", "teamcity", "internal-teamcity", "youtrack", "internal-youtrack", "internal-glean")
CUSTOM_KEYS = ("command", "args", "env", "url", "headers")  # the `.mcp.json` `mcpServers` entry shape
SECRET_KEY = re.compile(r"token|secret|password|passwd|api[_-]?key|authorization|credential", re.I)
SECRET_VALUE = re.compile(r"ghp_\w|github_pat_|sk-\w|xox[abp]-|AKIA[0-9A-Z]{8}|[A-Za-z0-9+/_-]{32,}")
REF = re.compile(r"\$\{(\w+)\}")


def _mcp_layers(defn, name):
    """[(where, mcp map, env names declared there or outside it)] of node `name`, in merge order: defaults ->
    environment -> role -> agent -> node."""
    node, agent, role, env = _parts(defn, name)
    f = defn["factory"]
    env_name = node.get("environment") or (defn["workflow"].get("settings") or {}).get("environment")
    raw = [("defaults", f.get("defaults") or {}), (f"environments.{env_name}", env), (f"roles.{agent.get('role')}", role),
           (f"agents.{node.get('agent')}", agent), (f"nodes.{name}", node)]
    declared, out = set(), []
    for where, layer in raw:
        declared = declared | set(layer.get("env") or {})
        out.append((where, layer.get("mcp") or {}, declared))
    return out


def mcp(defn, name):
    """{server name: entry} of node `name`: the layers merged by key, a `null` entry removing the inherited one."""
    out = {}
    for _, layer, _ in _mcp_layers(defn, name):
        for key, entry in layer.items():
            if entry is None:
                out.pop(key, None)
            else:
                out[key] = entry
    return out


def mcp_connectors(entries):
    """The Air connector ids of the `connector` entries, for the automation's `mcpConnectors`."""
    return [e["connector"] for e in entries.values() if "connector" in e]


def mcp_custom(entries):
    """{server name: entry} of the custom entries: the content of `.mcp.json` `mcpServers`."""
    return {k: e for k, e in entries.items() if "connector" not in e}


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def _secret_literals(entry):
    """The `env` and `headers` keys whose value looks like a secret and is not a `${VAR}` reference."""
    bad = []
    for field in ("env", "headers"):
        for k, v in (entry.get(field) or {}).items():
            rest = re.sub(r"^(Bearer|Basic|Token)\b\s*", "", REF.sub("", str(v)).strip(), flags=re.I)
            if rest and (SECRET_KEY.search(k) or SECRET_VALUE.search(rest)):
                bad.append(f"{field}.{k}")
    return bad


def _mcp_errors(defn):
    f, w = defn["factory"], defn["workflow"]
    known = set(((f.get("targets") or {}).get("air") or {}).get("mcp_connectors") or AIR_CONNECTORS)
    raw = [("defaults", (f.get("defaults") or {}).get("mcp"))]
    for section, owner in (("environments", f), ("roles", f), ("agents", f), ("nodes", w)):
        raw += [(f"{section}.{n}", (spec or {}).get("mcp")) for n, spec in (owner.get(section) or {}).items()]
    errs = []
    for where, layer in raw:
        for key, e in (layer or {}).items():
            at = f"{where}.mcp.{key}"
            if e is None:
                continue
            if not isinstance(e, dict):
                errs.append(f"bad-mcp-entry: {at}: expected a mapping or null")
                continue
            custom = [k for k in CUSTOM_KEYS if k in e]
            if "connector" in e and custom:
                errs.append(f"mcp-mixed-entry: {at}: `connector` with {', '.join(custom)}")
            elif "connector" in e:
                if e["connector"] not in known:
                    errs.append(f"unknown-mcp-connector: {at}: {e['connector']}")
            elif not ({"command", "url"} & set(e)):
                errs.append(f"bad-mcp-entry: {at}: needs `connector`, `command` or `url`")
            errs += [f"mcp-literal-secret: {at}.{k}: use a ${{VAR}} reference" for k in _secret_literals(e)]
    for name in w.get("nodes") or {}:
        for where, layer, declared in _mcp_layers(defn, name):
            for key, e in layer.items():
                missing = sorted({v for s in _strings(e) for v in REF.findall(s)} - declared)
                errs += [f"mcp-undeclared-env: {where}.mcp.{key}: ${{{v}}} is declared by no `env` at this or an outer layer"
                         for v in missing]
    return list(dict.fromkeys(errs))


def _jinja(defn):
    import jinja2  # only this layout needs it; the old loaders run with pyyaml alone
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(defn["root"])), undefined=jinja2.StrictUndefined,
                              keep_trailing_newline=True)


def render(defn, name):
    """The role body of node `name`, rendered with its merged settings; a node's own instructions come from its
    settings (`mode`, `focus`), never from a prompt of its own. Raises on an undefined name."""
    node, agent, role, env = _parts(defn, name)
    ctx = {"settings": settings(defn, name), "node": {**node, "name": name},
           "agent": {**agent, "name": node.get("agent")}, "role": role, "environment": env,
           "factory": {k: v for k, v in defn["factory"].items() if k != "environments"}}
    j = _jinja(defn)
    body = defn["bodies"].get(agent.get("role"), "") if agent.get("role") else ""
    return j.from_string(body).render(ctx)


def neighbours(defn):
    """{frozenset({a, b})}: agents a and b are neighbours when an outcome of a node of one adds a label that a node
    of the other triggers on (#590: they may mention each other). Generated mention nodes do not count."""
    nodes = [n for n in (defn["workflow"].get("nodes") or {}).values() if not n.get("generated")]
    woken = {}
    for n in nodes:
        for t in n.get("triggers") or []:
            if isinstance(t, dict) and "label" in t:
                woken.setdefault(t["label"], set()).add(n.get("agent"))
    pairs = set()
    for n in nodes:
        for oc in (n.get("outcomes") or {}).values():
            for e in (oc or {}).get("effects") or []:
                for b in woken.get(e.get("label"), ()):
                    if b and b != n.get("agent"):
                        pairs.add(frozenset((n.get("agent"), b)))
    return pairs


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
    names = ["factory-step", *(n for n in role_skills(defn, role) if n != "factory-step")]  # the engine's, always
    return "\n".join(f"- {name}: `~/.agents/skills/{name}/SKILL.md`" for name in names)


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


def _notify_errors(config):
    import notify
    return notify.config_errors(config) if isinstance(config, dict) else []


def _notify_effect_errors(where, outcome, spec, config):
    """A `notify` effect is `message` or `{message: message, people: [logins]}`, and needs `notify` config."""
    errs = []
    if not config:
        errs.append(f"missing-notify: {where}.{outcome}: a `notify` effect needs `notify` config in factory.yaml")
    body = spec if isinstance(spec, dict) else {"message": spec}
    people = body.get("people")
    if body.get("message") != "message" or set(body) - {"message", "people"} \
            or not (people is None or isinstance(people, list) and all(isinstance(p, str) for p in people)):
        errs.append(f"bad-effect: {where}.{outcome}: notify must be `message` or `{{message: message, people: [logins]}}`")
    elif config:
        known = (config or {}).get("people") or {}
        errs += [f"unknown-person: {where}.{outcome}: {p} is not in notify.people" for p in people or [] if p not in known]
    return errs


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
    if not f.get("owner"):
        errs.append("missing-owner: factory.yaml: `owner` (the GitHub login a person-wait is assigned to)")
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
        if spec.get("mentionable") and (spec.get("run") or spec.get("harness") in NO_ROLE):
            errs.append(f"mentionable-needs-role: agents.{a}: only an agent with a role can reply")
        if spec.get("harness") not in NO_ROLE and not spec.get("run") and spec.get("role") not in roles:
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
    if w.get("blockers", "start") not in ("start", "merge"):
        errs.append(f"bad-blockers: workflow.yaml: `blockers` is `start` or `merge`, not {w['blockers']!r}")
    default_env = (w.get("settings") or {}).get("environment")
    code = code_repos(defn)
    if len(code) > 1 and any(per_repo_node(n) for n in nodes) \
            and ((f.get("defaults") or {}).get("settings") or {}).get("write_mode", "per-repo") == "per-repo":
        errs += [f"per-repo-env: {r}: a per-repository node needs an environment where it is `main`"
                 for r in code if r not in per_repo_envs(defn)]
    for name, n in nodes.items():
        where = f"nodes.{name}"
        if n.get("agent") not in agents:
            errs.append(f"unknown-agent: {where}: {n.get('agent')}")
            continue
        human = (agents[n["agent"]] or {}).get("harness") == "human"
        env = n.get("environment") or default_env
        if n.get("generated") is not True and name == ROUTER or (name.startswith("reply-") and not n.get("generated")):
            errs.append(f"reserved-node: {where}: `{ROUTER}` and `reply-<agent>` are generated from `mentionable`")
        if (agents[n["agent"]] or {}).get("run") not in (None, "decide"):
            errs.append(f"unknown-command: agents.{n['agent']}: run {agents[n['agent']]['run']}")
        if not human and env not in envs:
            errs.append(f"unknown-environment: {where}: {env}")
        if n.get("model") is not None and n["model"] not in models:
            errs.append(f"unknown-model: {where}: {n['model']}")
        if not n.get("triggers"):
            errs.append(f"no-trigger: {where}")
        if "prompt" in n:
            errs.append(f"node-prompt: {where}: instructions belong in roles/<role>/role.md; pass what differs as settings")
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
            errs += [f"unknown-guard: {where}.{o}: {k}" for k in (oc.get("if") or {}) if k not in GUARDS]
            errs += [f"unknown-effect: {where}.{o}: {next(k for k in e if k != 'on')}" for e in oc.get("effects") or []
                     if next((k for k in e if k != "on"), None) not in EFFECTS]
            for effect in oc.get("effects") or []:
                if isinstance(effect, dict) and ("assign" in effect or "unassign" in effect):
                    key = "assign" if "assign" in effect else "unassign"
                    value = effect[key]
                    valid = isinstance(value, str) or (isinstance(value, list) and all(isinstance(v, str) for v in value))
                    if key == "unassign":
                        valid = valid or value == "all"
                    if not valid:
                        errs.append(f"bad-effect: {where}.{o}: {key} must be a login or a list of logins")
            for effect in oc.get("effects") or []:
                if isinstance(effect, dict) and "notify" in effect:
                    errs += _notify_effect_errors(where, o, effect["notify"], f.get("notify"))
            prefix = (w.get("stages") or {}).get("label_prefix", "factory:")
            by_target = {}
            for effect in oc.get("effects") or []:
                if not isinstance(effect, dict):
                    continue
                label = effect.get("label") or effect.get("unlabel")
                if not isinstance(label, str) or ":" not in label or label.startswith(prefix):
                    continue
                kind = "label" if "label" in effect else "unlabel" if "unlabel" in effect else None
                if kind:
                    target = effect.get("on") or "issue"
                    group = label.partition(":")[0]
                    by_target.setdefault((target, group), set()).add(kind)
            for (target, group), kinds in by_target.items():
                if kinds == {"label", "unlabel"}:
                    errs.append(f"label-group: {where}.{o}: cannot unlabel and label {group} on {target}; "
                                "keep only the label")
        if not human and n.get("enabled") is not False and env in envs:
            try:
                render(defn, name)
            except __import__("jinja2").TemplateError as e:
                errs.append(f"render: {where}: {e}")
    return errs + _mcp_errors(defn) + _notify_errors(f.get("notify"))


def warnings(defn):
    """A label an outcome adds that no node triggers on (a person may add it, so it is not an error)."""
    nodes = (defn["workflow"].get("nodes") or {}).values()
    triggered = {t["label"] for n in nodes for t in n.get("triggers") or [] if isinstance(t, dict) and "label" in t}
    added = {e["label"] for n in nodes for oc in (n.get("outcomes") or {}).values()
             for e in (oc or {}).get("effects") or [] if isinstance(e, dict) and "label" in e}
    prefix = (defn["workflow"].get("stages") or {}).get("label_prefix", "factory:")
    # A routing label (the stage prefix) should wake a node; `risk:low` and the like are states, read by guards.
    return [f"unwatched-label: {label}: no node triggers on it" for label in sorted(added)
            if label.startswith(prefix) and label not in triggered]


# --- Compile into the shapes the engine reads (#669, #677) ---------------------------------------
# Each node compiles to one engine node of the same name: its agent's role and model alias, its triggers as
# wakes, its outcomes' static effects; `if: {stage}` becomes `from`, `if: {ci}` becomes `ci`, the rest stays
# an `if` the gate checks. Labels are commands named after themselves; one an effect adds `on: pr` is a PR label.
TRIGGER_WAKES = {("pr", "reviewed"): "pr reviewed", ("pr", "pushed"): "pr pushed", ("pr", "opened"): "pr opened",
                 ("pr", "merged"): "state pr-merged", ("ticket", "closed"): "ticket closed", ("pr", "commented"): "pr commented",
                 ("ticket", "commented"): "ticket commented"}
EFFECTS = ("stage", "label", "unlabel", "comment", "review", "merge", "update-branch", "create-issues",
           "request-reviewers", "park-behind-main-red", "assign", "unassign", "notify")


def _name(label, prefix):
    """The command a label is: the label without the stage prefix (`factory:fix-medium` -> `fix-medium`)."""
    return label[len(prefix):] if label.startswith(prefix) else label


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
        return (url.stdout.strip().removesuffix(".git") or None) if url.returncode == 0 else None
    except OSError:
        return None


def _labels_of(n):
    found = [t["label"] for t in n.get("triggers") or [] if isinstance(t, dict) and "label" in t]
    for oc in (n.get("outcomes") or {}).values():
        found += [e.get("label") or e.get("unlabel") for e in (oc or {}).get("effects") or []
                  if "label" in e or "unlabel" in e]
    return found


def _pr_woken(n):
    return any(isinstance(t, dict) and next(iter(t)) in ("pr", "ci") for t in n.get("triggers") or [])


def _used_stages(w):
    """Every stage name the workflow's nodes and `stages:` mention."""
    stages, used = w.get("stages") or {}, set()
    for n in (w.get("nodes") or {}).values():
        used |= {t["stage"] for t in n.get("triggers") or [] if isinstance(t, dict) and "stage" in t}
        if (n.get("when") or {}).get("stage"):
            st = n["when"]["stage"]
            used |= set(st if isinstance(st, list) else [st])
        for oc in (n.get("outcomes") or {}).values():
            used |= {e["stage"] for e in (oc or {}).get("effects") or [] if "stage" in e}
            st = ((oc or {}).get("if") or {}).get("stage")
            used |= set(st if isinstance(st, list) else [st] if st and not str(st).startswith("!") else [])
    used |= {s for k in ("final", "waiting") for s in stages.get(k) or []}  # declared stages count too
    used.discard("none")
    return used


def to_project(defn):
    """(project descriptor, format-2 workflow) of the definition, for the project loaders (`project.load`,
    `workflow.load`) and `config`. The definition must have no `errors`."""
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
        help_ = environment_secrets(defn, en)
        environments[en] = {"repositories": [names[r["repo"]] for r in repos], "secrets": list(help_),
                            "secret_help": help_,
                            "domains": list(e.get("domains") or []), "vm": e.get("vm", "default"), "startup": STARTUP}

    # Stages: every name the nodes use, plus the engine's PR facts.
    used = _used_stages(w)
    kind = {s: k for k in ("final", "waiting") for s in stages.get(k) or []}
    states = {s: {"kind": kind.get(s, "active"), "store": {"label": f"{prefix}{s}"}} for s in sorted(used)}
    states["pr-merged"] = {"kind": "final", "store": {"pr": "merged"}}

    # Commands: every label a trigger or an effect names; on the PR when an effect puts it there (`on: pr`).
    # CI puts its result labels on the PR itself (quality-gates `ci-label`), so they are PR labels too (#790).
    on_pr = {f"{prefix}ci-green", f"{prefix}ci-red"}
    for n in nodes.values():
        for oc in (n.get("outcomes") or {}).values():
            on_pr |= {e.get("label") or e.get("unlabel") for e in (oc or {}).get("effects") or []
                      if ("label" in e or "unlabel" in e) and e.get("on") == "pr"}
    commands = {}
    for label in sorted({x for n in nodes.values() for x in _labels_of(n)}):
        commands[_name(label, prefix)] = {"store": {("pr_label" if label in on_pr else "label"): label}}

    def role_of(agent_name):
        a = agents[agent_name]
        return {"script": "script", "human": "human"}.get(a.get("harness")) or a.get("role") or agent_name

    roles = {}
    for name, n in nodes.items():
        a, role = agents[n["agent"]], role_of(n["agent"])
        if role not in ("script", "human"):
            roles.setdefault(role, {"tier": n.get("model") or a.get("model") or defaults.get("model")})

    wf_nodes, schedules = {}, {}
    for name, n in nodes.items():
        wf_nodes[name] = _node(name, n, agents, role_of, prefix, schedules, envs, default_env, sorted(used),
                               {c for c, v in commands.items() if "pr_label" in v["store"]})
        connectors = mcp_connectors(mcp(defn, name))
        if connectors:  # Air's `mcpConnectors`; the custom servers go to `.mcp.json` at setup (team_setup)
            wf_nodes[name]["mcp"] = connectors

    merge_settings = next(((v.get("settings") or {}) for v in nodes.values()
                           if "max_risk" in (v.get("settings") or {})), {})
    fix_rounds = next((v["settings"]["fix_rounds"] for v in nodes.values() if "fix_rounds" in (v.get("settings") or {})), None)
    team = team_url(defn["root"])
    air = (f.get("targets") or {}).get("air") or {}
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
        **({"write_mode": dset["write_mode"]} if dset.get("write_mode") else {}),
        "branch_pattern": dset.get("branch_pattern"),
        "commit_prefix": dset.get("commit_prefix"),
        "targets": {"air": {"bot_login": dset.get("bot_login"), "models": {a: _model_id(models, a) for a in models},
                           "mcp_connectors": list(air.get("mcp_connectors") or AIR_CONNECTORS)}},
        "roles": roles,
        "limits": {**(wset.get("limits") or {}), **({"fix_rounds": fix_rounds} if fix_rounds else {})},
        "gates": {"auto_merge": {"enabled": bool(merge_settings.get("auto_merge", True)),
                                 "max_risk": merge_settings.get("max_risk", "high")},
                  "confirm_external_writes": dset.get("confirm_external_writes", True),
                  "confirm_external_writes_scope": dset.get("confirm_external_writes_scope") or []},
        "reviews": {"trusted_bots": []},
        **({"owner": f["owner"]} if f.get("owner") else {}),
    }
    return descriptor, {"format": 2, "adapters": {"ticket": (f.get("tracker") or {}).get("adapter", "github-issues"),
                                                  "pr": "github"},
                        **({"blockers": w["blockers"]} if w.get("blockers") else {}),
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
        return f"command {_name(v, prefix)}"
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
    key = next(k for k in e if k != "on")
    if key == "stage":
        return {"state": e["stage"]}
    if key in ("label", "unlabel"):
        return {("command" if key == "label" else "uncommand"): _name(e[key], prefix)}
    return dict(e)  # `comment` with `on: mention` keeps it: the gate posts where the question was


def _node(name, n, agents, role_of, prefix, schedules, envs, default_env, all_stages, pr_cmds):
    env_name = n.get("environment") or default_env
    a = agents[n["agent"]]
    role = role_of(n["agent"])
    when = dict(n.get("when") or {})
    when = ({"state" if k == "stage" else k: v for k, v in when.items()} if when else {"always": True})
    if role == "human":
        when = {"actor": "human"}
    pr_woken = _pr_woken(n) or any(isinstance(t, dict) and "label" in t and _name(t["label"], prefix) in pr_cmds
                                   for t in n.get("triggers") or [])
    out = {"env": env_name} if role != "human" else {}
    out["wakes_on"] = [_wake(t, name, prefix, schedules, envs.get(env_name) or {}) for t in n.get("triggers") or []]
    out["when"] = when
    out["role"] = role
    if role == "script":
        out["run"] = n.get("run") or a.get("run")
    if role not in ("script", "human"):
        out["tier"] = n.get("model") or a.get("model")
    if role != "human":
        out["output"] = n.get("output", "read-only")
    if pr_woken and "ci" not in when:
        out["ci-independent"] = True
    if n.get("triggers") and all(isinstance(t, dict) and "schedule" in t for t in n["triggers"]):
        out["routine"] = True  # outside any ticket: `factory begin` needs no trigger payload
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
            res["if"] = guard  # label, pr, review, blockers, children: the gate checks them (gate.guard_holds)
        for k in ("else", "enabled", "to"):
            if k in oc:
                res[k] = oc[k]
        if "effects" in oc:
            res["effects"] = [_effect(e, prefix) for e in oc["effects"]]
        outcomes[o] = res
    out["outcomes"] = outcomes
    return out


# --- The shapes the gate reads, straight from the definition -------------------------------------
# `nodes(defn)` is engine/nodes.yaml's shape: triggers as event strings, `when.stage`, stage and label effects.
EVENTS = {("pr", "reviewed"): "pull_request_review.submitted", ("pr", "pushed"): "pull_request.synchronize",
          ("pr", "opened"): "pull_request.opened", ("pr", "merged"): "pull_request.merged",
          ("pr", "commented"): "issue_comment.created", ("ticket", "closed"): "issues.closed",
          ("ticket", "commented"): "issues.comment"}


def _trigger(t, name, prefix, on_pr, schedules, env):
    """A trigger of a definition node as the gate's event string; a schedule is recorded in `schedules`."""
    if t == "manual":
        return "manual"
    (k, v), = t.items()
    if k == "stage":
        return f"issues.labeled {prefix}{v}"
    if k == "label":
        return f"{'pull_request' if v in on_pr else 'issues'}.labeled {v}"
    if k == "schedule":
        key = name if name not in schedules or schedules[name]["cron"] == v else f"{name}-{len(schedules)}"
        schedules[key] = {"cron": v}
        return f"schedule {key}"
    if k == "webhook":
        return f"webhook {v}"
    if k == "ci":
        return f"workflow_run {(env.get('ci') or {}).get('workflow', 'quality-gates')}"
    return EVENTS[(k, v)]


def _outcome(oc, on_pr, all_stages):
    oc = dict(oc or {})
    guard = dict(oc.pop("if", None) or {})
    out = {}
    st = guard.pop("stage", None)
    if st is not None:
        out["from"] = [s for s in all_stages if s != st[1:]] if isinstance(st, str) and st.startswith("!") \
            else st if isinstance(st, list) else [st]
    if "ci" in guard:
        ci = guard.pop("ci")
        out["ci"] = ci if isinstance(ci, list) else [ci]
    if guard:
        out["if"] = guard  # label, pr, review, blockers, children: the gate checks them (gate.guard_holds)
    for k in ("else", "enabled", "to"):
        if k in oc:
            out[k] = oc[k]
    if "effects" in oc:
        out["effects"] = [_label_effect(e, on_pr) for e in oc["effects"]]
    return out


def _label_effect(e, on_pr):
    """A label is on the PR when any effect puts it there; `comment` with `on: mention` stays as written."""
    key = next(k for k in e if k != "on")
    if key in ("label", "unlabel"):
        return {key: e[key], **({"on": "pr"} if e[key] in on_pr else {})}
    return dict(e)


def nodes(defn):
    """The gate's node graph of the definition (nodes, schedules, webhooks, adapters, environments). The definition
    must have no `errors`."""
    f, w = defn["factory"], defn["workflow"]
    agents, envs, defaults = f.get("agents") or {}, f.get("environments") or {}, f.get("defaults") or {}
    wset = w.get("settings") or {}
    prefix = (w.get("stages") or {}).get("label_prefix", "factory:")
    default_env, all_stages = wset.get("environment"), sorted(_used_stages(w))
    primary = next(r["repo"] for r in envs[default_env]["repositories"] if r.get("main"))
    on_pr = {f"{prefix}ci-green", f"{prefix}ci-red"}
    for n in (w.get("nodes") or {}).values():
        for oc in (n.get("outcomes") or {}).values():
            on_pr |= {e.get("label") or e.get("unlabel") for e in (oc or {}).get("effects") or []
                      if ("label" in e or "unlabel" in e) and e.get("on") == "pr"}
    environments = {}
    for en, e in envs.items():
        repos = sorted(e.get("repositories") or [], key=lambda r: not r.get("main"))
        secrets = sorted(k for k, v in {**(defaults.get("env") or {}), **(e.get("env") or {})}.items()
                         if (v or {}).get("secret") and k != "GH_TOKEN")
        environments[en] = {"repository": "primary" if repos and repos[0]["repo"] == primary else repos[0]["repo"].split("/")[-1],
                            "secrets": secrets, "domains": list(e.get("domains") or []), "vm": e.get("vm", "default"),
                            "startup": STARTUP}
    schedules, out = {}, {}
    for name, n in (w.get("nodes") or {}).items():
        a = agents[n["agent"]]
        role = {"script": "script", "human": "human"}.get(a.get("harness")) or a.get("role") or n["agent"]
        env_name = n.get("environment") or default_env
        when = dict(n.get("when") or {}) or {"always": True}
        triggers = [_trigger(t, name, prefix, on_pr, schedules, envs.get(env_name) or {}) for t in n.get("triggers") or []]
        node = {"env": env_name} if role != "human" else {}
        node.update(triggers=triggers, when={"actor": "human"} if role == "human" else when, role=role)
        if role == "script":
            node["run"] = n.get("run") or a.get("run")
        if role not in ("script", "human"):
            node["tier"] = n.get("model") or a.get("model")
        if role != "human":
            node["output"] = n.get("output", "read-only")
        pr_woken = any(isinstance(t, dict) and (next(iter(t)) in ("pr", "ci") or ("label" in t and t["label"] in on_pr))
                       for t in n.get("triggers") or [])
        if pr_woken and "ci" not in node["when"]:
            node["ci-independent"] = True
        if n.get("triggers") and all(isinstance(t, dict) and "schedule" in t for t in n["triggers"]):
            node["routine"] = True  # outside any ticket: `factory begin` needs no trigger payload
        if n.get("enabled") is False:
            node["enabled"] = False
        node["outcomes"] = {o: _outcome(oc, on_pr, all_stages) for o, oc in (n.get("outcomes") or {}).items()}
        connectors = mcp_connectors(mcp(defn, name))
        if connectors:  # Air's `mcpConnectors`; the custom servers go to `.mcp.json` at setup (team_setup)
            node["mcp"] = connectors
        out[name] = node
    return {"adapters": {"ticket": (f.get("tracker") or {}).get("adapter", "github-issues"), "pr": "github"},
            "environments": environments, "schedules": schedules,
            "webhooks": {k: {"description": (v or {}).get("description", k)} for k, v in (f.get("webhooks") or {}).items()},
            "nodes": out}


def config(defn):
    """The engine config of the definition, to merge over engine/defaults.yaml: its project descriptor, with the
    tracker's `stages`, `stage_kinds` and `commands` that the gate reads."""
    descriptor = to_project(defn)[0]
    states = {name: s for name, s in descriptor["states"].items() if "label" in s["store"]}
    return {**descriptor, "tracker": {**descriptor["tracker"],
                                      "stages": {name: s["store"]["label"] for name, s in states.items()},
                                      "stage_kinds": {name: s["kind"] for name, s in states.items()},
                                      "commands": {name: c["store"] for name, c in descriptor["commands"].items()}}}

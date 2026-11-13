"""The team definition in the layout of ADR-0021: `factory.yaml` (what the factory has), `workflow.yaml` (how it
works) and `roles/<role>/role.md` (instructions).

`load(root)` reads the files; `errors(defn)` lists every problem; `warnings(defn)` lists what is legal but likely
wrong; `settings(defn, node)` is a node's merged settings; `render(defn, node)` renders its role body.
Nothing else in the engine reads this layout yet (#669 compiles it into the config and the node graph).
"""
import re
from pathlib import Path

import yaml

import plugins  # ADR-0026: a plugin's code is imported when the core calls it
import scopes

FACTORY = "factory.yaml"
WORKFLOW = "workflow.yaml"
ROLES = "roles"
NO_ROLE = ("script", "human")  # harnesses that run no role
GUARDS = ("stage", "label", "pr", "ci", "review", "blockers", "children", "ticket")  # the facts a guard reads (gate.GUARD_KEYS)
# ADR-0024: keys a node's `writes` replaced; `factory validate` names them (`removed-key`)
REMOVED_REPOSITORY_KEYS = ("main", "token_env")  # `write` names the environment's writable repository; `token` the secret (ADR-0027)
EXTERNAL_REVIEW = ("external_review_hours", "external_review_bot", "external_review_label")  # reconcile (#1263)
DEPRECATED_NODE_KEYS = ("listens", "writes")  # ADR-0027: a node declares `environments`, a repository its `write`
DEFAULT_ADAPTER = {"tracker": "github-issues", "vcs": "github"}
TIER_SUFFIXES = ("-small", "-medium", "-large", "-low", "-high")  # a tiered node: `implement-medium`, `review-high`
STARTUP = ".air/cloud/startup.sh"  # Air runs it from the checkout; it runs `factory setup` (ADR-0021)


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
    for add in plugins.registry(factory).hook("definition.load"):
        add(defn)  # comments (#590): the generated router and reply nodes of `mentionable` agents
    return defn


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


def _parts(defn, name, env_name=None):
    """(node, agent, role, environment) of node `name` in environment `env_name` (default: its first); missing parts
    are `{}`."""
    f, w = defn["factory"], defn["workflow"]
    node = (w.get("nodes") or {}).get(name) or {}
    agent = (f.get("agents") or {}).get(node.get("agent")) or {}
    role = (f.get("roles") or {}).get(agent.get("role")) or {}
    if env_name is None:
        env_name = _first_env(node, w.get("settings")) or (w.get("settings") or {}).get("environment")
    env = (f.get("environments") or {}).get(env_name) or {}
    return node, agent, role, env


def node_writes(defn, name):
    """Full names of the repositories node `name` writes, in environment order; `writes: all` is every repository of
    its environment. `[]` when the node writes nothing."""
    node, _, _, env = _parts(defn, name)
    writes = node.get("writes")
    listed = [full_name(r["repo"]) for r in env.get("repositories") or []]
    if writes == "all":
        return listed
    if not isinstance(writes, list):  # malformed: `factory validate` reports `bad-writes`
        return []
    wanted = {full_name(r) for r in writes if isinstance(r, str)}
    return [r for r in listed if r in wanted]


def tracker_repositories(defn):
    """Full names of the ticket repositories, in order; the first is the tracker. `tracker.repositories`, the
    repositories linked by `tracker.projects` (YouTrack, #981), or the single `tracker.repository`."""
    t = defn["factory"].get("tracker") or {}
    listed = t.get("repositories")
    if listed is None and isinstance(t.get("projects"), dict):
        listed = list(t["projects"].values())
    if listed is None:
        listed = [t["repository"]] if t.get("repository") else []
    return [full_name(r) for r in listed if isinstance(r, str)] if isinstance(listed, list) else []


def _has_event_trigger(node):
    return any(isinstance(t, dict) and not {"schedule", "webhook"} & t.keys() for t in node.get("triggers") or [])


def node_listens(defn, name):
    """Full names of the ticket repositories node `name` listens to: `tracker` is the first one, `tickets` (the
    default) all of them. A node with no event trigger (schedule, webhook, manual) listens to nothing: `[]`."""
    node = _parts(defn, name)[0]
    if not _has_event_trigger(node):
        return []
    tickets = tracker_repositories(defn)
    listens = node.get("listens", "tickets")
    if listens == "tracker":
        return tickets[:1]
    if listens == "tickets":
        return tickets
    return [full_name(r) for r in listens if isinstance(r, str)] if isinstance(listens, list) else []


def _primary(defn, env):
    """The repository the node output applies to in environment `env` (ADR-0024: an environment has no `main`): the
    tracker repository, else its first repository."""
    repos = (env or {}).get("repositories") or []
    return next(iter(tracker_repositories(defn)), None) or repos[0]["repo"]


# --- ADR-0027: each repository of an environment names its tracker, vcs and write ------------------------------
def _as_block(value):
    """An adapter block from config value `value`: `"space"` is `{adapter: space}`; anything else not a dict is None."""
    if isinstance(value, str):
        return None if value == "none" else {"adapter": value}
    return dict(value) if isinstance(value, dict) else None


def contract(defn, kind, block):
    """The `tracker-contract` or `vcs-contract` (ports/place.py) a selected plugin provides for `block`; None when none."""
    name = (block or {}).get("adapter") or DEFAULT_ADAPTER[kind]
    return plugins.registry(defn["factory"]).impl(f"{kind}-contract", name)


def _layered(defn, kind, env, entry):
    """Repository `entry`'s `tracker` or `vcs` block: the top-level block, then the environment's, then its own. A
    narrower block with another adapter keeps only the factory's keys (ADR-0026); the contract's `LOCAL_KEYS` (a
    repository's place: github `repository`, a `scope`) never come from the top level. `tracker: none`: no tickets."""
    f = defn["factory"]
    if kind == "tracker" and entry.get("tracker") in ("none", False):
        return None
    top = f.get("tracker") if kind == "tracker" else (f.get("vcs") or f.get("code_host"))
    layers = [_as_block(top), _as_block(env.get(kind)), _as_block(entry.get(kind))]
    adapter = next((b["adapter"] for b in reversed(layers) if b and b.get("adapter")), DEFAULT_ADAPTER[kind])
    local = set(getattr(contract(defn, kind, {"adapter": adapter}), "LOCAL_KEYS", ()))
    out = {}
    for i, b in enumerate(layers):
        if not b:
            continue
        b = {k: v for k, v in b.items() if not (i == 0 and k in local)}
        same = (b.get("adapter") or adapter) == (out.get("adapter") or adapter)
        out = {**{k: v for k, v in out.items() if same or k not in scopes.SYSTEM_KEYS}, **b}
    return {**out, "adapter": adapter}


def repositories(defn, env_name):
    """The repositories of environment `env_name` in order, each `{repo, write, token, tracker, vcs}`: its full name, whether
    runs may push to it, the secret of its `token` (or None), and its layered blocks (`tracker` None: context only, no tickets)."""
    env = ((defn["factory"].get("environments") or {}).get(env_name)) or {}
    return [{"repo": full_name(r["repo"]), "write": r.get("write") is True, "token": r.get("token"),
             "tracker": _layered(defn, "tracker", env, r), "vcs": _layered(defn, "vcs", env, r)}
            for r in env.get("repositories") or [] if isinstance(r, dict) and r.get("repo")]


env_repositories = repositories  # the name `to_project` can still reach under its local `repositories` list


def node_environments(defn, name):
    """The environments node `name` runs in: its `environments` list, else its `environment`, else the workflow's
    `settings.environments` list, else its `settings.environment`."""
    w = defn["workflow"]
    node = (w.get("nodes") or {}).get(name) or {}
    wset = w.get("settings") or {}
    for listed in (node.get("environments"), None if node.get("environment") else wset.get("environments")):
        if isinstance(listed, list):
            return [e for e in listed if isinstance(e, str)]
    one = node.get("environment") or wset.get("environment")
    return [one] if one else []


def copies(defn, name):
    """The deploy copies of node `name` (ADR-0027): per environment, one per repository it listens to (the writable
    ones, else every repository with a tracker); a node with no event trigger has one per environment. Each copy is
    `{env, repo, main, place, write, tracker, vcs, additional}`: `main` is the checkout whose events it hears (the
    tracker's `home`, else the repository; for a schedule the writable repository, else the first), `write` whether it
    may push there, `additional` the environment's other repositories."""
    node = ((defn["workflow"].get("nodes") or {}).get(name)) or {}
    event, out = _has_event_trigger(node), []
    for env in node_environments(defn, name):
        repos = repositories(defn, env)
        if not repos:
            continue
        writable = [r for r in repos if r["write"]]
        for r in (writable or [r for r in repos if r["tracker"]]) if event else [None]:
            c = contract(defn, "tracker", r["tracker"]) if r and r["tracker"] else None
            main = (writable or repos)[0]["repo"] if r is None else (c and c.home(r["tracker"], r["repo"])) or r["repo"]
            out.append({"env": env, "repo": r and r["repo"], "main": main,
                        "place": c.place(r["tracker"], r["repo"]) if c else None,
                        "write": any(w["repo"] == main for w in writable),
                        "tracker": r and r["tracker"], "vcs": r and r["vcs"],
                        "additional": [x["repo"] for x in repos if x["repo"] != main]})
    return out


def event_repositories(defn):
    """The repositories whose events wake copies (ADR-0027), in order: each tracker's `home` (its GitHub issues) and
    each writable repository's vcs `home` (its PRs). Their labels are planned; a tracker with no home (YouTrack)
    plans its own tags."""
    out = []
    for env in (defn["factory"].get("environments") or {}):
        for r in repositories(defn, env):
            for kind, on in (("tracker", r["tracker"] is not None), ("vcs", r["write"])):
                c = contract(defn, kind, r[kind]) if on else None
                if c and (home := c.home(r[kind], r["repo"])):
                    out.append(home)
    return list(dict.fromkeys(out))


def default_environment(defn):
    """The workflow's `settings.environment`, else (ADR-0027) the first environment a node names, else the first."""
    w = defn["workflow"]
    wset = w.get("settings") or {}
    listed = wset.get("environments") if isinstance(wset.get("environments"), list) else []
    named = wset.get("environment") or (listed[0] if listed else None) or next(
        (e for n in (w.get("nodes") or {}).values() if (e := _first_env(n or {}))), None)
    return named or next(iter(defn["factory"].get("environments") or {}), None)


def _first_env(node, wset=None):
    """A node's first environment: of its `environments` list (ADR-0027), else its `environment`, else the first of
    the workflow's `settings.environments`."""
    listed = node.get("environments")
    default = (wset or {}).get("environments") if not node.get("environment") else None
    return (listed[0] if isinstance(listed, list) and listed else None) or node.get("environment") \
        or (default[0] if isinstance(default, list) and default else None)


def adopted(defn):
    """Whether the definition uses the ADR-0027 model (a repository's `write`, `tracker` or `vcs`, a node's
    `environments`): only then do the repository checks run, as errors."""
    f, w = defn["factory"], defn["workflow"]
    return any(isinstance(r, dict) and ({"write", "tracker", "vcs"} & r.keys())
               for e in (f.get("environments") or {}).values() for r in (e or {}).get("repositories") or []) \
        or any("environments" in (n or {}) for n in (w.get("nodes") or {}).values()) \
        or "environments" in (w.get("settings") or {})


def label_prefix(defn):
    """The stage label prefix (`tracker.label_prefix`, default `factory:`) of the primary repository of the default
    environment: the top-level tracker block, then the environment's, then the repository's own (ADR-0027). While
    `workflow.yaml` still has `stages.label_prefix`, it is the fallback (`warnings` reports the move)."""
    f = defn["factory"]
    fallback = (defn["workflow"].get("stages") or {}).get("label_prefix") or "factory:"
    envs = f.get("environments") or {}
    env = envs.get(default_environment(defn)) or {}
    try:
        primary = full_name(_primary(defn, env))
    except (IndexError, KeyError, TypeError):
        primary = None
    entry = next((r for r in env.get("repositories") or [] if isinstance(r, dict) and r.get("repo")
                  and full_name(r["repo"]) == primary), None)
    chosen = None  # `label_prefix` is no system key: it carries over whatever the layers' adapters are
    for layer in (f.get("tracker"), env.get("tracker"), (entry or {}).get("tracker")):
        chosen = (_as_block(layer) or {}).get("label_prefix", chosen)
    return chosen if isinstance(chosen, str) and chosen else fallback  # a bad value is `errors`'s to report


def _repository_errors(defn):
    """The ADR-0027 checks: `write` is a boolean, each block's adapter is provided and valid, no two repositories
    share a tracker place, and no node hears one place from two environments."""
    f, errs, places = defn["factory"], [], {}
    # a repository's own tracker block (its issues, or a project and `scope`) says where its tickets are; these
    # repeat it per repository (YouTrack per-repository stage tags, a project per repository)
    blocks = [("tracker", f.get("tracker"))] + [(f"environments.{e}.tracker", (s or {}).get("tracker"))
                                                for e, s in (f.get("environments") or {}).items()]
    blocks += [(f"environments.{e}: {r.get('repo')}: tracker", r.get("tracker")) for e, s in (f.get("environments") or {}).items()
               for r in (s or {}).get("repositories") or [] if isinstance(r, dict)]
    errs += [f"removed-key: {where}.{k} (ADR-0027: each repository's tracker block and `scope` say where its tickets are)"
             for where, b in blocks if isinstance(b, dict) for k in ("repository_field", "projects", "repositories") if k in b]
    for e, spec in (f.get("environments") or {}).items():
        for r in (spec or {}).get("repositories") or []:
            if isinstance(r, dict) and "write" in r and not isinstance(r["write"], bool):
                errs.append(f"bad-write: environments.{e}: {r.get('repo')}: `write` is true or false")
            if isinstance(r, dict) and "token" in r and not (isinstance(r["token"], str) and r["token"]):
                errs.append(f"bad-token: environments.{e}: {r.get('repo')}: `token` is the name of a secret")
        for r in repositories(defn, e):
            where = f"environments.{e}: {r['repo']}"
            for kind in ("tracker", "vcs"):
                block = r[kind]
                if block is None:
                    continue
                c = contract(defn, kind, block)
                if c is None:
                    errs.append(f"unknown-{kind}: {where}: {block['adapter']}: no selected plugin provides this {kind}")
                    continue
                errs += c.check(block, where) if kind == "tracker" else c.check(block, r["repo"], where)
                if kind == "tracker":
                    places.setdefault(c.place(block, r["repo"]), set()).add(r["repo"])
    errs += [f"place-in-two-repositories: {p}: holds the tickets of {', '.join(sorted(rs))}; give each repository "
             f"its own place (its own issues, or a `scope`)" for p, rs in places.items() if len(rs) > 1]
    for name in (defn["workflow"].get("nodes") or {}):
        heard = {}
        for c in copies(defn, name):
            if c["place"]:
                heard.setdefault(c["place"], set()).add(c["env"])
        errs += [f"node-hears-twice: nodes.{name}: environments {', '.join(sorted(es))} both listen to {p}: "
                 f"one ticket would start two runs" for p, es in heard.items() if len(es) > 1]
    return list(dict.fromkeys(errs))


def _repository_warnings(defn):
    """Deprecated keys (ADR-0027) and environments with several writable repositories (Air refuses them). The
    repository checks (`_repository_errors`) run only on a definition that `adopted` the model, so validating an older
    one imports no plugin code."""
    f, w = defn["factory"], defn["workflow"]
    out = [f"deprecated-key: nodes.{n}: `{k}` (ADR-0027: a node declares `environments`, a repository its `write`)"
           for n, spec in (w.get("nodes") or {}).items() for k in DEPRECATED_NODE_KEYS if k in (spec or {})]
    out += [f"deprecated-key: agents.{a}: `access` (ADR-0027: a repository's `write` decides what a run may push)"
            for a, spec in (f.get("agents") or {}).items() if "access" in (spec or {})]
    out += [f"deprecated-key: tracker.{k} (ADR-0027: each repository names its tracker)"
            for k in ("repositories", "projects") if k in (f.get("tracker") or {}) and not adopted(defn)]
    for e, spec in (f.get("environments") or {}).items():  # raw flags: no plugin code is imported here
        writable = [str(r.get("repo")) for r in (spec or {}).get("repositories") or [] if isinstance(r, dict) and r.get("write") is True]
        if len(writable) > 1:
            out.append(f"several-writable: environments.{e}: {', '.join(writable)}: the air surface allows one "
                       f"writable repository per environment")
    return out


def environment_variables(defn, env_name):
    """{name: {"secret", "required": bool, "description": str}} of the variables `env_name` declares: those of the defaults and the
    environment, and those of the role, agent and node of every node that runs in it. GH_TOKEN is Air's own and is
    left out. A later layer overrides `secret`; the description falls back to an earlier one."""
    f, w = defn["factory"], defn["workflow"]
    layers = [(f.get("defaults") or {}), ((f.get("environments") or {}).get(env_name) or {})]
    for name in w.get("nodes") or {}:
        node, agent, role, _ = _parts(defn, name)
        if env_name in node_environments(defn, name):
            layers += [role, agent, node]
    out = {}
    for layer in layers:
        for k, v in (layer.get("env") or {}).items():
            if k != "GH_TOKEN":
                out[k] = {"secret": bool((v or {}).get("secret")), "required": (v or {}).get("required", True) is not False,
                          "description": (v or {}).get("description") or (out.get(k) or {}).get("description") or ""}
    return dict(sorted(out.items()))


def environment_secrets(defn, env_name):
    """{name: description} of the secrets `env_name` needs (its `environment_variables` marked `secret`)."""
    return {k: v["description"] for k, v in environment_variables(defn, env_name).items() if v["secret"]}


def settings(defn, name, env_name=None):
    """Merged settings of a node: defaults -> environment -> role -> agent -> workflow settings -> node."""
    node, agent, role, env = _parts(defn, name, env_name)
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
    env_name = (node_environments(defn, name) or [None])[0]
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


def render(defn, name, repo=None, env_name=None):
    """The role body of node `name`, rendered with its merged settings; a node's own instructions come from its
    settings (`mode`, `focus`), never from a prompt of its own. `node.repo` is the repository of the automation
    copy (ADR-0024), `""` outside a deployed copy; `env_name` the copy's environment (ADR-0027: a node may run in
    several). Raises on an undefined name."""
    node, agent, role, env = _parts(defn, name, env_name)
    ctx = {"settings": settings(defn, name, env_name), "node": {**node, "name": name, "repo": repo or ""},
           "agent": {**agent, "name": node.get("agent")}, "role": role, "environment": env,
           "factory": {k: v for k, v in defn["factory"].items() if k != "environments"}}
    j = _jinja(defn)
    body = defn["bodies"].get(agent.get("role"), "") if agent.get("role") else ""
    return j.from_string(body).render(ctx)


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


def _writes_errors(where, node, agent, env):
    repos = (env or {}).get("repositories") or []
    writes = node.get("writes")
    errs = []
    if writes is not None and writes != "all":
        listed = {full_name(r["repo"]) for r in repos}
        if not isinstance(writes, list) or not all(isinstance(r, str) for r in writes):
            return [f"bad-writes: {where}: `writes` is `all` or a list of repositories"]
        errs = [f"bad-writes: {where}: {r} is not in the environment" for r in writes if full_name(r) not in listed]
    if writes and agent.get("access") == "read-only":
        errs.append(f"writes-read-only: {where}: a node with `writes` runs an agent with `access: read-only`")
    if not writes and agent.get("access") == "full" and not node.get("generated"):
        errs.append(f"no-writes: {where}: a node with `access: full` needs `writes`")
    return errs


def _listens_errors(defn, where, node):
    listens = node.get("listens")
    if listens is None or listens in ("tracker", "tickets"):
        return []
    if not isinstance(listens, list) or not all(isinstance(r, str) for r in listens):
        return [f"bad-listens: {where}: `listens` is `tracker`, `tickets` or a list of ticket repositories"]
    return [f"bad-listens: {where}: {r} is not a ticket repository" for r in listens
            if full_name(r) not in tracker_repositories(defn)]


def _event_errors(defn, name, node):
    """`writes-not-heard` and `schedule-writes-several` of node `name`."""
    where, writes = f"nodes.{name}", node_writes(defn, name)
    listens = node_listens(defn, name)
    errs = [f"writes-not-heard: {where}: writes {r} but does not listen to it" for r in writes
            if _has_event_trigger(node) and r not in listens]
    if len(writes) > 1 and any(isinstance(t, dict) and "schedule" in t for t in node.get("triggers") or []):
        errs.append(f"schedule-writes-several: {where}: a scheduled node writes at most one repository")
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
    registry = plugins.registry(f)
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
        if spec.get("harness") not in NO_ROLE and not spec.get("run") and spec.get("role") not in roles:
            errs.append(f"unknown-role: agents.{a}: {spec.get('role')}")
        if spec.get("model") is not None and spec["model"] not in models:
            errs.append(f"unknown-model: agents.{a}: {spec['model']}")
        if spec.get("harness") != "human" and spec.get("model") is None and (f.get("defaults") or {}).get("model") is None:
            errs.append(f"no-model: agents.{a}")
    for e, spec in envs.items():
        repos = (spec or {}).get("repositories") or []
        if not repos:  # compiling needs a repository for the node output (`_primary`)
            errs.append(f"empty-environment: environments.{e}: lists no repositories")
        errs += [f"removed-key: environments.{e}: {r.get('repo')}: `{k}` (ADR-0027: a repository's `write` names the "
                 "environment's writable repository)" for r in repos for k in REMOVED_REPOSITORY_KEYS if k in r]
    errs += scopes.errors(f)  # environments with their own trackers (#1069)
    if "label_prefix" in (f.get("tracker") or {}) and not (
            isinstance(f["tracker"]["label_prefix"], str) and f["tracker"]["label_prefix"]):
        errs.append("bad-label-prefix: factory.yaml: `tracker.label_prefix` is a non-empty string")
    listed = (f.get("tracker") or {}).get("repositories")
    if listed is not None and (not isinstance(listed, list) or not all(isinstance(r, str) for r in listed)):
        errs.append("bad-tracker-repositories: factory.yaml: `tracker.repositories` is a list of repositories")
    if w.get("blockers", "start") not in ("start", "merge"):
        errs.append(f"bad-blockers: workflow.yaml: `blockers` is `start` or `merge`, not {w['blockers']!r}")
    default_env = default_environment(defn)
    errs += scopes.vcs_errors(f, nodes={  # each node's PR effects on its environment's vcs (#1070)
        name: (n.get("environment") or default_env,
               [e for o in (n.get("outcomes") or {}).values() for e in (o or {}).get("effects") or []])
        for name, n in nodes.items() if isinstance(n, dict)})
    in_envs = {full_name(r["repo"]) for spec in envs.values() for r in (spec or {}).get("repositories") or []}
    errs += [f"tracker-repository: factory.yaml: tracker repository {r} is in no environment"
             for r in tracker_repositories(defn) if r not in in_envs]
    code = code_repos(defn)
    tracker_repo = full_name((f.get("tracker") or {}).get("repository") or "")
    tracker_name = (f.get("tracker") or {}).get("repository")
    project_envs = scopes.project_envs(f)

    def deaf_tracker(env, main_name):
        """A ticket trigger hears nothing when the environment's main repository is not its tracker's. #1069: a node
        of the default environment runs once per project environment (each checked by `scopes.errors`); a project
        environment's own tracker is its tracker; a YouTrack tracker wakes nodes in any repository."""
        if project_envs and env == default_env:
            return False
        block = project_envs.get(env)
        if block is None:
            return scopes.adapter(f.get("tracker")) == "github-issues" and main_name != tracker_repo
        return scopes.adapter(block) == "github-issues" and main_name != full_name(block.get("repository") or "")
    pr_labels = {}
    for source in nodes.values():
        source_env = envs.get(source.get("environment") or default_env) or {}
        source_repo = _primary(defn, source_env) if source_env.get("repositories") else ""
        for outcome in (source.get("outcomes") or {}).values():
            for effect in (outcome or {}).get("effects") or []:
                if isinstance(effect, dict) and effect.get("on") == "pr" and ("label" in effect or "unlabel" in effect):
                    label = effect.get("label") or effect.get("unlabel")
                    pr_labels.setdefault(label, set()).add(full_name(source_repo))
    for source_env in envs.values():
        if (source_env or {}).get("ci"):
            source_repo = _primary(defn, source_env) if source_env.get("repositories") else ""
            for state in ("green", "red"):
                label = f"{label_prefix(defn)}ci-{state}"
                pr_labels.setdefault(label, set()).add(full_name(source_repo))
    new_model = adopted(defn)
    for name, n in nodes.items():
        where = f"nodes.{name}"
        if n.get("agent") not in agents:
            errs.append(f"unknown-agent: {where}: {n.get('agent')}")
            continue
        human = (agents[n["agent"]] or {}).get("harness") == "human"
        node_envs = node_environments(defn, name) if new_model else [n.get("environment") or default_env]
        env = node_envs[0] if node_envs else None
        # ADR-0027: a copy's main repository is where its events happen by construction (`copies`); no deaf trigger
        if not human and env in envs and not new_model:
            env_repos = (envs.get(env) or {}).get("repositories") or []
            main_repo = _primary(defn, envs.get(env)) if env_repos else ""  # no `main`: the tracker (ADR-0024)
            main_name = full_name(main_repo)
            for trigger in n.get("triggers") or []:
                if trigger == "manual" or isinstance(trigger, dict) and ("schedule" in trigger or "webhook" in trigger):
                    continue
                pr_label = trigger.get("label") if isinstance(trigger, dict) else None
                code_event = isinstance(trigger, dict) and next(iter(trigger), None) in ("pr", "ci")
                pr_event = code_event or isinstance(trigger, dict) and pr_label in pr_labels
                source_repos = pr_labels.get(pr_label)
                if (source_repos and main_name not in source_repos) or (not pr_event and deaf_tracker(env, main_name)) \
                        or (code_event and main_name not in {full_name(r) for r in code}):
                    errs.append(f"deaf-trigger: {where}: environment {env} main repository {main_repo} "
                                f"does not match tracker {tracker_name}")
                    break
        if (agents[n["agent"]] or {}).get("run") not in (None, "decide"):
            errs.append(f"unknown-command: agents.{n['agent']}: run {agents[n['agent']]['run']}")
        if not human:
            errs += [f"unknown-environment: {where}: {e}" for e in node_envs or [None] if e not in envs]
        if not new_model:  # ADR-0024 keys, until the team migrates (ADR-0027)
            errs += _writes_errors(where, n, agents[n["agent"]] or {}, envs.get(env) or {})
            errs += _listens_errors(defn, where, n) + _event_errors(defn, name, n)
        if n.get("model") is not None and n["model"] not in models:
            errs.append(f"unknown-model: {where}: {n['model']}")
        if not n.get("triggers"):
            errs.append(f"no-trigger: {where}")
        if "prompt" in n:
            errs.append(f"node-prompt: {where}: instructions belong in roles/<role>/role.md; pass what differs as settings")
        merged = {**(defn["workflow"].get("settings") or {}), **(n.get("settings") or {})}
        late = [k for k in EXTERNAL_REVIEW[1:] if merged.get(k)]  # the hours may be a workflow setting
        if late and not all(merged.get(k) for k in EXTERNAL_REVIEW):  # #1263: reconcile needs all three
            errs.append(f"external-review: {where}: set {', '.join(EXTERNAL_REVIEW)} together")
        for t in n.get("triggers") or []:
            if isinstance(t, dict) and "webhook" in t and t["webhook"] not in hooks:
                errs.append(f"unknown-webhook: {where}: {t['webhook']}")
        outcomes = n.get("outcomes") or {}
        if not outcomes:
            errs.append(f"no-outcome: {where}")
        routine = bool(n.get("triggers")) and all(isinstance(t, dict) and "schedule" in t for t in n["triggers"])
        for o, oc in outcomes.items():
            oc = oc or {}
            if routine and oc.get("effects"):  # ADR-0015: `apply_routine` applies no effects
                errs.append(f"routine-effects: {where}.{o}")
            if oc.get("else") is not None and oc["else"] not in outcomes:
                errs.append(f"unknown-else: {where}.{o}: {oc['else']}")
            guards = [k for k in oc.get("if") or {} if k not in GUARDS]
            kinds = [next((k for k in e if k != "on"), None) for e in oc.get("effects") or []]
            errs += _plugin_key_errors(registry, f"{where}.{o}", o, guards, kinds, oc.get("effects") or [])
            for effect in oc.get("effects") or []:
                if isinstance(effect, dict) and ("assign" in effect or "unassign" in effect):
                    key = "assign" if "assign" in effect else "unassign"
                    value = effect[key]
                    valid = isinstance(value, str) or (isinstance(value, list) and all(isinstance(v, str) for v in value))
                    if key == "assign":
                        valid = valid or (isinstance(value, dict) and set(value) == {"field"} and isinstance(value["field"], str))
                    if key == "unassign":
                        valid = valid or value == "all"
                    if not valid:
                        errs.append(f"bad-effect: {where}.{o}: {key} must be a login or a list of logins" + (" or {field: <name>}" if key == "assign" else ""))
                if isinstance(effect, dict) and "request-reviewers" in effect:
                    value = effect["request-reviewers"]
                    if not (value is None or (isinstance(value, str) and value.strip())
                            or (isinstance(value, list) and value and all(isinstance(v, str) and v.strip() for v in value))
                            or (isinstance(value, dict) and set(value) == {"field"} and isinstance(value["field"], str)
                                and value["field"].strip())):
                        errs.append(f"bad-effect: {where}.{o}: request-reviewers must be `changes`, a login, a list of "
                                    "logins or {field: <name>}")
                    elif isinstance(value, dict) and effect.get("on", "pr") != "pr":
                        errs.append(f"bad-effect: {where}.{o}: request-reviewers asks on the PR (`on: pr`)")
            prefix = label_prefix(defn)
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
        for e in (node_envs if not human and n.get("enabled") is not False else []):
            if e not in envs:
                continue
            try:
                render(defn, name, env_name=e)
            except __import__("jinja2").TemplateError as err:
                errs.append(f"render: {where}" + (f" in {e}" if len(node_envs) > 1 else "") + f": {err}")
    for check in registry.hook("definition.errors"):
        errs += check(defn)  # comments: a mentionable agent needs a role; notify: its config and effects
    errs += [plugins.refusal("factory.yaml", k, owner) for k in f  # a block only a plugin reads (`notify:`)
             if (owner := plugins.owner(k, "config")) and not registry.is_on(owner)]
    errs += plugins.errors(f)
    if adopted(defn):
        errs += _repository_errors(defn)
    return errs + _mcp_errors(defn)


def _plugin_key_errors(registry, where, outcome, guards, kinds, effects):
    """An outcome's guard facts, effect kinds, `on:` targets and name that a plugin owns while it is off
    (`module-off`, ADR-0026), and the guards and effects nobody owns."""
    errs = []
    for key, of in [*((k, "guard") for k in guards), *((k, "effect") for k in kinds if k not in EFFECTS)]:
        owner = plugins.owner(key, of)
        if owner is None:
            errs.append(f"unknown-{of}: {where}: {key}")
        elif not registry.is_on(owner):
            errs.append(plugins.refusal(where, key, owner))
    errs += [plugins.refusal(where, f"`on: {e['on']}`", owner) for e in effects if isinstance(e, dict)
             and isinstance(e.get("on"), str) and (owner := plugins.owner(e["on"], "trigger")) and not registry.is_on(owner)]
    if (owner := plugins.owner(outcome, "outcome")) and not registry.is_on(owner):  # the gate refuses it too
        errs.append(plugins.refusal(where, outcome, owner))
    return errs


def warnings(defn):
    """A label an outcome adds that no node triggers on (a person may add it, so it is not an error); an agent
    setting of a plugin that is off (`mentionable` without comments: it is then ignored)."""
    nodes = (defn["workflow"].get("nodes") or {}).values()
    triggered = {t["label"] for n in nodes for t in n.get("triggers") or [] if isinstance(t, dict) and "label" in t}
    added = {e["label"] for n in nodes for oc in (n.get("outcomes") or {}).values()
             for e in (oc or {}).get("effects") or [] if isinstance(e, dict) and "label" in e}
    prefix = label_prefix(defn)
    # A routing label (the stage prefix) should wake a node; `risk:low` and the like are states, read by guards.
    registry = plugins.registry(defn["factory"])
    off = [plugins.refusal(f"agents.{a}", k, owner) for a, spec in (defn["factory"].get("agents") or {}).items()
           for k in spec or {} if (spec or {})[k] and (owner := plugins.owner(k, "agent")) and not registry.is_on(owner)]
    more = [w for check in registry.hook("definition.warnings") for w in check(defn)]
    legacy = ["deprecated-key: defaults.settings.issues_token_env: declare `token: <SECRET_NAME>` on each environment "
              "repository the gate writes with it (ADR-0027)"] \
        if ((defn["factory"].get("defaults") or {}).get("settings") or {}).get("issues_token_env") else []
    moved = ["deprecated-key: workflow.yaml stages.label_prefix moved to tracker.label_prefix in factory.yaml"] \
        if "label_prefix" in (defn["workflow"].get("stages") or {}) else []
    return legacy + moved + [f"unwatched-label: {label}: no node triggers on it" for label in sorted(added)
            if label.startswith(prefix) and label not in triggered] + off + more + _repository_warnings(defn)


# --- Compile into the shapes the engine reads (#669, #677) ---------------------------------------
# Each node compiles to one engine node of the same name: its agent's role and model alias, its triggers as
# wakes, its outcomes' static effects; `if: {stage}` becomes `from`, `if: {ci}` becomes `ci`, the rest stays
# an `if` the gate checks. Labels are commands named after themselves; one an effect adds `on: pr` is a PR label.
TRIGGER_WAKES = {("pr", "reviewed"): "pr reviewed", ("pr", "pushed"): "pr pushed", ("pr", "opened"): "pr opened",
                 ("pr", "merged"): "state pr-merged", ("ticket", "closed"): "ticket closed", ("pr", "commented"): "pr commented",
                 ("ticket", "commented"): "ticket commented"}
EFFECTS = ("stage", "label", "unlabel", "comment", "review", "merge", "update-branch", "create-issues",
           "request-reviewers", "park-behind-main-red", "assign", "unassign", "ready-for-review")  # and the plugins' effects


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


def youtrack_tickets(defn):
    """Whether a ticket repository of the definition keeps its tickets in YouTrack: the adapter of its own tracker
    block, else its environment's, else the top-level one (`tracker: none` holds no tickets)."""
    f = defn["factory"]
    top = _as_block(f.get("tracker")) or {}
    for env in (f.get("environments") or {}).values():
        env = env or {}
        for r in env.get("repositories") or []:
            if not isinstance(r, dict) or r.get("tracker") in ("none", False):
                continue
            layers = (_as_block(r.get("tracker")), _as_block(env.get("tracker")), top)
            if next((b["adapter"] for b in layers if b and b.get("adapter")), None) == "youtrack":
                return True
    return False


def to_project(defn):
    """(project descriptor, format-2 workflow) of the definition, for the project loaders (`project.load`,
    `workflow.load`) and `config`. The definition must have no `errors`."""
    f, w = defn["factory"], defn["workflow"]
    nodes, agents, envs = w.get("nodes") or {}, f.get("agents") or {}, f.get("environments") or {}
    models = f.get("models") or {}
    defaults = f.get("defaults") or {}
    dset, wset = defaults.get("settings") or {}, w.get("settings") or {}
    stages = w.get("stages") or {}
    prefix = label_prefix(defn)
    default_env = default_environment(defn)

    # Repositories: every listed one; the primary is the main repository of the default environment.
    primary = _primary(defn, envs[default_env])
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
        repos = e.get("repositories") or []
        help_ = environment_secrets(defn, en)
        new_model = adopted(defn)
        written = {r["repo"] for r in env_repositories(defn, en) if r["write"]} if new_model else \
            {r for name in nodes if (nodes[name].get("environment") or default_env) == en for r in node_writes(defn, name)}
        tokens = {names[r["repo"]]: r["token"] for r in repos if isinstance(r, dict) and isinstance(r.get("token"), str) and r["token"]}
        environments[en] = {"repositories": [names[r["repo"]] for r in repos], "secrets": list(help_),
                            **({"tokens": tokens} if tokens else {}),  # a repository's write token secret (ADR-0027)
                            # where the gate may write in this environment (#994): what its nodes write (ADR-0024)
                            "writable": [names[r["repo"]] for r in repos if full_name(r["repo"]) in written],
                            "secret_help": help_,
                            "domains": list(e.get("domains") or []), "vm": e.get("vm", "default"), "startup": STARTUP,
                            # ADR-0027: a repository's own tracker is a ticket place (`ticket_places`), not a #1069 project
                            **({"tracker": dict(e["tracker"])} if isinstance(e.get("tracker"), dict) and not new_model else {}),
                            **({"vcs": e["vcs"]} if e.get("vcs") else {}),  # its own vcs (#1070)
                            # the checks that count as CI (#1211); the gate reads them as `ci_checks_of`
                            **({"ci": {"checks": list(e["ci"]["checks"])}} if (e.get("ci") or {}).get("checks") else {})}

    # Stages: every name the nodes use, plus the engine's PR facts.
    used = _used_stages(w)
    kind = {s: k for k in ("final", "waiting") for s in stages.get(k) or []}
    ticket_store = "tag" if youtrack_tickets(defn) else "label"  # a ticket-side label is a tag where tickets live in YouTrack
    states = {s: {"kind": kind.get(s, "active"), "store": {ticket_store: f"{prefix}{s}"}} for s in sorted(used)}
    states["pr-merged"] = {"kind": "final", "store": {"pr": "merged"}}

    # Commands: every label a trigger or an effect names; on the PR when an effect puts it there (`on: pr`).
    # ci-sync puts the CI result labels on the PR itself (#790, #1207), so they are PR labels too.
    on_pr = {f"{prefix}ci-green", f"{prefix}ci-red"}
    for n in nodes.values():
        for oc in (n.get("outcomes") or {}).values():
            on_pr |= {e.get("label") or e.get("unlabel") for e in (oc or {}).get("effects") or []
                      if ("label" in e or "unlabel" in e) and e.get("on") == "pr"}
    commands = {}
    for label in sorted({x for n in nodes.values() for x in _labels_of(n)}):
        commands[_name(label, prefix)] = {"store": {("pr_label" if label in on_pr else ticket_store): label}}

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
        **{k: f[k] for k in ("vcs", "code_host") if f.get(k)},  # the vcs of the pull requests (#1070)
        "environments": environments,
        "workflow": WORKFLOW,
        "states": states,
        "commands": commands,
        **({"issues_token_env": dset["issues_token_env"]} if dset.get("issues_token_env") else {}),  # deprecated (#1236)
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
        **({"plugins": plugins.listed(f)} if plugins.listed(f) else {}),
        **({"surface": f["surface"]} if f.get("surface") else {}),  # where the nodes run (ADR-0026: air, local)
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
    env_name = _first_env(n) or default_env
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
    if n.get("timeout_minutes"):  # reconcile waits this long for its outcome (`limits.run_timeout_minutes` otherwise)
        out["timeout_minutes"] = n["timeout_minutes"]
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
    prefix = label_prefix(defn)
    default_env, all_stages = default_environment(defn), sorted(_used_stages(w))
    primary = _primary(defn, envs[default_env])
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
                            "startup": STARTUP,
                            "declared": [{"name": k, **v} for k, v in environment_variables(defn, en).items()]}
    schedules, out = {}, {}
    for name, n in (w.get("nodes") or {}).items():
        a = agents[n["agent"]]
        role = {"script": "script", "human": "human"}.get(a.get("harness")) or a.get("role") or n["agent"]
        env_name = _first_env(n) or default_env
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
        if n.get("timeout_minutes"):
            node["timeout_minutes"] = n["timeout_minutes"]
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
    states = {name: s for name, s in descriptor["states"].items() if {"label", "tag"} & s["store"].keys()}
    out = {**descriptor, "tracker": {**descriptor["tracker"], "label_prefix": label_prefix(defn),
                                     "stages": {name: s["store"].get("label") or s["store"]["tag"] for name, s in states.items()},
                                     "stage_kinds": {name: s["kind"] for name, s in states.items()},
                                     "commands": {name: c["store"] for name, c in descriptor["commands"].items()}}}
    return {**out, **ticket_places(defn, out["tracker"])} if adopted(defn) else out


def ticket_places(defn, tracker):
    """ADR-0027, for the gate: one ticket, one repository, one PR (`one_repository_per_ticket`). The GitHub Issues
    homes are the tracker's `repositories` (#982: one GitHub tracker reads tickets `owner/repo#n` of each), the top
    `tracker.repository` first; every repository whose tracker has no home (YouTrack) is a `ticket_places` entry the
    gate routes to by id and scope, its PRs on its own vcs."""
    homes, places = [], []
    # #1325: the GitHub Issues homes share the top tracker only when it is GitHub Issues too; under another top tracker
    # (YouTrack) each home is a place of its own, or its issues get the top tracker's adapter and fall out of scope
    shared = scopes.adapter(tracker) == "github-issues"
    for env in (defn["factory"].get("environments") or {}):
        repos = repositories(defn, env)
        for r in repos:
            c = contract(defn, "tracker", r["tracker"]) if r["tracker"] else None
            if c is None:
                continue
            if (home := c.home(r["tracker"], r["repo"])) and shared:
                homes.append(next((x["repo"] for x in repos if x["repo"].lower() == home), home))
            else:
                block = {**r["tracker"], "repository": r["repo"]} if home else r["tracker"]  # #1325: its own issues
                if not any(p["repository"] == r["repo"] and p["tracker"] == block for p in places):
                    places.append({"repository": r["repo"], "tracker": block, "vcs": r["vcs"],
                                   "repositories": [r["repo"], *(x["repo"] for x in repos if x["repo"] != r["repo"])]})
    places.sort(key=lambda p: not p["tracker"].get("scope"))  # #1361: an unscoped place (intake) holds every ticket of
    # its tracker, so the gate tries it after the scoped places, whatever the order of the environments
    top = next((h for h in homes if h.lower() == full_name(tracker.get("repository") or "").lower()), None)
    ordered = list(dict.fromkeys([*([top] if top else []), *homes]))
    return {"tracker": {**tracker, "repositories": ordered, **({"repository": ordered[0]} if ordered and not top else {})},
            "ticket_places": places, "one_repository_per_ticket": True}


# --- The sweep's decisions, read from the node graph ------------------------------------------------
def _allows(graph, node, outcome, stage):
    """True when the graph's `node` has `outcome` and it may run from `stage` (its `from`, when it lists any)."""
    out = ((graph.get("nodes") or {}).get(node) or {}).get("outcomes", {}).get(outcome)
    return out is not None and out.get("enabled") is not False and (not out.get("from") or stage in out["from"])


def plan(graph, facts, auto_merge=None, default_tier="mid", blockers="start", start_waits=False):
    """(node, outcome, payload) of the first start, merge, finish, reopen or join decision whose facts hold and whose
    outcome the node graph (`nodes(defn)` or the engine's graph) allows from the ticket's stage, else None. Nothing
    here reads a CI label: `ci` is the fact of the workflow runs at the head."""
    import gate  # lazily: the gate imports this module
    stage, pr_open = facts["stage"], facts.get("pr_state") == "open"
    waits = blockers == "merge" and facts.get("blockers_open")  # approved, but a blocker is still open

    def allowed(node, outcome):
        return _allows(graph, node, outcome, stage)

    if facts.get("pr_state") == "merged" and allowed("finish", "merged"):  # `from` lists every stage but the final ones
        comments = facts.get("comments", [])
        return ("finish", "merged", {}) if not gate.after_merge_checks(facts.get("body")) \
            or not gate.after_merge_comment(comments) or gate.after_merge_complete(comments) else None
    if stage == "ready" and allowed("start", "started") \
            and (blockers == "merge" and not start_waits or not facts.get("blockers_open")) \
            and facts.get("issue_state", "open") == "open":
        return "start", "started", {"tier": gate.fix_tier(facts, default_tier)}
    if facts.get("answered") and allowed("finish", "answered"):
        return "finish", "answered", {}
    if pr_open and facts.get("conflict") and allowed("finish", "conflict"):
        return "finish", "conflict", {}  # the cheap updater merges main; the reviewers then check the resolution
    if pr_open and allowed("reopen", "reopened") \
            and ((facts.get("round_head") is not None and facts.get("head") != facts.get("round_head"))
                 or facts.get("ci") == "red"):
        return "reopen", "reopened", {}
    if not waits and allowed("merge", "merged") and (m := gate.merge_decision(facts, auto_merge)) \
            and allowed("merge", m[0]):
        return "merge", *m
    if (kids := facts.get("children")):
        if bad := [c for c in kids if c["blocked"] or c["not_planned"]]:
            if allowed("finish", "child-blocked"):
                what = ", ".join(f"#{c['number']} ({'closed as not planned' if c['not_planned'] else 'blocked'})" for c in bad)
                return "finish", "child-blocked", {"reason": f"[factory] Blocked: child tickets need a person: {what}. "
                                                             f"Fix them, then set {gate.vocab.stage('ready')} on this ticket."}
        elif all(c["state"] == "closed" for c in kids) and allowed("finish", "children-done"):
            return "finish", "children-done", {}
    if stage == "in-review" and allowed("join", "waiting") and facts.get("waiting_reviewers") \
            and not facts.get("handed_to_fix"):
        return "join", "waiting", {}  # ask them again; nothing moves until they decide
    if stage == "in-review" and allowed("join", "approved") and (j := gate.join_decision(facts)):
        return "join", *j
    return None

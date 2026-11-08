"""Per-environment trackers with scope (#1069, ADR-0026 "Per-environment trackers").

An environment may name its own tracker, `environments.<env>.tracker: {adapter, ..., scope}`: a project of the factory.
Unset keys come from the top-level `tracker` when the adapters match; with another adapter only the factory's own keys
(stages, labels, ...) carry over, never the system's (`SYSTEM_KEYS`). `scope` is the environment's share of that
tracker, in the tracker's terms: `{label}` on github-issues (the ticket carries the label), `{field, value}` on youtrack
(the ticket's custom field holds the value; `type` is the field's YouTrack type, used when a ticket is created). A
ticket inside no environment's scope is no environment's: the gate leaves it alone. A team with no environment tracker
has none of this: its one top-level tracker, as before.

- `project_envs(config)`: {env: merged tracker block} of the environments that declare a tracker, in order.
- `identity(block)`: the system a block names; environments on one identity split it by scope.
- `matches(scope, attrs, fields)`: whether a ticket with these labels and field values is in `scope`.
- `errors(config)`: the `factory validate` checks.
- `ScopedTracker`: the gate's tracker over the project environments: each ticket goes to its environment's tracker.
"""
import plugins

SYSTEM_KEYS = ("adapter", "repository", "url", "project", "token_env", "done_state", "repository_field", "bot_logins",
               "scope", "repositories")
SCOPE_KEYS = {"github-issues": ({"label"}, set()), "youtrack": ({"field", "value"}, {"type"})}  # (required, optional)


class OutOfScope(Exception):
    """The ticket is inside no project environment's scope (or not the run's environment's): the gate does nothing."""


def adapter(block):
    return (block or {}).get("adapter") or "github-issues"


def merged(config, block):
    """Environment tracker `block` over the top-level tracker: every unset key when the adapters match, else only the
    factory's own keys."""
    top = config.get("tracker") or {}
    keep = (lambda k: k != "scope") if adapter(top) == adapter(block) else (lambda k: k not in SYSTEM_KEYS)
    return {**{k: v for k, v in top.items() if keep(k)}, **block}


def project_envs(config):
    """{env: merged tracker block} of the environments of `config` that declare a `tracker`."""
    envs = (config or {}).get("environments")
    return {name: merged(config, e["tracker"]) for name, e in (envs.items() if isinstance(envs, dict) else ())
            if isinstance(e, dict) and isinstance(e.get("tracker"), dict)}


def identity(block):
    """The system a tracker block names: (adapter, repository) or (adapter, url, project)."""
    if adapter(block) == "youtrack":
        return "youtrack", str(block.get("url") or "").rstrip("/"), block.get("project")
    return adapter(block), str(block.get("repository") or "").lower()


def matches(scope, attrs=(), fields=None):
    """Whether a ticket holding labels or tags `attrs` and custom field values `fields` ({field: [values]}) is in
    `scope`; no scope holds every ticket."""
    if not scope:
        return True
    if "label" in scope:
        return scope["label"] in attrs
    return scope.get("value") in ((fields or {}).get(scope.get("field")) or [])


def owns_id(block, ticket):
    """Whether `ticket` can be a ticket of this tracker: `<project>-<n>` on YouTrack, a number on GitHub."""
    if adapter(block) == "youtrack":
        return isinstance(ticket, str) and (not block.get("project") or ticket.startswith(f"{block['project']}-"))
    return isinstance(ticket, int) or str(ticket).isdigit()


def errors(config, where="factory.yaml"):
    """The checks of the environment trackers: a selected plugin provides the adapter, the scope keys are the
    adapter's, and environments that share a tracker each have a different scope."""
    envs, out = project_envs(config), []
    if not envs:
        return out
    provided = plugins.registry(config).impls("tracker")
    shared = {}
    for name, block in envs.items():
        w, a, scope = f"{where}: environments.{name}.tracker", adapter(block), block.get("scope")
        if a not in provided:
            out.append(f"env-tracker: {w}: adapter {a}: no selected plugin provides this tracker")
        if a == "youtrack" and not (block.get("url") and block.get("project")):
            out.append(f"env-tracker: {w}: youtrack needs `url` and `project` (here or in the top-level tracker)")
        if a == "github-issues" and not block.get("repository"):
            out.append(f"env-tracker: {w}: github-issues needs `repository` (here or in the top-level tracker)")
        if scope is not None:
            need, optional = SCOPE_KEYS.get(a, (set(), set()))
            keys = set(scope) if isinstance(scope, dict) else set()
            if not isinstance(scope, dict) or keys - need - optional or need - keys \
                    or not all(isinstance(v, str) and v for v in scope.values()):
                out.append(f"bad-scope: {w}.scope: {a} takes {' and '.join(sorted(need)) or 'no scope'}"
                           + (f" (optional: {', '.join(sorted(optional))})" if optional else ""))
        shared.setdefault(identity(block), []).append((name, scope if isinstance(scope, dict) else None))
    for ident, members in shared.items():
        if len(members) < 2:
            continue
        names = ", ".join(n for n, _ in members)
        out += [f"ambiguous-scope: {where}: environments.{n}.tracker has no scope but shares its tracker with {names}"
                for n, s in members if not s]
        seen = {}
        for n, s in members:
            key = tuple(sorted((k, v) for k, v in (s or {}).items() if k != "type"))
            if s and key in seen:
                out.append(f"overlapping-scope: {where}: environments.{n}.tracker and environments.{seen[key]}.tracker "
                           f"have the same scope on one tracker")
            seen.setdefault(key, n)
    return out


def env_repositories(config, env):
    """The owner/repo names of environment `env`'s repositories (descriptor names or `repo:` entries)."""
    urls = {r["name"]: r["url"] for r in config.get("repositories") or [] if isinstance(r, dict) and "name" in r}
    out = []
    for r in ((config.get("environments") or {}).get(env) or {}).get("repositories") or []:
        url = urls.get(r, r) if isinstance(r, str) else r.get("repo", "")
        out.append("/".join(str(url).rstrip("/").removesuffix(".git").split("/")[-2:]).lower())
    return out


TICKET_METHODS = ("facts", "stale_facts", "parent", "children", "blocking", "blocked_by", "not_planned",
                  "changed_since", "in_scope")


class ScopedTracker:
    """The gate's tracker and code host when environments have their own trackers. Each ticket goes to its
    environment's tracker: the run's environment (`env`, FACTORY_ENV) when it is a project environment, else the first
    project environment whose tracker can hold the ticket's id and whose scope holds the ticket. A ticket of no
    environment raises OutOfScope. `build(config, env)` returns that environment's (tracker, vcs)."""

    def __init__(self, config, build, env=None):
        self.config, self.build = config, build
        self.envs = project_envs(config)
        self.fixed = env if env in self.envs else None
        self.prefer, self.current = None, self.fixed
        self._systems, self._env_of = {}, {}

    def systems(self, env):
        if env not in self._systems:
            self._systems[env] = self.build(self.config, env)
        return self._systems[env]

    def candidates(self):
        if self.fixed:
            return [self.fixed]
        return sorted(self.envs, key=lambda e: e != self.prefer)

    def env_of(self, ticket):
        """The environment of `ticket`, else None. A tracker that cannot read the ticket (another repository's
        number) does not hold it."""
        if ticket not in self._env_of:
            self._env_of[ticket] = None
            for env in self.candidates():
                if not owns_id(self.envs[env], ticket):
                    continue
                tracker = self.systems(env)[0]  # a tracker that cannot be built fails the step
                try:
                    held = tracker.in_scope(ticket)
                except Exception:
                    held = False
                if held:
                    self._env_of[ticket] = env
                    break
        return self._env_of[ticket]

    def tracker_of(self, ticket):
        env = self.env_of(ticket)
        if env is None:
            raise OutOfScope(ticket)
        self.current = env
        return self.systems(env)[0]

    def apply(self, e):
        return self.tracker_of(e["ticket"]).apply(e)

    def sweep_tickets(self, stages, open_only=False, now=None):
        """Each environment's tickets in `stages`, from its own scoped query; a ticket is its first environment's."""
        found = []
        for env in self.candidates():
            for n in self.systems(env)[0].sweep_tickets(stages, open_only, now):
                if self._env_of.setdefault(n, env) == env and n not in found:
                    found.append(n)
        return found

    def prefetch(self, numbers):
        for env in self.candidates():
            mine = [n for n in numbers if self._env_of.get(n) == env]
            tracker = self.systems(env)[0]
            if mine and hasattr(tracker, "prefetch"):
                tracker.prefetch(mine)

    def env_of_repository(self, repository):
        """The project environment that lists `repository` (owner/repo), its main repository first; else None."""
        name = str(repository or "").lower()
        return next((e for e in self.candidates() if name in env_repositories(self.config, e)[:1]), None) \
            or next((e for e in self.candidates() if name in env_repositories(self.config, e)), None)

    def vcs_for(self, repository=None):
        env = self.env_of_repository(repository) or self.current or self.candidates()[0]
        return env, self.systems(env)[1]

    def ticket_for_pr(self, number, repository=None):
        """The ticket of PR `number`, from the vcs of the environment that lists the PR's repository; that
        environment is tried first for the ticket."""
        env, vcs = self.vcs_for(repository)
        self.prefer = env
        return vcs.ticket_for_pr(number, repository) if repository else vcs.ticket_for_pr(number)

    def __getattr__(self, name):
        """A ticket method (`TICKET_METHODS`) goes to the ticket's environment; any other to the current one. Only
        what the environments' trackers have exists here (`hasattr` keeps its meaning in the gate)."""
        if name.startswith("_") or name in ("config", "build", "envs", "fixed", "prefer", "current"):
            raise AttributeError(name)
        probe = self.systems(self.current or self.candidates()[0])[0]
        if not hasattr(probe, name):
            raise AttributeError(name)
        if name in TICKET_METHODS:
            return lambda n, *a, **kw: getattr(self.tracker_of(n), name)(n, *a, **kw)
        return getattr(probe, name)

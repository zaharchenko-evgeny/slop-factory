"""Engine plugins (ADR-0026): what a team turns on, and which plugin owns each workflow key.

A plugin is the package `factory_plugins.<name>`. Its `__init__.py` holds one `PLUGIN = Plugin(...)` declaration and
nothing else: code is named as `"module:function"` strings and imported only when the core calls it. A pull-request
node imports the engine from main's worktree, so it also runs main's plugin code; a plugin that is off costs the core
one import of its declaration.

- `selected(config, use)`: the plugins a config turns on: `plugins:` (alias `modules:`), the systems, surface and agent
  runners it names, the ones a command uses (`use`: `deploy --target local` uses the local surface, a local run its
  agent runner, `chart` the chart tool), and every `requires`, closed.
- `registry(config, use)`: the selection's effects, guards, triggers, outcomes, hooks, implementations and commands
  (cached per selection).
- `owner(key, of)`: the plugin, on or off, that owns a workflow key; `refusal(where, key)` is the one message used
  when it is off.
- `errors(config)`: the `plugins:` list checks of `factory validate`.
- `impl_error(config, interface, name, where)`: why no selected plugin provides an implementation (a `vcs:` name).
"""
import importlib
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass(frozen=True)
class Plugin:
    name: str
    provides: dict = field(default_factory=dict)  # "tracker"|"vcs"|"surface"|"agent"|"notifier" -> {impl: "mod:Class"}
    commands: dict = field(default_factory=dict)  # cli word -> "mod:function" (argv) -> exit code
    effects: dict = field(default_factory=dict)  # (kind, target) -> "mod:function"; a target in `triggers` is its own
    guards: dict = field(default_factory=dict)  # guard fact -> "mod:function" (value, facts, tracker, ticket) -> bool
    triggers: tuple = ()  # extra trigger / `on:` keys (comments: mention)
    outcomes: tuple = ()  # marker outcomes (comments: ask)
    hooks: dict = field(default_factory=dict)  # hook name -> "mod:function"
    requires: tuple = ()  # plugins turned on with this one
    agent_keys: tuple = ()  # agent settings in factory.yaml that only this plugin reads (comments: mentionable)
    config_keys: tuple = ()  # top-level factory.yaml keys that only this plugin reads (notify: notify)


# Every planned plugin (#1064), so a team may list one before its slice lands: a planned plugin with no package yet
# turns on nothing.
KNOWN = {name: f"factory_plugins.{name}" for name in (
    "comments", "notify", "notify_email", "github", "youtrack", "space", "air", "local", "claude", "codex", "junie", "pi",
    "chart", "facts")}
# The plugin a system, surface, agent runner or development tool name turns on implicitly (a tool only through `use`).
IMPLIED = {"tracker": {"github-issues": "github", "github": "github", "youtrack": "youtrack"},
           "vcs": {"github": "github", "space": "space"},
           "surface": {"air": "air", "local": "local"},
           "agent": {"claude": "claude", "claude-code": "claude", "codex": "codex", "junie": "junie", "pi": "pi"},
           "tool": {"chart": "chart", "facts": "facts"}}
LISTS = ("plugins", "modules")  # `modules:` is the ADR-0025 name of `plugins:`
CATEGORIES = ("effect", "guard", "trigger", "outcome", "agent", "command", "config")


def refusal(where, key, plugin=None):
    """The one module-off message: `key`, used at `where`, needs plugin `plugin` (its owner by default)."""
    return f"module-off: {where}: {key} needs plugins: [{plugin or owner(key)}]"


def load(spec):
    """The object a `"module:name"` string names, imported now."""
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)


@lru_cache(maxsize=None)
def declaration(name):
    """The `PLUGIN` of a known plugin; an empty one for a planned plugin that has no package yet."""
    path = KNOWN[name]
    try:
        module = importlib.import_module(path)
    except ModuleNotFoundError as e:
        if e.name != path:
            raise
        return Plugin(name)
    plugin = module.PLUGIN
    if plugin.name != name:
        raise ValueError(f"plugin {path}: declares the name {plugin.name!r}")
    return plugin


def keys(plugin):
    """{(category, key)} a plugin owns. An effect on one of its own triggers (`comment` `on: mention`) is owned by
    the trigger: the effect kind stays the core's."""
    out = {("guard", k) for k in plugin.guards} | {("trigger", k) for k in plugin.triggers}
    out |= {("outcome", k) for k in plugin.outcomes} | {("agent", k) for k in plugin.agent_keys}
    out |= {("command", k) for k in plugin.commands} | {("config", k) for k in plugin.config_keys}
    return out | {("effect", kind) for kind, target in plugin.effects if target not in plugin.triggers}


@lru_cache(maxsize=None)
def owners():
    """{(category, key): plugin} over every known plugin. Two owners of one key are an error at load."""
    out = {}
    for name in KNOWN:
        for key in keys(declaration(name)):
            if out.setdefault(key, name) != name:
                raise ValueError(f"plugin-clash: {key[0]} {key[1]}: owned by {out[key]} and {name}")
        for need in declaration(name).requires:
            if need not in KNOWN:
                raise ValueError(f"plugin {name}: requires unknown plugin {need}")
    return out


def owner(key, of=None):
    """The plugin that owns workflow key `key` in category `of` (any category when None), whether on or off."""
    table = owners()
    return next((table[(c, key)] for c in ([of] if of else CATEGORIES) if (c, key) in table), None)


def listed(config):
    """The names `plugins:` and `modules:` list, in order; a value that is not a list lists nothing."""
    out = []
    for key in LISTS:
        value = (config or {}).get(key)
        if isinstance(value, list):
            out += [n for n in value if isinstance(n, str) and n not in out]
    return out


def _named(value):
    """The adapter name of a `tracker:`/`vcs:` value: a mapping's `adapter`, or the string itself."""
    return value.get("adapter") if isinstance(value, dict) else value if isinstance(value, str) else None


def implied(config):
    """The plugins a config turns on by naming a system, a surface or an agent runner (github when none is named)."""
    config = config or {}
    names = [("tracker", _named(config.get("tracker")) or "github-issues"),
             ("vcs", _named(config.get("vcs") or config.get("code_host")) or "github"),
             ("surface", config.get("surface"))]
    envs = config.get("environments")
    names += [("vcs", _named(e.get("vcs"))) for e in (envs.values() if isinstance(envs, dict) else [])
              if isinstance(e, dict) and e.get("vcs")]
    names += [("tracker", _named(e.get("tracker")) or "github-issues") for e in (envs.values() if isinstance(envs, dict) else [])
              if isinstance(e, dict) and isinstance(e.get("tracker"), dict)]  # an environment's own tracker (#1069)
    repos = [r for e in (envs.values() if isinstance(envs, dict) else []) if isinstance(e, dict)
             for r in e.get("repositories") or [] if isinstance(r, dict)]
    names += [(kind, _named(r.get(kind))) for r in repos for kind in ("tracker", "vcs")
              if r.get(kind) not in (None, "none", False)]  # a repository's own tracker or vcs (ADR-0027)
    names += [("tracker", _named(p.get("tracker")) or "github-issues") for p in config.get("ticket_places") or []
              if isinstance(p, dict) and isinstance(p.get("tracker"), dict)]  # a compiled definition's YouTrack places (ADR-0027)
    models = config.get("models")
    names += [("agent", m.get("harness")) for m in (models.values() if isinstance(models, dict) else [])
              if isinstance(m, dict)]
    return [IMPLIED[i][n] for i, n in names if isinstance(n, str) and n in IMPLIED[i]]


def selected(config, use=()):
    """The sorted names of the plugins `config` turns on, every `requires` included. Unknown names are left out
    (`errors` reports them). `use`: (interface, name) pairs a command uses, whose plugins are on for it."""
    used = [IMPLIED[i][n] for i, n in use if n in IMPLIED.get(i, {})]
    todo = [n for n in [*listed(config), *implied(config), *used] if n in KNOWN]
    out = set()
    while todo:
        name = todo.pop()
        if name not in out:
            out.add(name)
            todo += declaration(name).requires
    return tuple(sorted(out))


def errors(config, where="factory.yaml"):
    """`plugins:` (or `modules:`) must be a list of known plugins."""
    errs = []
    for key in LISTS:
        value = (config or {}).get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            errs.append(f"bad-plugins: {where}: `{key}` must be a list, for example [comments]")
        else:
            errs += [f"unknown-plugin: {key}: {n}" for n in value if n not in KNOWN]
    return errs


def impl_error(config, interface, name, where):
    """Why no selected plugin provides `interface` `name` (a `vcs:` adapter, ...), else None: the module-off refusal
    for a known implementation whose plugin is off, else `unknown-<interface>` naming the known ones."""
    if name in registry(config).impls(interface):  # nothing is imported
        return None
    if name in IMPLIED.get(interface, {}):
        return refusal(where, name, IMPLIED[interface][name])
    return (f"unknown-{interface}: {where}: {name}: no selected plugin provides this {interface} "
            f"(known: {', '.join(sorted(IMPLIED.get(interface, {})))})")


class Registry:
    """What the selected plugins add. Code is imported only when a caller asks for it."""

    def __init__(self, names):
        self.names = tuple(names)
        self.plugins = {n: declaration(n) for n in self.names}

    def is_on(self, name):
        return name in self.plugins

    def owner(self, key, of=None):
        return owner(key, of)

    def refusal(self, where, key, of=None):
        return refusal(where, key, owner(key, of))

    def effect(self, kind, target="ticket"):
        """The handler spec of effect `kind` on `target` (`ticket`, `pr`, or a plugin trigger such as `mention`)."""
        return next((p.effects[(kind, target)] for p in self.plugins.values() if (kind, target) in p.effects), None)

    def effect_kinds(self):
        """The effect kinds the selected plugins add (a kind on their own trigger is the core's)."""
        return {k for p in self.plugins.values() for k, t in p.effects if t not in p.triggers}

    def guards(self):
        return {k: v for p in self.plugins.values() for k, v in p.guards.items()}

    def triggers(self):
        return {k for p in self.plugins.values() for k in p.triggers}

    def outcomes(self):
        return {k for p in self.plugins.values() for k in p.outcomes}

    def hook(self, name, plugin=None):
        """The callables the selected plugins (only `plugin`, when given) attach to hook `name`, imported now."""
        return [load(p.hooks[name]) for n, p in self.plugins.items() if name in p.hooks and plugin in (None, n)]

    def impl(self, interface, name):
        """The class a selected plugin provides as `interface` `name`, imported now; None when none does."""
        spec = next((p.provides[interface][name] for p in self.plugins.values()
                     if name in (p.provides.get(interface) or {})), None)
        return load(spec) if spec else None

    def impls(self, interface):
        """The names of every `interface` the selected plugins provide, sorted; nothing is imported."""
        return sorted({n for p in self.plugins.values() for n in p.provides.get(interface) or {}})

    def command(self, word):
        """The function behind CLI word `word`, imported now; None when no selected plugin adds it."""
        spec = next((p.commands[word] for p in self.plugins.values() if word in p.commands), None)
        return load(spec) if spec else None


@lru_cache(maxsize=None)
def _registry(names):
    return Registry(names)


def registry(config, use=()):
    """The registry of the plugins `config` turns on (and those `use` names), cached per selection."""
    return _registry(selected(config, use))


def reset():
    """Forget the cached declarations and registries (tests that change KNOWN)."""
    for cached in (declaration, owners, _registry):
        cached.cache_clear()

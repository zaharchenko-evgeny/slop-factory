# ADR-0026: Engine plugins: events in, records out, effects

Status: accepted, requested by #1064 (slice 1: #1065). Amends ADR-0025 and
ADR-0004.

## Context

ADR-0025 made comment reactions an opt-in module. The core still named it
everywhere: `gate.py`, `definition.py`, `nodes.py` and `deploy.py` each
imported `comments` and checked `modules: [comments]` in their own way, with
four different refusal messages. Notifiers (`notify.ADAPTERS`), local
harnesses (`HARNESS_CLI`, `SKILL_DIR`), Space (`adapter == "space"`) and the
GitHub and YouTrack trackers are closed lists or branches of the same kind.

An earlier plan moved the trackers and code hosts behind two wide interfaces
of about 18 methods each. That would have moved the factory's rules into
every adapter: the stage policy, the merge gates, idempotency and the
`[factory]` text rules.

## Decision

1. **A plugin is a package `factory_plugins.<name>`.** Its `__init__.py`
   holds one declaration and nothing else:

   ```python
   PLUGIN = Plugin(
       name="comments",
       provides={},   # "tracker"|"vcs"|"surface"|"agent"|"notifier" -> {impl: "mod:Class"}
       commands={},   # cli word -> "mod:function" (argv) -> exit code
       effects={},    # (kind, target) -> "mod:function"
       guards={},     # guard fact -> "mod:function"
       triggers=(),   # extra trigger / `on:` keys
       outcomes=(),   # marker outcomes
       hooks={},      # hook name -> "mod:function"
       requires=(),   # plugins turned on with this one
       agent_keys=(), # agent settings only this plugin reads
   )
   ```

   Code is named as `"module:function"` strings. The core imports it only
   when it calls it. The `factory_plugins` namespace keeps plugin names
   apart from installed packages.
2. **The main-worktree rule.** A pull-request node imports the engine from
   main's worktree (`cli.load_engine`). Plugins load from the same
   directory, so a pull request cannot replace plugin code either. A plugin
   that is off costs one import of its declaration; a test checks
   `sys.modules`.
3. **Selection.** `plugins.selected(config)` is:
   - the names `plugins:` lists (`modules:` is its ADR-0025 alias);
   - the system named by `tracker.adapter` (github when unset);
   - every vcs named by `vcs:` (alias `code_host:`) or by
     `environments.<env>.vcs` (github when unset);
   - the `surface:`;
   - the runner of every `models.<alias>.harness`;
   - every `requires`, closed transitively.

   `plugins.KNOWN` lists every planned plugin. A planned plugin with no
   package yet turns on nothing, so a team can list it before it lands.
   An unknown name is `unknown-plugin`; a value that is not a list is
   `bad-plugins`.
4. **Ownership.** Each workflow key has one owner: an effect kind, a guard
   fact, an `on:` target (trigger), a marker outcome, an agent setting or a
   CLI word. Two owners of one key are an error when the registry loads.
   Nodes never name a plugin. They use keys, and the registry finds the
   owner.
5. **One refusal format.** A key whose plugin is off is refused by
   `factory validate` and by the gate with:

   ```
   module-off: <where>: <key> needs plugins: [<plugin>]
   ```

6. **Events in, records out, effects.** A system plugin (tracker, vcs or
   both) provides `event(payload)`, `read(subject, want)` and
   `find(query)`, plus effect handlers keyed by `(kind, target)` and setup
   hooks (`provision`, `wake_rules`, `probes`). Records
   (`TicketRecord`, `PrRecord`, `RepoRecord`) are plain data.
7. **Core policies stay in the core.** The core derives every fact from
   records, and wraps every handler in the factory's rules:
   - `stage: done` closes the ticket first;
   - `merge` runs only when its `needs` facts hold, one repository at a
     time with the primary last, always with `--match-head-commit`;
   - remote effects run before tracker writes;
   - the `[factory]` prefix and the `Factory-Run:` trailer are added in one
     place;
   - every handler returns whether it wrote, and returns `False` the second
     time.

   Outcomes come only from the workflow; a feature may add a marker
   outcome (`ask`).
8. **Hook points** are the only places the core calls a plugin:
   `definition.load`, `definition.errors`, `definition.warnings`,
   `gate.step_context`, the `guards` of `gate.guard_holds`, `gate.outcome`,
   the effect registry, `deploy.schema`, `deploy.files`, `doctor.probes`
   and `reconcile.passes`.
9. **Core commands** stay in `cli.py`: `begin`, `end`, `check`, `apply`,
   `sweep`, `ci-sync`, `decide`, `ci`, `deploy`, `run`, `contracts`,
   `chart`, `migrate`, `upgrade`, `check-workflow`, `validate`, `retro`,
   `setup`, `env`, `doctor` and `reconcile`. Later slices move `ci-sync`,
   `run` and `deploy --target local` into their plugins. `cli` asks the
   registry for a word it does not know before it prints help.

## Consequences

`comments` is the first plugin. Its declaration names the `route`, `reply`
and `skip` effects on `ticket`, the `comment` effect `on: mention`, the
`mention` guard and trigger, the `ask` outcome, the `mentionable` agent
setting, and five hooks. The core no longer imports it, and
`gate.comments_on`, `gate.module_off`, `deploy.comments_on` and
`definition.module_on` are gone.

A team's `modules: [comments]` keeps working. The engine default is
`plugins: []`.

Until #1067 adds the effect registry with the core policies, the gate calls
a plugin's effect handler in the shape it had before: the comment node's
handler turns its effects into core effects, and an `on: mention` handler
rewrites one effect. Later slices move notify (#1066), GitHub (#1068),
YouTrack (#1069), Space (#1070), the local surface and agent runners
(#1071) and Air (#1072) onto the registry.

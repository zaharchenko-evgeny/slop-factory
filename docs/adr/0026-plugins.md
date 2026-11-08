# ADR-0026: Engine plugins: events in, records out, effects

Status: accepted, requested by #1064 (slices: #1065, #1066, #1067). Amends
ADR-0025 and ADR-0004.

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
       config_keys=(),# top-level factory.yaml keys only this plugin reads
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
   fact, an `on:` target (trigger), a marker outcome, an agent setting, a
   top-level `factory.yaml` key or a CLI word. Two owners of one key are an
   error when the registry loads.
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
   `gate.send`, the effect registry, `deploy.schema`, `deploy.files`,
   `doctor.probes` and `reconcile.passes`.
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

#1067 makes decisions 6 and 7 real for the gate. `factory/derive.py` derives
every fact from records (`factory/ports/records.py`). `factory/effects.py`
is the `(kind, target)` registry with the core policies: `close` before
`stage: done`, the merge `needs`, `primary_last`, `Effect.remote` in place of
`gate.REMOTE_FIRST`, `frame` for the `[factory]` prefix and trailer, and `mark`
for the team's node marker. `factory/gate_tracker.py` holds
`GateTracker(tracker_for_env, vcs_for_env)`, which reads records and applies
effects on the system of their target. `GitHubGateTracker` and `MemorySystem`
declare their handlers in `effects`. `gate_port.CodeHost` is now `Vcs`, with
`event(payload)`. `factory/tests/system_contract.py` is the contract each
system passes: records read back, and a second apply of every declared
`(kind, target)` returns `False`.

The gate is ready for scopes but wires one tracker. `Subject` and `Query`
carry an optional `scope`, in the system's own terms: `{label: ...}` on
GitHub, `{field: ..., value: ...}` on YouTrack. Every environment resolves to
the one top-level tracker (`GateTracker.single`), so no read is scoped yet.
Per-environment trackers pass a resolver and the ticket's environment.

Remaining gaps:
- A feature plugin's effects keep their own hook shape. The comments and
  notify handlers (`effects.plugin_handlers`) rewrite effects before the stop
  and human-attention rules see them. The `on: mention` handler
  (`effects.plugin_target`) rewrites one effect after `order`. `gate.send`
  sends only after the match check. Making them system effects would move
  them past those points, so they stay hooks.
- `gate.resolve` drops `on:` for route, reply, park-behind-main-red, stage,
  review and merge, so those target what their kind names.
- GitHub's `react` reports a write each time: the API returns the reaction
  that already exists.
- A single repository's `update-branch` does not read before it writes; it
  relies on GitHub refusing the second PUT.
- A system without `effects` keeps the old remote kinds (`effects.REMOTE`).

Later slices move GitHub (#1068), YouTrack (#1069), Space (#1070), the local
surface and agent runners (#1071) and Air (#1072) into plugins.

`notify` (#1066) is the second feature plugin. Its declaration names the
`notify` effect on `ticket` and `pr`, the `notify` CLI word
(`factory notify test <login>`), the `notify` config key, and the
`definition.errors` and `gate.send` hooks. Its effect handler reads the
message from the payload before anything is written; `gate.send` sends it
once the outcome is sure to apply, after the gate's match check. The core
posts the `[factory]` comments of the sends that went out even when one
failed, then raises. A channel is a separate plugin that provides a
`notifier`, the `ports.notifier.Notifier` slot (ADR-0022: `name`,
`validate`, `send`, optional `ack`); `notify_email` provides `email` and
requires `notify`. A `notify:` block, a `notify` effect or a notifier whose
plugin is off is `module-off`, so a team turns notify on with
`plugins: [notify, notify_email]` (`notify_email` alone turns on both).

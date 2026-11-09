# ADR-0026: Engine plugins: events in, records out, effects

Status: accepted, requested by #1064 (slices: #1065, #1066, #1067, #1068, #1069, #1070, #1071, #1072, #1073). Amends
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
   - every tracker named by `environments.<env>.tracker.adapter`;
   - the `surface:`;
   - the runner of every `models.<alias>.harness`;
   - the plugin of a surface, runner or development tool that a command uses
     (`use`): `deploy --target local`, `factory chart`;
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
   `doctor.probes` and `reconcile.passes`, plus the system setup hooks of
   #1068 (`deploy.labels`, `deploy.provision`, `deploy.secrets`, `deploy.run`,
   `deploy.plan_files`).
9. **Core commands** stay in `cli.py`: `begin`, `end`, `check`, `apply`,
   `sweep`, `decide`, `ci`, `deploy`, `migrate`, `upgrade`,
   `check-workflow`, `validate`, `retro`, `setup`, `env`, `doctor` and
   `reconcile`. `ci-sync` is the github plugin's command (#1068); `run` and
   `deploy --target local` are the local plugin's (#1071), `contracts` and
   `deploy --target air` the air plugin's (#1072), and `chart` the chart
   plugin's (#1073). `cli` asks the registry for a word it does not know
   before it prints help.

## Consequences

`comments` is the first plugin. Its declaration names the `route`, `reply`
and `skip` effects on `ticket`, the `comment` effect `on: mention`, the
`mention` guard and trigger, the `ask` outcome, the `mentionable` agent
setting, and four hooks (#1068 moved its `deploy.files` to github). The core
no longer imports it, and
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

`Subject` and `Query` carry an optional `scope`, in the system's own terms:
`{label: ...}` on GitHub, `{field: ..., value: ...}` on YouTrack. A team with
no environment tracker resolves every environment to the one top-level tracker
(`GateTracker.single`), so no read is scoped.

### Per-environment trackers

Amended by ADR-0027: a repository of an environment carries its own `tracker` and `vcs` (the
environment's blocks are its defaults), and a node names its `environments` instead of the default-environment
fan-out below.

A factory may serve several projects (#1069). Each project is an environment
with its own tracker, `environments.<env>.tracker`, in the same `factory.yaml`
and one deploy:

```yaml
tracker: {adapter: youtrack, url: https://yt.example, project: PRJ}
environments:
  backend: {repositories: [...], tracker: {adapter: youtrack, scope: {field: Subsystem, value: Backend}}}
  web: {repositories: [...], tracker: {adapter: github-issues, repository: o/web, scope: {label: "area:web"}}}
```

- **Merge.** An environment's block takes every unset key from the top-level
  `tracker` when the adapters match. With another adapter it takes only the
  factory's own keys (stages, labels), never the system's (`url`, `project`,
  `repository`, `token_env`, `done_state`, `repository_field`, `bot_logins`).
- **Scope.** `{label}` on github-issues: the ticket carries the label.
  `{field, value[, type]}` on youtrack: the custom field holds the value;
  `type` is the field's YouTrack type for a created ticket (default
  `SingleEnumIssueCustomField`).
- **Resolution** (`factory/scopes.py`, `ScopedTracker`). With environment
  trackers the gate's tracker and vcs are one `ScopedTracker`. Each ticket
  goes to its environment's tracker, built from the environment's descriptor
  (`gate.env_descriptor`): its merged tracker block, its repositories with
  the main one primary, and that main repository as the code host. A run in a
  project environment (`FACTORY_ENV`) takes only that environment's tickets.
  Elsewhere the ticket goes to the first environment whose tracker can hold
  its id (`PRJ-12` on YouTrack, a number on GitHub) and whose scope holds
  it. A PR's ticket is looked up on the vcs of the environment that lists the
  PR's repository, and that environment is tried first.
- **No environment.** A ticket inside no scope is left alone. `check` exits
  10 with `{"go": false, "reason": "skip: <ticket> outside every environment
  scope"}`, because `factory begin` reads exit 0 as go. `apply`, `sweep`,
  `decide`, `ci` and `wake-reviewers` print
  `{"skipped": true, "reason": "skip: ..."}` and exit 0. Nothing is written.
- **Queries.** A scoped GitHub search adds `label:"<label>"`, and a scoped
  YouTrack query adds `and <field>: {<value>}`. `sweep` and `sweep --stale`
  list each environment's tickets with its own query.
- **Created tickets.** Sub-issues and the `Main is red` ticket get the
  environment's label or field value.
- **Deploy.** A node of the default environment (`settings.environment`) gets
  one automation per project environment, named `<node> <env>`. It runs in
  that environment's Air environment (`FACTORY_ENV=<env>`) on its main
  repository. A YouTrack event trigger also filters on `<field>: <value>`.
  GitHub's `label` filter is the label the event adds, so a GitHub trigger
  has no scope filter and the gate's scope check stops another project's
  ticket. The knowledge review runs once. A node that names another
  environment is planned as before, and so is a team with no environment
  tracker: its plan is byte-identical.
- **Validation** (`scopes.errors`, from `factory validate` on `factory.yaml`
  and on the compiled descriptor). A selected plugin must provide the
  adapter; each environment tracker turns on its plugin. The scope keys must
  be the adapter's. Environments that share one tracker (adapter and
  repository, or adapter, url and project) must each have a scope, and no
  two scopes may be the same.

Limits: two GitHub environments in different repositories can both hold
number `n`; outside a project run, the first environment that holds it wins.
The Space vcs, `factory doctor` probes and YouTrack tag provisioning still
read the top-level tracker. A scope that two labels share cannot be checked
statically.

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

Later slices move YouTrack (#1069, below), Space (#1070), the local surface and
agent runners (#1071), Air (#1072) and the stage chart (#1073) into plugins;
`retro` stays in the core (#1073, below).

`github` (#1068) is the first system plugin. `factory_plugins/github/` holds
`gate.py`, `tracker.py` and `graphql.py` (formerly `factory/gate_github.py`,
`github_tracker.py` and `github_graphql.py`) and provides tracker
`github-issues` and vcs `github`. It is on by default: a config that names no
tracker or no vcs selects it. `vcs:` is the name of `code_host:`, which stays
as an alias. The core imports only its declaration; `gate.tracker_of(config)`
loads the implementation the registry names. Its hooks:
- `deploy.labels` and `deploy.provision` reconcile and write the repository
  labels of a `ports.system.TrackerSchema`. The core still computes the
  labels, so the plan keys stay unchanged; a plugin adds labels with
  `deploy.schema`. `factory:comment` stays a core label (ADR-0025).
- `deploy.secrets` adds `FACTORY_WRITE_TOKEN` under `write_mode: token`, and
  `deploy.run` gives the deploy its project-write `gh` runner.
- `deploy.plan_files` renders the Actions workflows of `runner: actions`
  nodes. `deploy.files` syncs the wake files of `wake_rules(features)`: the
  comment wake workflow and script are installed while `comments` is on and
  deleted from the project repositories once it is off. The comments
  plugin's own `deploy.files` hook is gone.
- `doctor.probes` runs the repository, wake-file and project-write probes.
- `reconcile.passes` runs the CI label sync, and `ci-sync` is its command.

The doctor's ticket filing and its frontier read still call `gh` directly.

`youtrack` (#1069) is the second system plugin. `factory_plugins/youtrack/`
holds `store.py` (formerly `factory/youtrack_tracker.py`) and provides
tracker `youtrack`; `tracker.adapter: youtrack` selects it. YouTrack holds no
pull requests, so `YouTrackTracker.from_config` builds the store from a
tracker config block and asks the selected vcs class for the gate tracker
over it (`GitHubCodeHost.gate_tracker`): the github plugin no longer imports
YouTrack code, and `gate.tracker_of` has no fallback. The store declares its
ticket effects (`stage`, `label`, `unlabel`, `set-field`) and its store kinds;
the gate tracker takes them from any store. `set-field` stays a
`<field>:<value>` tag, since the gate reads the attribute (the `risk` fact)
back from the tags. Its hooks: `deploy.plan` (the `tags` and `fields` plan
keys), `deploy.repo_tags` (the per-repository stage tags of the triggers),
`deploy.tracker` (the tag and field-value provisioner the deploy drives
between its label steps) and `doctor.probes`. The #892 comment wake rule had
not merged and was not ported.

`space` (#1070) is the third system plugin. `factory_plugins/space/gate.py`
(formerly `factory/gate_space.py`) provides vcs `space`; `vcs: space` (alias
`code_host:`), top-level or on an environment, selects it. `SpaceCodeHost`
reads a ticket's merge request as a `PrRecord`, declares `label`, `unlabel`,
`review`, `update-branch` and `merge` on `pr` as `effects`, and names the
facts it owns (`pr_facts`). Its `gate.trigger` hook reads a merge request
webhook in `gate.from_trigger`, so the core names no vendor. An environment's
vcs is `config.vcs(config, env)`: its own `vcs:` over the top-level one, keys
merged when the adapters match. A vcs that declares effects is put beside the
tracker (`gate_tracker.OnVcs`): its PR effects and PR reads go to it, its
facts replace the tracker's, and everything else stays the tracker's. GitHub's
vcs declares none, because its tracker hosts the pull requests. `factory
validate` reports a vcs that no selected plugin provides, and every node
effect on a PR that its environment's vcs does not declare. `ci-sync` stays
the github plugin's: a team whose vcs is another plugin's has none.

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

`local` (#1071) is the first surface plugin, and `claude`, `codex`, `junie`
and `pi` are the agent runner plugins. `factory_plugins/local/surface.py`
(formerly `factory/local.py`) provides surface `local` as `LocalSurface`
(`ports.surface.Surface`: `plan`, `read_state`, `diff`, `apply`, `outputs`)
and the CLI words `run` and `deploy --target local`, with the same flags and
exit codes. A command that uses the surface turns it on, as `surface: local`
does (`plugins.registry(config, use=...)`). Each runner provides `agent`
under its harness name (claude: `claude-code`, alias `claude`) as
`ports.agent.AgentRunner`: `cli`, `skill_dir`, `command(prompt, model,
write)` and a PATH `probe`. They replace the `HARNESS_CLI` and `SKILL_DIR`
tables and `harness_argv`. A role's harness (its mode's, the role's, its
tier's `models.<alias>.harness`, `targets.local.harness`, else `claude-code`)
turns its runner on when the role runs, and `models.<alias>.harness` turns it
on through `plugins.implied`. An unknown harness is `unknown-agent`, naming
the known ones; `deploy --target local` reports it as missing. The local
poll reads the trackers (`gate.tracker_of`, or each environment's tracker
with #1069 environments) through `find`, with three query kinds the GitHub
tracker adds: `labeled`, `prs` and `closed`. These make the same `gh` calls
the poll made: one paged search for every watched label, `/events` only for
an id that does not decode, and the `pr list` and `issue list` reads.

`air` (#1072) is the second surface plugin. `factory/deploy.py` keeps the
target-neutral plan: node specs, prompts, triggers, variants, the compiled
automation and the tracker labels. `factory_plugins/air/deploy.py` holds the
Air automation and environment shapes (`plan_air`), the live state, diff and
apply through the `Mcp` client and the REST `Backend`, and the secret and
variable checks. `factory_plugins/air/contracts.py` (formerly
`factory/contracts.py`) serves and refreshes the recordings, which stay in
`factory/tests/contracts/` beside the GitHub ones the tests read. `AirSurface`
provides surface `air`; the CLI words are `deploy --target air` and
`contracts`. `factory deploy` with no `--target` runs the deploy of
`surface:`, else of `air` as before (`plan` is always Air's). A surface
answers `outputs()`: Air the five `outputBehaviour` values of its contract,
`local` `read-only`, `create-pr` and `push-to-source`. `factory validate`
reports `unsupported-output` for a deployed node whose `output:` the
surface (`surface:`, else every selected surface once `targets.air` is set)
does not support; a team with neither is not checked. Validation imports only
the surface class, never the Air deploy code.

### chart and retro

#1073 decides the last two commands.

- **`chart` is a feature plugin.** It is development tooling: it renders the
  stage statechart, the Mermaid chart and node table of `docs/statechart.md`
  and `docs/statechart-proposed.md`, and no node, gate step or deploy reads
  it. `factory_plugins/chart/render.py` holds the code that was in
  `factory/nodes.py` (`chart`, `table`, `block`, `trigger_text`, `writes`,
  `planned`), and the plugin adds the CLI word `chart`. A team need not list
  it: like a surface, the command that uses it turns it on
  (`plugins.registry(config, use=(("tool", "chart"),))`), so `factory chart`
  and `uv run factory/nodes.py chart` print the same output as before, and
  `plugins: [chart]` is accepted but changes nothing. Without the command
  the core imports only its declaration; `nodes.py validate` never loads the
  renderer. The generated docs blocks did not change.
- **`retro` stays in the core.** It collects the lessons every role writes
  (ADR-0021): the gate turns a result's `lessons` into one marked comment and
  the lesson label after every outcome it applies (`gate.lesson_effects`),
  and `retro collect` reads those comments back. Moving retro into a plugin
  would need a core-to-plugin hook for that `lesson` effect on every applied
  outcome, while the lessons are written whether or not a team runs the
  retro node. `factory retro`, `retro.py` and the `retro` node are unchanged.

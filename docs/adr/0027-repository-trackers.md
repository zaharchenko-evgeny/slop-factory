# ADR-0027: Environments declare each repository's tracker, vcs and write; nodes declare environments

Status: accepted, decided by the operator on 9 November 2026 (#1132). Supersedes
ADR-0024. Amends ADR-0026 "Per-environment trackers" and ADR-0017 "One branch
name across repositories".

## Context

ADR-0024 gave nodes `listens` and `writes`, kept agent `access`, and added
`tracker.repositories`. #1069 and #1070 added an environment's own `tracker` and
`vcs`, and a node of the default environment fanned out once per project
environment. The gate still reads a ticket's PR repositories from a
`repositories:` body line (ADR-0017). These overlap, and they do not describe a
setup with several environments, each with its own GitHub repositories and
YouTrack subsystems.

Facts that bound the design:

- Air wakes an automation only on GitHub events of its main repository (#977,
  probe A). YouTrack events reach any automation, filtered by the trigger's
  project and custom-field value.
- Air pushes only to an automation's main repository.
- Space merge request events arrive by webhook (the space plugin's
  `gate.trigger` hook).

## Options

1. Keep ADR-0024 (`listens`, `writes`, `access`) and the #1069 fan-out.
2. A repository carries its tracker, vcs and write; a node names its
   environments, and deploy derives the copies.
3. One tracker per environment, with repositories mapped by labels.

## Decision

Option 2.

1. **Repositories.** `environments.<env>.repositories` is a list of
   `{repo, write, tracker, vcs}`:
   - `write`: whether runs may push to it (default `false`);
   - `tracker`: an adapter block, or `none` (context only, no tickets);
   - `vcs`: an adapter block;
   - `token`: the name of the secret that holds the gate's write token for
     this repository (issues, labels, comments, sub-issue and blocked-by
     links). Air's token has no issues permission on another repository, even
     one the run checks out, so a link between issues of two repositories
     needs it. A repository with a `token` is written with it; one without is
     written from its checkout's directory with Air's token (#1246), and the
     environment's own repository (`FACTORY_ENV_REPOSITORY`) always with Air's
     token. Deploy adds each `token` secret to its environment. This replaces
     the removed `token_env` key; the tracker-wide `issues_token_env` setting
     still works as every repository's token until a repository declares its
     own, and `factory validate` warns about it (`deprecated-key`).

   The blocks layer as top-level `tracker`/`vcs`, then the environment's, then
   the repository's own. A narrower block with another adapter keeps only the
   factory's keys (ADR-0026). A repository never takes its tracker's place keys
   from the top level; the tracker contract names them (`LOCAL_KEYS`). So
   without its own block, a repository's tickets are its own GitHub issues.
2. **One writable repository per environment on Air.** More than one
   `write: true` fails the Air deploy (#1134). The local target allows several.
   `factory validate` warns with `several-writable`.
3. **Nodes.** A node declares `environments: [..]`. `environment` is the
   one-environment alias, and the workflow's `settings.environment` is the
   default. A generated node (comments' `reply-<agent>`) takes the environments
   of its agent's nodes.
4. **Copies** (`definition.copies`). For each environment of the node:
   - An event node gets one copy per repository it listens to: the writable
     repository, or every repository with a tracker when none is writable.
   - A node with only schedule, webhook or manual triggers gets one copy.
   - A copy's main checkout is where its events happen: the tracker's `home`
     (GitHub Issues: the issues' repository; YouTrack: none, so the repository
     itself). A scheduled copy runs in the writable repository, else the
     environment's first.
   - `write` says whether the copy may push there. The node's `output` decides
     whether it does.
5. **Plugin contracts** (`ports/place.py`). A plugin provides a light
   `tracker-contract` and `vcs-contract` per adapter, next to its runtime
   `tracker` and `vcs` classes:
   - tracker: `place(block, repo)`, `home(block, repo)`, `check(block, where)`
     and `LOCAL_KEYS`;
   - vcs: `home(block, repo)` and `check(block, repo, where)`.

   The engine reads only `adapter` and asks the contract, so it names no
   system. github provides `github-issues` and `github`, youtrack provides
   `youtrack`, and space provides `space`. A repository's adapters turn their
   plugins on.
6. **Labels.** Labels keep one name everywhere. The gate labels a ticket in its
   own place, and each copy hears only its place.
7. **Gate** (#1135). One ticket, one repository, one PR. A ticket resolves to
   its repository by tracker place. A split creates each child in the
   repository it names through `create-issues`. The `repositories:` body line
   goes.
8. **Removed.**
   - Deprecated (warnings) until the team migrates (slop-eval-factory#92),
     then removed: node `listens` and `writes`, agent `access`,
     `tracker.repositories` and `tracker.projects`.
   - Already gone: environment `main` and `token_env`. Deprecated: the
     `issues_token_env` setting (`token` on a repository replaces it).
   - The #1069 default-environment fan-out goes in #1134.

## Checks (`factory validate`)

The repository checks run on a definition that uses this model (a repository's
`write`, `tracker` or `vcs`, or a node's `environments`):

- `bad-write`: `write` is not a boolean.
- `unknown-tracker` / `unknown-vcs`: no selected plugin provides the adapter.
- The contracts' own checks: `youtrack-project`, `bad-scope`, `space-vcs`.
- `place-in-two-repositories`: two repositories share a tracker place. One
  YouTrack project needs a `scope` per repository.
- `node-hears-twice`: two environments of one node listen to one place, so one
  ticket would start two runs.

Validating a definition in the older form imports no plugin code.

## Consequences

- A team declares where tickets and code live once, per repository. Adding a
  tracker or vcs is a plugin with its contract.
- Automations multiply as node × environment × listened repository. Slop
  (`engine`, `team`, `intake`) has about 40, as it has today.
- Splitting work across repositories means separate tickets with blocked-by
  links. One run never pushes to two repositories on Air.
- Open: whether the Air YouTrack trigger filters on a custom field. If it does
  not, the youtrack plugin's trigger narrows by a suffixed tag (#1134).

## Rejected options

- **Option 1.** `access` repeats what `write` and `output` say, and `writes`
  was also used to pick a scheduled node's checkout. `listens` cannot express
  YouTrack subsystems.
- **Option 3.** A label per repository on one tracker does not wake a copy
  whose main repository is another one (#977).
- **Suffixed wake labels routed from one tracker (ADR-0024).** For GitHub, Air
  cannot deliver another repository's events.

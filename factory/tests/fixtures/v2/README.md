# Slop factory, definition v2 (draft)

A draft of this factory in the proposed format. Nothing reads it yet; the live
factory still runs from `factory.project.yaml` and `workflows/delivery.yaml`.

## Layout

```
factory.yaml            what the factory has: tracker, models, skills, roles, agents, environments, webhooks, defaults
workflow.yaml           how it works: stages, shared settings (limits, default environment), nodes in delivery order
roles/<role>/role.md    the role's instructions (Jinja), no YAML
roles/<role>/setup.sh   optional; installs the role's tools
roles/<role>/skills/    optional role-only local skills
skills/<name>/          optional local shared skills
roles/<role>/...        any other file the role needs
roles/_common.md        rules every role includes
```

An agent in `factory.yaml` is a role + a model + access + settings, so one role serves
several agents (`dev-cheap`, `dev`, `dev-strong`). `gate` (a script) and `human` have no role.
An environment has its repositories (one `main`), domains, env and an optional setup
script, which installs its tools; the engine's own bootstrap (install the pinned engine, fetch the config at the deployed
SHA, render roles) runs first, then the environment's setup, then each role's.

Skills are defined once in `factory.yaml` `skills`: local (`path` in this repository) or from a
source (`github:owner/repo@commit` or `npm:package@version`, plus `path` inside it and `sha256`).
`defaults.skills` and each role's `skills` list names from there; a role can also define a skill
inline, the same way, for itself only. The engine's `factory-step` skill comes with the engine.
Setup installs the skills of each agent's role and checks every sha256.

Model routing is explicit: `start` picks `implement-cheap` or `implement` by the ticket's
complexity, a developer's `done-full` or `done-cheap` picks the review, and `error` or `escalate`
adds the next node's label. An Air automation has one fixed model, so there is no router.

## Rules

- A node is an agent + environment + triggers + outcomes. Its agent reports one
  outcome; the gate applies that outcome's effects.
- Settings, models and env vars merge factory.yaml defaults -> environment -> role -> agent ->
  workflow.yaml settings -> node. A narrower layer
  overrides a value; `null` clears it.
- Several triggers on one node are OR. Air matches one label per trigger.
- `if` on an outcome is a guard the gate checks against ticket and PR fields
  (`stage`, `risk`, `complexity`, `ci`, `blockers`). When it fails, the gate applies
  `else` instead.
- Jinja renders once, in the engine's bootstrap, from the definition at the
  deployed SHA (`FACTORY_CONFIG`). Runtime data (the ticket, the PR) is never
  templated; the agent reads it with `factory begin`. Secret values never reach
  a template.
- The handoff schema is generated from the node's outcomes and the fields their
  effects read (`comment: questions` needs `questions`). There are no schema files.
- Project checks (lint, tests) belong to the project's own CI. The factory reads
  CI only as a state: `ci: failed` wakes a node (Air `workflow_run.failed`), and
  `merge` checks green CI itself, woken again by `reconcile`.

## Knowledge

1. Any node may return `lessons` in its result. The gate posts them as a comment on the ticket
   (marker `<!-- factory:lesson -->`) and adds the label `factory:lesson`.
2. `retro` runs nightly in the `knowledge` environment, where the knowledge base is the main
   repository. It lists tickets with the label, removes the label, takes the uncollected lesson
   comments, marks them collected, adds the run facts, and merges everything into `lessons/`.
   The platform opens one PR.
3. `review-knowledge` reviews the PR; a person accepts it (`accept-knowledge`, not deployed).
   A later node can accept it automatically.
4. Every environment that lists the knowledge base as a repository lets its agents read
   `lessons/_shared.md`, `lessons/<repo>.md` and `lessons/agents/<agent>.md` (see `roles/_common.md`).
   They read the base's current state at run time, not the deployed SHA.

The layout and the 150-line cap per file are in the retro agent's instructions. Retro never
writes project repositories.

## Where each current setting went

| Current | v2 |
|---|---|
| `engine_repository`, `team_repository`, `repositories[].role`, `write_mode` | `engine:` pin; the main repo is `main: true` in an environment; extra writes use `token_env` |
| `targets.air.models` tiers | `models:` aliases (`luna`, `sonnet`, `sol`); agents `dev-cheap`, `dev`, `dev-strong`, `reviewer`, `reviewer-cheap` |
| `roles:` + `agents:` (tier, modes, fallback) | `agents:` in factory.yaml (role + model); review modes are nodes |
| `skills.lock`, `roles/<r>/role.md`, `skills.txt`, `setup.sh`, `handoff.schema.json` | `skills:` and `roles:` in factory.yaml + `roles/<r>/role.md` and optional `setup.sh`; no schema files |
| environments `read`, `write`, `ops` (factory.yaml) | `engine` (slop-factory main, slop-eval-factory writable) and `team` (slop-eval-factory main) |
| `environments/setup.sh`, `environments/write/setup.sh`, `.air/cloud/startup.sh` | the engine's bootstrap, then the environment's `setup` script, then each role's `setup.sh` |
| `gates.auto_merge` | node `merge` settings |
| `gates.ticket_approval` | removed: a person moves a ticket to `triage` (node `human`) |
| `limits.fix_rounds` | node `fix` settings |
| other `limits` | workflow.yaml `settings.limits` |
| `branch_pattern`, `commit_prefix` | factory.yaml `defaults.settings` |
| `reviews.trusted_bots` | removed: `join` counts agent reviews and people with write access; other bots never block |
| `knowledge`, distill | see Knowledge below; `distill` is merged into `retro` |
| `notify.github_user` | node `notify` settings |
| `checks` | removed: the engine's own CI runs them |
| `states:`, `commands:` | removed: stage names come from the nodes; labels are `factory:<stage>` |
| `implement-<tier>`, `fix-<tier>`, `update-<tier>` | nodes `implement-cheap`, `implement`, `implement-strong`, `fix`, `update`, `update-strong` |
| `review-<focus>`, `rereview-<focus>`, focus required by risk | `review-full` (sol: full, structure-security) and `review-cheap` (luna: behaviour), chosen by the developer's `done-full` / `done-cheap`; `done-cheap` has a risk guard |
| `factory:ci-green`, `factory:ci-red` labels | removed: `ci: failed` trigger and `merge`'s own CI check |
| `doctor`, `doctor-write`, `doctor-ops` | `doctor-engine`, `doctor-team` |
| `pilot`, `ticket`, `ci-independent`, `tiers`, `focuses` | removed; only `enabled: false` remains |
| `judgments/`, `policies/`, approver role | not used by any node; left out |

## Open points

- Air has no CI-success trigger, so `merge` waits up to one `reconcile` interval
  (15 minutes). A feature request for `workflow_run.succeeded` is still to be drafted.
- `investigate` and `adopt` stay disabled, as today.

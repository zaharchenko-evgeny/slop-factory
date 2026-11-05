# Slop factory

Slop factory is a reusable engine for delivering GitHub Issues as reviewed pull
requests. This repository contains the engine, setup skill, and deploy tooling.

## Install

Install the engine as a `uv` tool:

```sh
uv tool install git+https://github.com/zaharchenko-evgeny/slop-factory@v0.1.0
```

After this change merges, the operator tags the merge commit `v0.1.0` and
pushes the tag; the tag is not created by this change.

## Use

`factory --help` lists the available commands. The subcommands are `begin`,
`end`, `check`, `apply`, `sweep`, `deploy`, `contracts`, `chart`, `validate`, `migrate`, `upgrade`, and `doctor`.
`begin` starts a node and prints its handoff path; `end` applies the completed
handoff. `check` and `apply` operate the gate, while `sweep` runs its event
processing. `deploy` creates Air automations, `chart` renders the workflow,
`validate` checks configuration, and `doctor` diagnoses the local setup.
`contracts refresh` re-records the Air service contracts and is operator-only.

## Team configuration

Each team keeps settings in `factory.descriptor.yaml`. The descriptor supports
these top-level keys, defined by `engine/descriptor.schema.json`:
`engine`, `project`, `repositories`, `branch_pattern`, `commit_prefix`,
`team_repository`, `tracker`, `code_host`, `notify`, `targets`, `models`, `roles`,
`limits`, `gates`, `ci`, `knowledge`, `nodes`, `reviews`, and `checks`.

### Project descriptor

A project can keep its settings in `factory.project.yaml` and the workflow file
it links with `workflow:` instead (ADR-0016, defined by `engine/project.schema.json`
and `engine/workflow.schema.json`). States and commands name their tracker
store; the workflow names only states and commands. When the team root has
`factory.project.yaml`, the engine reads only that file and its workflow.
`factory migrate workflow <nodes.yaml> <factory.descriptor.yaml> --out DIR`
converts the old pair. `repositories[]` marks exactly one repository
`role: primary` and the others `role: project`; `targets` takes `air` and
`local` (ADR-0017). A new project starts from the engine's
[`template/`](template/README.md); `factory upgrade --diff` shows what changed
in it since the project's `engine` version, and `factory upgrade --apply PATH`
takes one file.

### Team-owned runtime files

Runtime `roles/`, `skills/`, workflows, policies, and judgments come only from
the team's repository. `template/roles/` and `template/skills/` are setup
examples, not runtime fallbacks; the engine's node graph is `engine/nodes.yaml`.

An environment can also define a `setup.sh` in the team's `environments`
directory. It runs for every environment, followed by the `setup.sh` in the
subdirectory named by `FACTORY_ENV` when that file exists. Both run with `bash`
after the factory and skills are installed; a failing hook stops startup.

## This repository

This repository is the engine and deploy package only. The Slop factory
configuration and runtime files live in the separate settings repository.

Slop factory's settings live in
[slop-eval-factory](https://github.com/zaharchenko-evgeny/slop-eval-factory):
a change to its workflow, states, roles or skills goes there. CI checks the
migrated copy in `factory/tests/fixtures/slop/` (`test_slop_plan_unchanged`).

See [factory setup](skills/factory-setup/SKILL.md), [design](docs/design.md),
[environment variables](docs/environment.md),
and the [delivery template](template/workflows/delivery.md) for more detail.

## Attention

Find every open ticket or pull request waiting for a person with
`is:open label:factory:needs-human`.

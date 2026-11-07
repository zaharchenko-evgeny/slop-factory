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
`team_repository`, `tracker`, `code_host`, `owner`, `notify` (v1: `github_user`), `targets`, `models`, `roles`,
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
`local` (ADR-0017). A new team starts from the factory-setup skill's
[`skills/factory-setup/template/`](skills/factory-setup/template/README.md), in the layout of ADR-0021
(`factory.yaml`, `workflow.yaml`, `roles/`). The old layout's template is
[`engine/template-v1/`](engine/template-v1/README.md); `factory upgrade --diff` shows what changed
in it since the project's `engine` version, and `factory upgrade --apply PATH`
takes one file.

`write_mode` says how the factory writes to the `role: project` repositories.
`per-repo` (the default) deploys the write nodes once per repository. With
`token`, `factory deploy` keeps one node set and adds the secret
`FACTORY_WRITE_TOKEN` to the `write` and `ops` environments, never to `read`.
A person sets it in Air to a fine-grained personal access token with Contents,
Pull requests and Issues write on every project repository. Air's own token
can write only the factory repository. While the token is set, each `gh` write
that names a project repository runs with it as `GH_TOKEN`, for example a CI
label or a merge. In `write` and `ops`, `factory doctor` runs one
`write:label:<owner/repo>` probe per project repository and reports `fail`
when the token is missing in `token` mode.

### Team-owned runtime files

Runtime `roles/`, `skills/`, workflows, policies, and judgments come only from
the team's repository. `engine/template-v1/roles/` and `engine/template-v1/skills/` are setup
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
a change to its definition (`factory.yaml`, `workflow.yaml`, `roles/`; ADR-0021) goes there. CI checks the
copy of those files in `factory/tests/fixtures/slop/`. The copy may differ from
`engine/nodes.yaml`. To regenerate it, run this from a clean slop-eval-factory
checkout at the commit to copy:

```bash
uv run factory/cli.py migrate project <checkout> --out factory/tests/fixtures/slop
```

The command validates the definition, copies its files byte for byte and writes the
commit and the team's URL to `factory/tests/fixtures/slop/SOURCE`. CI checks out that commit and
`test_slop_fixture_is_migrate_project_output` fails when the fixture differs
from a fresh copy. To run that test locally, set `FACTORY_SLOP_TEAM=<checkout>`.

See [factory setup](skills/factory-setup/SKILL.md), [design](docs/design.md),
[environment variables](docs/environment.md),
and the [delivery template](engine/template-v1/workflows/delivery.md) for more detail.

## Attention

Find every open ticket or pull request waiting for a person with
`is:open label:factory:needs-human`.

# ADR-0016: A project descriptor and a workflow per factory project

Status: proposed, decided by the operator on 3 November 2026. Builds on ADR-0014
(the node model) and ADR-0015 (the engine package and team descriptor). It
supersedes the parts of ADR-0014 and ADR-0015 that define `workflows/nodes.yaml`
as the project workflow and `factory.descriptor.yaml` as the project's
configuration.

## Context

ADR-0015 separates shared engine machinery from team settings, but its team
descriptor still mixes project identity and workflow configuration. A factory
project should own its tracker vocabulary and its workflow, while the engine
provides reusable machinery and a template project. The project configuration
must describe state and commands without binding the engine to GitHub labels,
YouTrack fields, or Air event names.

## Decision

Each factory project has a `factory.project.yaml` descriptor and one or more
workflow files. The descriptor identifies the project and links the workflow
used for a run with `workflow: workflows/<name>.yaml`. Several workflows per
project may be supported later.

The descriptor declares:

- **`states`**: named states, each with a `kind` of `active`, `waiting`, or
  `final`, and a `store` that identifies its source. A store can be a ticket
  label, a ticket field value (such as a YouTrack status), or a PR fact. PR
  states use the same declaration model as ticket states; supported facts
  include `pr: open`, `approved`, `merged`, `ci-green`, `ci-red`, and
  `conflicting`.
- **`commands`**: named requests that wake a node, each with a `store` such as
  a ticket label, PR label, or YouTrack tag.
- **`environments`**: named environments with their repositories and setup
  scripts, stored at `environments/<name>/setup.sh` (see #406).

The separate workflow describes how the factory works using state and command
names only. It does not contain tracker labels, field values, tags, or Air
trigger names. Adapters translate declared state and command names to those
external representations and to Air triggers. The engine therefore contains
the gate, adapters, node and effect types, reconciler, and deploy machinery,
but no project-specific labels or GitHub event names.

Each project has its own factory repository. The Slop factory project will use
`slop-eval-factory`; `slop-factory` remains the engine and supplies the template
project. Engine upgrades are explicit: `factory upgrade --diff` compares a
project with the template and presents changes for review. An engine upgrade
never silently overwrites project files.

## Consequences

- Project state and commands can be mapped to different trackers without
  changing the workflow or engine.
- PR lifecycle states are explicit alongside ticket states, so node guards
  refer to declared state names rather than tracker-specific facts.
- The project descriptor and workflow are reviewable configuration owned by
  each project; upgrades can be inspected before adoption.
- ADR-0014's node model remains the engine execution model. This decision
  changes where project workflow and state vocabulary are declared, not how a
  node runs.

## Rejected options

- **Keep `factory.descriptor.yaml` and `workflows/nodes.yaml` as the project
  contract.** This leaves project tracker vocabulary and workflow structure
  coupled to the shared engine configuration.
- **Put tracker labels or event names in workflows.** That would make each
  workflow depend on a particular tracker and trigger provider.
- **Upgrade projects silently from the engine template.** This hides changes
  to project-owned behavior and prevents operators from reviewing them.

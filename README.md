# Slop factory

A software factory that delivers GitHub Issues as reviewed pull requests, plus
the skill that generates it.

## What is here

- `skills/factory-setup/`: the reusable skill that sets up a factory.
- `factory.yaml`: repositories, tracker stages, models, gates, limits, checks.
- `roles/`: router, developer and reviewer role files, each with a `setup.sh`.
- `workflows/delivery.md`: the delivery workflow.
- `.agents/skills/factory-delivery/`: runs that workflow locally with `pi`.
- `docs/design.md`, `docs/plan.md`, `docs/adr/`: design, plan, decisions.

Project rules are in `AGENTS.md`.

## Run the factory

The factory runs through Air automations. See the
[factory delivery skill](.agents/skills/factory-delivery/SKILL.md) for the
local delivery workflow and [factory deployment skill](.agents/skills/factory-deploy/SKILL.md)
for deploying the nodes to Air Cloud.

## Approval gate

`gates.confirm_external_writes` is `true` in `factory.yaml`. Every write kind in
`gates.confirm_external_writes_scope` (today: none) is shown to you and waits
for your approval. Pushing the ticket's own branch, issue and PR text, comments
and label changes are auto-approved, so a run can go unattended. Merging stays
with you.

## More

- [Targets: Air Cloud](docs/targets.md)
- [Delivery state chart](docs/statechart.md) and the [proposed whole-factory chart](docs/statechart-proposed.md)
- [Recipe: auto-approve and auto-merge](docs/recipes/auto-approve.md)

- [Design](docs/design.md)
- [Plan](docs/plan.md)
- [Delivery workflow](workflows/delivery.md)

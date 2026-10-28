# Offline Evaluation factory

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

## Run the factory locally on a ticket

1. Check the tools: run each `roles/<role>/setup.sh` (needs `pi`, `gh`, `git`).
   A failure stops the run.
2. Put the `factory:ready` label on the issue. That label starts a run.
3. Ask `pi` to deliver the ticket with the `factory-delivery` skill, for
   example "deliver ticket 13".
4. The skill follows [workflows/delivery.md](workflows/delivery.md): router
   intake, developer in `.factory/worktrees/<n>`, pre-review checks from
   `factory.yaml`, an independent reviewer, fix rounds, then the PR.

Run files stay in `.factory/` (git-ignored).

## Approval gate

`gates.confirm_external_writes` is `true` in `factory.yaml`. Every push, PR,
comment and label change is shown to you and waits for your approval.

## More

- [Design](docs/design.md)
- [Plan](docs/plan.md)
- [Delivery workflow](workflows/delivery.md)

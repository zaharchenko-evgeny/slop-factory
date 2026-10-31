# ADR-0014: The node model

Status: accepted when #148 merges. Supersedes the mechanism of ADR-0013 and of `docs/statechart-proposed.md`. The states and outcomes in both stay.

## Context

The transition table (`delivery.sm.yaml`, vars, intents, command labels, leases) is hard to deploy to Air. Every Air automation has one kind of start (one or more triggers) and AND-only filters. The factory is rebuilt as a list of nodes that run the same way locally and on Air. The pilot: the current local factory delivers the new engine (#21). The target chart and node table are on #149.

## Decision

- **Node** = one or more triggers + one condition + a role, model and environment + outcomes. One node is one Air automation (`[factory:<node>]`), or one entry of the local loop. A node may have several triggers: `review-<focus>` triggers on `pull_request.opened` and `synchronize`, so an adopted PR and a pushed fix both start a reviewer.
- **Events wake, conditions decide.** A trigger is a coarse pre-filter. `gate check` evaluates `when` over fresh facts. A duplicate or stale event fails the condition and does nothing (AIRA-727).
- **Facts come from adapters**: the ticket (GitHub Issues or YouTrack: stage, fields, labels, blockers) and the PR (GitHub: state, head, CI, reviews by head SHA). There are no hidden vars. Counters are derived from facts; for example `fix_rounds` is the number of bot request-changes reviews.
- **An outcome is an ordered list of idempotent effects** that `gate apply` runs: set stage, add or remove a label, comment, PR review, create child issues, merge. The stage change goes last. The agent only picks the outcome and writes its payload.
- **The join is an effect.** Each reviewer's apply re-evaluates "all reviews of the current head approve and CI passed".
- **Environment per node**: repository, the env config (secrets, domains, VM), and `.air/cloud/startup.sh` in the repository. `FACTORY_ENV` selects the branch in the script.
- **Schedules**: the sweep and the routines are cron nodes.
- **Merging is serialized by the sweep**: one merge train, using `--match-head-commit`.
- The state chart is derived from the nodes, not written by hand.

## Rejected options

- **Transition table with vars** (ADR-0013 mechanism): hidden state (vars, leases, command labels) cannot be expressed as one trigger plus AND filters, and drifts from the tracker.
- **Air Workflows**: a per-ticket loop in one run, rejected in ADR-0013 for the same reasons (the bot cannot approve its own PR, output only after the run ends).
- **Air Factory**: re-check once its config-as-code exists. Until then it cannot be reviewed or versioned in this repository.

## Air facts relied on

- A label set by an automation starts the next automation within seconds (#4, #100).
- An automation can have several triggers; filters combine with AND only, so conditions live in `gate check`.
- Duplicate and stale events happen (AIRA-727); the design absorbs them.
- `push-to-source` pushes only after the run ends.
- The bot cannot approve its own PR; `read-only` limits version control only, not labels, comments or reviews.
- Each node starts a new environment (about one minute); the environment is set per automation through the env config and `.air/cloud/startup.sh`.

## Consequences

- Local and Air run the same node code; locally `drive.py loop` evaluates each node's condition instead of waiting for events.
- `docs/statechart.md` and the delivery chart become generated views of the node list.

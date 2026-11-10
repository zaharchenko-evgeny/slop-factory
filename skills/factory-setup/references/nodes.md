# Node types

Each node of `template/workflow.yaml` is an agent, an environment, triggers and outcomes. The agent
reports one outcome; the gate applies its effects. Nodes with agent `clerk` run no model: the node's
guards choose the outcome. `tests/test_templates.py` fails when a node has no row here.

## `listens` and `writes`

ADR-0024. An environment lists checkouts and has no `main`. Two keys on a node say how it uses them:

- `writes`: the repositories the node writes, `all` or a list. A node with `writes` runs one Air automation per written
  repository; Air's platform push writes there. Without `writes` the node writes nothing.
- `listens`: the ticket repositories the node hears: `tickets` (default, every ticket repository), `tracker` (the
  first) or a list. A node with no ticket trigger (schedule, webhook, manual) listens to nothing.

A node that listens to more than one repository gets one automation per repository, named `<node> <repo-name>` in the
deploy plan (for example `implement-medium api`). With one repository the name is just `<node>`.

In the template, the developer nodes (`implement-*`, `fix`, `update`) have `writes: all`. The router, review, `automerge`,
`merge`, `finish` and `notify` nodes write nothing. `retro` writes only the team repository. A scheduled node
writes at most one repository. The generated README lists each repository with the automations that run in it.

| Node | Purpose | Trigger | Outcomes |
| --- | --- | --- | --- |
| `human` | A person resumes a ticket from any stage but `done`. | manual | `triage`, `ready` |
| `intake` | Scopes a ticket, sets size and risk, or asks, splits or rejects it. | stage `triage` | `small-low`, `small-high`, `medium-low`, `medium-high`, `large-high`, `ready`, `grill`, `split`, `not-actionable` |
| `dispatch` | Estimates a ready ticket without a size and starts the developer it needs. | stage `ready`, blockers closed | `small`, `medium`, `large` |
| `implement-small` | Developer on the cheap model. | label `factory:implement-small` | `done-low`, `done-high`, `error` (retries as medium), `blocked` |
| `implement-medium` | Developer on the mid model. | label `factory:implement-medium` | `done-low`, `done-high`, `error` (retries as large), `blocked` |
| `implement-large` | Developer on the strong model. | label `factory:implement-large` | `done-low`, `done-high`, `blocked` |
| `review-low` | One light review of a low-risk PR. The body has `## Summary`, `## Walkthrough` and `## Merge danger`. | label `factory:review-low`; CI may still run | `approve-low`, `approve-risky` (never on red CI: `changes` instead), `changes` |
| `review-high` | Two focused reviews of a high-risk PR. The body has `## Summary`, `## Walkthrough` and `## Merge danger`, and `## Decision for you` first for `approve-risky`. | label `factory:review-high`; CI may still run | `approve-low`, `approve-risky` (never on red CI: `changes` instead), `changes` |
| `ci-failed` | Sends an approved PR whose CI finished red to the developer; an approval on red CI is already turned into `changes`. | label `factory:ci-red`, stage `approved` | `fix` (CI is red), `waiting` (CI is green or still running) |
| `fix` | Developer fixes review findings or red CI and pushes a new head. | label `factory:fix` | `done-low`, `done-high`, `blocked` |
| `update` | Developer resolves a merge conflict with `main`. | label `factory:update` | `resolved`, `unresolvable` |
| `automerge` | Decides whether an approved PR merges alone or goes to a person. | label `factory:automerge` | `merge` (risk low), `needs-human` |
| `merge` | Merges an approved head with green CI, updates a branch that is behind, or flags a conflict. | label `factory:merge` | `conflict`, `updated-low`, `updated-high`, `merged`, `ci-red` (a fix round), `waiting` |
| `finish` | Marks a ticket `done` after its PR merges or its children close. | PR merged, ticket closed | `done`, `children-done`, `waiting` |
| `notify` | Tells the owner about a state that needs them. | stage `blocked` or `needs-answers`, label `factory:needs-human` | `notified`, `skipped` |
| `retro` | Collects lessons from finished tickets into one knowledge-base PR. | schedule, daily | `proposed`, `nothing` |
| `knowledge-review` | Reviews the retro PR. | label `factory:knowledge-review` | `approve`, `changes` |
| `accept-knowledge` | A person accepts or rejects the retro PR. | manual | `accepted`, `rejected` |
| `reconcile` | Re-fires missed wakes and polls CI for merge. | schedule, every 15 minutes | `reconciled` |
| `doctor-main`, `doctor-team` | Probe the main and team environments. | schedule, daily | `probed` |

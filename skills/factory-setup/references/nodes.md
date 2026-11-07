# Node types

Each node of `template/workflow.yaml` is an agent, an environment, triggers and outcomes. The agent
reports one outcome; the gate applies its effects. Nodes with agent `clerk` run no model: the node's
guards choose the outcome. `tests/test_templates.py` fails when a node has no row here.

| Node | Purpose | Trigger | Outcomes |
| --- | --- | --- | --- |
| `human` | A person resumes a ticket from any stage but `done`. | manual | `triage`, `ready` |
| `intake` | Scopes a ticket, sets size and risk, or asks, splits or rejects it. | stage `triage` | `small-low`, `small-high`, `medium-low`, `medium-high`, `large-high`, `ready`, `grill`, `split`, `not-actionable` |
| `dispatch` | Estimates a ready ticket without a size and starts the developer it needs. | stage `ready`, blockers closed | `small`, `medium`, `large` |
| `implement-small` | Developer on the cheap model. | label `factory:implement-small` | `done-low`, `done-high`, `error` (retries as medium), `blocked` |
| `implement-medium` | Developer on the mid model. | label `factory:implement-medium` | `done-low`, `done-high`, `error` (retries as large), `blocked` |
| `implement-large` | Developer on the strong model. | label `factory:implement-large` | `done-low`, `done-high`, `blocked` |
| `review-low` | One light review of a low-risk PR. The body has `## Summary`, `## Walkthrough` and `## Merge danger`. | label `factory:review-low` or `factory:ci-green`, CI green | `approve-low`, `approve-risky`, `changes` |
| `review-high` | Two focused reviews of a high-risk PR. The body has `## Summary`, `## Walkthrough` and `## Merge danger`, and `## Decision for you` first for `approve-risky`. | label `factory:review-high` or `factory:ci-green`, CI green | `approve-low`, `approve-risky`, `changes` |
| `ci-red` | Sends a PR with red CI straight to the developer, with no review. | label `factory:ci-red`, stage `in-review` | `fix` (CI is red), `waiting` (CI is green or still running) |
| `fix` | Developer fixes review findings or red CI and pushes a new head. | label `factory:fix` | `done-low`, `done-high`, `blocked` |
| `update` | Developer resolves a merge conflict with `main`. | label `factory:update` | `resolved`, `unresolvable` |
| `automerge` | Decides whether an approved PR merges alone or goes to a person. | label `factory:automerge` | `merge` (risk low), `needs-human` |
| `merge` | Merges an approved head with green CI, updates a branch that is behind, or flags a conflict. | label `factory:merge` | `conflict`, `updated-low`, `updated-high`, `merged`, `waiting` |
| `finish` | Marks a ticket `done` after its PR merges or its children close. | PR merged, ticket closed | `done`, `children-done`, `waiting` |
| `notify` | Tells the owner about a state that needs them. | stage `blocked` or `needs-answers`, label `factory:needs-human` | `notified`, `skipped` |
| `retro` | Collects lessons from finished tickets into one knowledge-base PR. | schedule, daily | `proposed`, `nothing` |
| `knowledge-review` | Reviews the retro PR. | label `factory:knowledge-review` | `approve`, `changes` |
| `accept-knowledge` | A person accepts or rejects the retro PR. | manual | `accepted`, `rejected` |
| `reconcile` | Re-fires missed wakes and polls CI for merge. | schedule, every 15 minutes | `reconciled` |
| `doctor-main`, `doctor-team` | Probe the main and team environments. | schedule, daily | `probed` |

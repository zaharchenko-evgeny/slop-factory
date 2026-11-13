# Reading the workflow

How to read this team's `workflow.yaml` and `factory.yaml`, and how to find where a ticket is and what runs next.
The `factory-troubleshoot` skill uses this guide to find and repair stuck tickets.

## The two files

- `factory.yaml` defines things: the tracker, the code host, models, skills, roles, agents (a role, a harness and a
  model), environments (a set of checkouts) and webhooks.
- `workflow.yaml` connects them: stages, shared `settings`, then `nodes` in delivery order.

Settings merge in this order, and a later layer wins: `factory.yaml` `defaults` → environment → role → agent →
`workflow.yaml` `settings` → the node's `settings`.

Always read the files at the commit that runs. Air runs a ticket's automation on the checked-out branch, so a ticket
branch that is older than a change on `main` still runs the old definition until `main` is merged into it. To read
`main` without touching a checkout: `git fetch origin && git show origin/main:workflow.yaml`.

## State: stages and labels

A ticket's state is its labels (on GitHub) or tags (on YouTrack), and its PR's state.

- **Stage.** Exactly one label `<label_prefix><stage>`, for example `factory:ready`. `label_prefix` is in
  `factory.yaml` `tracker`. Stage names come from the nodes: `when`, triggers and `stage` effects.
- **Stage kinds.** `stages.final` (for example `done`, `not-actionable`) never moves again. `stages.waiting` (for
  example `draft`, `blocked`, `needs-answers`) waits for a person. Every other stage is active: a node must move the
  ticket out of it.
- **Command labels.** Labels such as `factory:implement-small`, `factory:review-low`, `factory:fix` and
  `factory:merge` wake one node. They are not stages.
- **Value labels.** `risk:*` and `complexity:*` hold one value each. Outcomes set them; routing reads them.
- **`factory:needs-human`.** A person must decide. The gate or `reconcile` sets it when a run stops or a rule fires.
- **State mirroring.** `tracker.state_mirror` sets the tracker's own State field on each stage change, for example
  `in-dev → In progress`. `tracker.state_rules` maps a person's State change back, for example `Duplicate: stop`.

## A node

```yaml
dispatch:
  agent: dispatcher                 # factory.yaml agents: the role, harness and model
  environments: [main]              # where it runs; one copy per environment
  triggers: [{ stage: ready }]      # what wakes it; several triggers are OR
  when: { stage: ready, blockers: closed }   # it acts only when this holds
  output: read-only                 # what Air may push: read-only, create-pr, push-to-source, ...
  outcomes:
    small:   { effects: [{ label: "factory:implement-small" }, { stage: in-dev }] }
    blocked: { effects: [{ comment: reason }, { stage: blocked }] }
```

- **Triggers.** `stage: X` and `label: X` wake the node when the label is added. `pr: merged` and `ticket: closed`
  wake it on that event. `schedule: "<cron>"` runs a routine. `manual` is a person. `webhook` is an outside call.
- **`when`.** The guard on the ticket's state when the node starts: `stage`, `label`, `blockers` (`closed`) or
  `fix-rounds`. If it does not hold, the node exits without an outcome (`factory begin` exit 10). That is normal.
- **Outcomes.** The agent reports exactly one outcome with `factory end <outcome>`. The gate, not the agent,
  applies the outcome's effects.
- **`if` and `else`.** An outcome's `if` (`ci`, `pr`, `stage`, `label`, `review`, `children`) is checked by the
  gate. When it fails, the gate applies the outcome named by `else`, or refuses and changes nothing.
- **Effects.** `stage: X` moves the ticket. `label` and `unlabel` add and remove labels (`on: pr` targets the PR).
  `comment` posts a text that the agent reported (`reason`, `verdict`, `questions`). `field` sets a tracker field.
  `review`, `merge` and `update-branch` act on the PR, with their own checks (a merge needs an approved head with
  green CI). `create-issues` files child tickets.

The `human` node lists the moves a person may make from any stage but `done`: usually `triage` (scope it again)
and `ready` (start or resume delivery).

## Where is a ticket, and what runs next

1. Read the ticket's stage label, command labels, `risk:*` and `complexity:*`, and its PR's state and CI labels
   (`factory:ci-green`, `factory:ci-red`).
2. Find the nodes whose trigger matches the newest of those labels and whose `when` holds now. That node runs
   next, or has just run.
3. Read the ticket's and the PR's `[factory]` comments, newest first. Each comment ends with a `Factory-Run:`
   trailer that names the run. A gate refusal or a recovery note (`recover:`, `reconcile:`) says why it stopped.
4. Read that node's outcomes: they are every way out of the stage. An active stage with no run, no new outcome and
   no new comment for longer than the limits below is stuck.

## Limits

`workflow.yaml` `settings.limits`:

| Limit | Meaning |
| --- | --- |
| `stale_hours` | An active stage with no label or commit event for this long is stale. `reconcile` re-fires its wake once; a second time it sets `factory:needs-human`. |
| `stale_command_minutes` | The same, for a command label (`implement-*`, `fix`, `update`) that no run answered. |
| `run_timeout_minutes` | A run with no outcome after this long is re-woken once; a second miss sets `blocked` and `factory:needs-human`. A node's `timeout_minutes` overrides it. |
| `agent_runs_per_ticket` | After this many developer runs since the last `ready`, no developer run starts; the gate comments `Stopped: ... reached limits.agent_runs_per_ticket`. Setting `ready` again starts a new count. |
| `fix_rounds` (on the `fix` node) | After this many fix rounds with changes still requested, the gate comments `Stopped: ... reached limits.fix_rounds`. |
| `resume_cap`, `environment_repair_attempts` | Declared in the template; the engine does not read them yet. |

## Routines

- `reconcile` (a schedule, usually every 15 minutes) re-fires missed wakes, labels CI results on PRs and finishes
  tickets whose PR merged. On GitHub Issues it also updates a `[factory] reconcile heartbeat` comment on the map
  issue with the time of its last pass.
- `doctor-*` (daily) probes every write, read, tool, skill and secret in its environment. A failed probe opens or
  updates a ticket `Doctor: <probe> fails`; `factory:stuck` lists tickets stuck longer than 6 hours.
- `retro` (daily) proposes lessons in one knowledge-base PR.

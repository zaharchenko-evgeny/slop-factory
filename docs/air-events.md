# Air event catalog

This page lists the `eventType` values and the filters that `create_automation` takes for `github` and `youtrack`
triggers, and how a workflow trigger compiles to them. The code is `TRIGGER_WAKES`, `EVENTS` and `trigger_errors` in
`factory/definition.py` and `trigger` in `factory/deploy.py`. The recorded Air contract is
`factory/tests/contracts/air-mcp.json`; probes are in [targets.md](targets.md).

Status: **probed** means an Air run or a recorded payload showed it. **in use** means `factory deploy` plans it today. **assumed** means the name follows GitHub's webhook names
and no run has shown it yet. `factory deploy plan` prints the compiled trigger. Check an assumed row on the first apply.

## Filters

Air filters are exact or substring matches, ANDed. There is no OR, no NOT and no glob.

| Workflow filter | Air filter | Applies to | Status |
| --- | --- | --- | --- |
| `label: <name>` | `label` | `issues.labeled`, `pull_request.labeled` (probed), `pull_request.unlabeled`, `issues.unlabeled` (assumed) | probed |
| `pr_author: <login>` | `pr_author` | every `pull_request*` event | probed (#152 probe 5) |
| `state: approved\|changes_requested\|commented` | `review_state` | `pull_request_review.submitted` | probed (recorded payload) |
| `by: <login>` | `sender` | every GitHub event | assumed |
| `by: factory` | `sender: <targets.air.bot_login>` | every GitHub event | assumed |
| `by: human` | `sender_type: User` | every GitHub event | assumed |
| `push: <branch>` | `branch` | `push` | assumed |
| (CI) | `workflow_name` | `workflow_run.failed`, `workflow_run.completed` | in use, assumed |
| (CI green) | `conclusion: success` | `workflow_run.completed`, `check_suite.completed` | assumed |
| (YouTrack) | `tagAdded`, or a field name and value | `issueUpdated` | in use |

`by: human` needs a positive filter because Air has no NOT. It matches the GitHub `sender.type` of `User`, so a push
or a comment by the factory's bot does not fire it. If Air does not expose `sender` or `sender_type`, Air refuses the trigger
or never matches it. Probe this before a team relies on `by`.

## GitHub events

| Workflow trigger | Air `eventType` | Status |
| --- | --- | --- |
| `{ stage: <s> }`, `{ label: <l> }` on an issue | `issues.labeled` | probed |
| `{ label: <l> }` on a PR | `pull_request.labeled` | probed |
| `{ ticket: closed }` | `issues.closed` | in use |
| `{ ticket: commented }` | `issues.comment` | in use |
| `{ pr: opened }` | `pull_request.opened` | probed |
| `{ pr: pushed }` | `pull_request.synchronize` | in use |
| `{ pr: merged }` | `pull_request.merged` | in use |
| `{ pr: reviewed }`, `{ pr: reviewed, state: <s> }` | `pull_request_review.submitted` | probed |
| `{ pr: commented }` | `issue_comment.created` | in use |
| `{ pr: reopened }` | `pull_request.reopened` | assumed |
| `{ pr: ready_for_review }` | `pull_request.ready_for_review` | assumed |
| `{ pr: converted_to_draft }` | `pull_request.converted_to_draft` | assumed |
| `{ pr: closed }` (closed without merge) | `pull_request.closed` | assumed |
| `{ pr: review_requested }` | `pull_request.review_requested` | assumed |
| `{ pr: unlabeled }` | `pull_request.unlabeled` | assumed |
| `{ pr: edited }` | `pull_request.edited` | assumed |
| `{ pr: review_comment }` | `pull_request_review_comment.created` | assumed |
| `{ ci: failed }` | `workflow_run.failed` | in use |
| `{ ci: green }` | `workflow_run.completed` | assumed |
| `{ ci: checks_green }` | `check_suite.completed` | assumed |
| `{ push: <branch> }` | `push` | assumed |
| `{ ticket: opened }` | `issues.opened` | assumed |
| `{ ticket: reopened }` | `issues.reopened` | assumed |
| `{ ticket: unlabeled }`, `{ ticket: unlabeled, label: <l> }` | `issues.unlabeled` | assumed |
| `{ ticket: assigned }` | `issues.assigned` | assumed |

`ci: green` and `ci: checks_green` compile to the `completed` events with a `conclusion: success` filter, because Air has no
green-only event. `ci: green` also carries the environment's CI `workflow_name`. `push` takes one branch name (no glob) and
accepts `by`. A node woken by `push` or `check_suite` has no PR or ticket number in its event.

`pr: closed` stands for a close without a merge. Air has a separate `pull_request.merged` event. Whether `pull_request.closed`
also fires for a merged PR is not probed; a node that must skip merged PRs checks the PR state in its `when`.

## YouTrack events

Air has one YouTrack event, `issueUpdated`. A `tagAdded` filter or a field name and value narrows it. Field values are
case-sensitive. A YouTrack trigger takes no PR events, no `by` and no `state`.

## Events Air does not deliver

| Event | Fallback |
| --- | --- |
| Automation finished (AIRA-1476) | The tracker stage chains the next step. |
| PR labels on `pull_request.opened` and `synchronize` (AIRA-970) | The run checks the `ez/` branch prefix and exits at once otherwise. |
| A trigger with NOT or OR (`not by factory`, `a or b`) | Use `by: human`, or list two triggers. |
| Any event outside the tables above (a check run, a tag push, a deployment) | A `webhook` trigger, then a `schedule` trigger, then the reconcile sweep. |
| A missed event (Air down, a run that failed before it started, AIRA-1300) | The reconcile sweep reads the tracker and wakes the node again. |

## Validation

`factory validate` fails on an unknown trigger key, event or filter, and on a filter that does not fit its event
(`state` only fits `pr: reviewed`). The message names the supported values, for example
`unknown-event: nodes.x: pr: pushd: supported pr events: pr closed, ...`.

## Numberless events

A `push`, `ci: green` or `ci: checks_green` trigger has no ticket or PR number. A node woken only by such triggers
(or schedules) is a routine: `factory begin <node>` takes no number. Mixing these triggers with `pr`, `ticket` or
other numbered triggers on one node is a `mixed-triggers` validation error.

# Targets: how the delivery state machine runs locally and in Air Cloud

The state machine (`workflows/delivery.sm.yaml`) and the tracker adapter are the
same on every target. A target only decides **what starts a run** and **where it
runs**. Every run calls the same driver (`factory/drive.py`); the driver claims
the ticket, reads its state from the tracker, runs the actions of `agent`
states, and stops at a `human` or `external` state. No target moves a card
itself.

Status: **local** is implemented (#61, #68, #74). **Air** is planned (#7, #8).
The Air automations follow [the delivery state chart](statechart.md) (ADR-0013),
which replaces the Air rows below once its implementation tickets land. The
spike results are on #4.

## States

| State | Actor | Local target | Air target (planned) |
| --- | --- | --- | --- |
| `draft` | human | Ignored by the loop | No trigger |
| `ready` | agent | `drive.py loop --once` takes it from the tracker frontier (open, unblocked, unclaimed) | GitHub `issues.labeled`, filter `label = factory:ready` → `drive.py run $ISSUE` |
| `in-dev` | agent | Runs inside the same driver run (implement, checks) | Same run, no trigger |
| `in-review` | agent | Same run: reviewers, approver, publish, CI wait | Same run, no trigger |
| `changes-requested` | agent | Same run: developer `fix`, checks, re-review | Same run; a new review from a human starts a run (see events) |
| `approved` | human | Waits; the loop polls `pr.merged` through the `github-pr-merged` detector | GitHub `pull_request.merged` → `drive.py loop --once` |
| `merged` | agent | Post-merge actions (retro), then `done` | Same run as the merge trigger |
| `done` | human | Final | Final |
| `blocked` | human | `diagnose` and `notify` run once; waits for a person | Same actions; a person sets `ready` again, which fires the `ready` trigger |
| `waiting-main` | external | A blocking check fails on the ticket's branch and on the base too: the ticket is blocked by the open `Main is red: <check>` owner ticket (created `risk:low`, `ready` when missing). No `diagnose` or `notify`. The frontier starts nothing else while an owner ticket is open | Same; the scheduled sweep resumes it |
| `failed` | human | Same as `blocked`; `drive.py run N --resume` restarts | `--resume` from a manual run or a webhook |

## External events

| Event in the machine | Local detector | Air trigger | Filters | Air output behaviour |
| --- | --- | --- | --- | --- |
| `stage.ready` | Loop frontier scan every 10 min (launchd) | `issues.labeled` | `label = factory:ready` | `create-pr` (the driver opens the PR itself) or `read-only`; see open question 1 |
| `ci.passed` / `ci.failed` / `ci.pending` | Read inside the run by `gh pr checks` after publish | Read inside the run; a late failure also fires `workflow_run.failed` → `drive.py run <ticket>` | `workflow_name` contains `quality-gates` | `push-to-source` |
| Human review asks for changes | Not detected yet (planned) | `pull_request_review.submitted` | `review_state = changes_requested` | `push-to-source` |
| `comment.added` (`blocked`, `resume_on: comment`) | `loop --once` rechecks new comments (router `recheck`, cheap); `yes` returns the ticket to `ready` | Air has no `issue_comment` trigger: the scheduled sweep (`loop --once`) covers it. Only OWNER, MEMBER and COLLABORATOR comments count; local comments count as OWNER | repository | any |
| `main.green` (`waiting-main`) | `main.green` detector in `factory.yaml` (GitHub: every blocker of the ticket is closed) in `loop --once`; moves the ticket to `ready` and the next start merges the fixed base into its worktree. Not detected on the local tracker | Scheduled sweep → `drive.py loop --once` | repository | any |
| `pr.merged` | `github-pr-merged` detector in `loop --once` | `pull_request.merged` | repository | `read-only` only |
| Daily collect, weekly improve (#63, #64), distill, safety sweep | `loop --once` opens due tickets | Scheduled trigger → `drive.py loop --once` | cron | any |
| YouTrack ticket ready (#12) | YouTrack adapter frontier | YouTrack `issueUpdated:tagAdded` | exact, case-sensitive tag | `create-pr` |

The Air target renders one automation per external event declared in the
machine, plus one schedule, from `delivery.sm.yaml` and `factory.yaml`. Each
automation's prompt is thin: "run `uv run factory/drive.py run <ticket>` (or
`loop --once`) and report its final line". Automations carry the `[factory]`
name prefix, so only factory-owned automations are planned, updated or pruned.

## Runtime per target

| Concern | Local | Air |
| --- | --- | --- |
| Scheduler | launchd LaunchAgent, every 10 min, under `lockf` (`factory/schedule_local.py`) | Air triggers and a scheduled automation |
| Overlap | `lockf` per cycle, tracker claim per ticket | Tracker claim per ticket (duplicate trigger firings are expected) |
| Harness | `pi` for every role (`roles.<role>.harness`) | `claude-code` or `codex` adapter (#76) with the models Air provides |
| Model access | JetBrains Central proxy (`model_access: central`) | Air's own model access |
| Secrets | none for models; `gh` login of the user | Project environment secrets, names only: `GITHUB_TOKEN`, decisions-kit auth |
| Tools | installed on the machine | installed in the environment's setup script: `uv`, `gh`, `conftest`, `decide`, the harness CLI; `.air/cloud/startup.sh` installs `conftest` and `decide`, and `JetBrains/jbeval` is an additional repository of every automation that runs the approver |
| Logs | `.factory/runs/<N>/`, `.factory/runs/loop/loop.log` | Run page in Air plus the same run files in the run's workspace |

## Known Air limits that shape the design

- Trigger filters are exact key/value matches with no OR or NOT; YouTrack values are case-sensitive.
- `pull_request.opened` and `synchronize` do not expose PR labels to filters (AIRA-970); a run started by them must check the branch prefix `ez/` and exit at once otherwise.
- Scheduled runs use the creator's token; an expired login makes schedules silent.
- Two triggers that fire together start two runs; the tracker claim makes the second one exit.
- A run that fails before its session starts cannot be rerun from its payload (AIRA-1300).
- Automation chaining (`Automation finished`, AIRA-1476) is not available yet; the tracker stages chain the steps instead.

## Open questions for the Air spike (#4)

1. Output behaviour: the driver pushes branches and opens PRs with `gh`. Does Air allow that from a `create-pr` run, and can a `read-only` run (the merge trigger) push the retro entry to `knowledge-inbox`?
2. Do GitHub event triggers fire for this personal repository through the Air GitHub app?
3. Does the environment setup script install `uv`, `gh`, `conftest`, `decide` and the harness CLI, and can the harness use Air's models headless?

## Probed Air facts (#152)

Probed on 31 October with personal probe automations on `ez/152-probes` (model codex/gpt-6-luna). The probe automations are deleted. ADR-0014 does not exist yet, so the consequences are below; move them there once it merges (#166).

| # | Question | Answer | Run link |
| --- | --- | --- | --- |
| 1 | A label added by `jetbrains-air[bot]` fires `issues.labeled` and `pull_request.labeled` | Yes, both, within 2 s (labels on #164, #165, #167) | [12a95b1b](https://air.jetbrains.cloud/org/05cf1a7f-6ab5-713b-abd3-29d0c8a05e2d/automations/4fd6cfca-9ffd-4569-89b1-04f593cbc223?run=12a95b1b-5aec-4ffe-a26b-0ee487f0aa89), [bb1ac000](https://air.jetbrains.cloud/org/05cf1a7f-6ab5-713b-abd3-29d0c8a05e2d/automations/4fd6cfca-9ffd-4569-89b1-04f593cbc223?run=bb1ac000-a636-44b5-9b33-11ab1af51611), [aee758ee](https://air.jetbrains.cloud/org/05cf1a7f-6ab5-713b-abd3-29d0c8a05e2d/automations/766d62c3-0f85-4dc4-b464-79e44bb664e6?run=aee758ee-ecdc-45d1-85ee-d29d64e1f9cd) |
| 2 | A YouTrack tag added by a run fires `tagAdded` | Not probed: no YouTrack project for this repository; moved to #163 | none |
| 3 | `push-to-source` accepts a merge commit from `main` | Pending: needs `main` past the base of PR #167; probe after #166 merges | none yet |
| 4 | `.air/cloud/startup.sh` runs and `FACTORY_ENV` reaches it | Yes: `startup ran 2026-10-31T09:44:22Z FACTORY_ENV=probe`; the script is read from the automation's branch | [12a95b1b](https://air.jetbrains.cloud/org/05cf1a7f-6ab5-713b-abd3-29d0c8a05e2d/automations/4fd6cfca-9ffd-4569-89b1-04f593cbc223?run=12a95b1b-5aec-4ffe-a26b-0ee487f0aa89) |
| 5 | `pull_request.opened` fires for a PR the platform opens after `create-pr` | Yes: PR #167 opened by `app/jetbrains-air`; filter `pr_author: jetbrains-air[bot]` works | [69343c6a](https://air.jetbrains.cloud/org/05cf1a7f-6ab5-713b-abd3-29d0c8a05e2d/automations/9a76d1d9-e6d2-4d1b-a856-bc9242caeaf9?run=69343c6a-cf87-441a-9b8a-27474dbd1a25) |
| 6 | Two runs of one automation run at the same time | Yes: 09:44:05–09:44:50 and 09:44:06–09:44:55 overlap | runs 12a95b1b and bb1ac000 (links above) |
| 7 | Re-adding a present label fires no second event | Yes: the second add on #164 (exit 0) produced no event and no run | events of #164 |

Other findings and their consequences for the design (#150, #159):

- Concurrent runs of one automation are real: the join and the merge train must be idempotent and keyed by head SHA. The pilot sweep merges nothing (dry-run).
- In a run `gh` acts as `jetbrains-air[bot]` through `GH_TOKEN`; `gh api user` answers 403, so a role must not call it.
- The `create-pr` agent named its branch `ez/152-probe-p5` from AGENTS.md, so the gate must check branch naming, not assume it.
- `result.summary` was null for 2 of 5 finished runs: the node's handoff goes through `gate apply`, not the run summary.
- No answer is "no", so no workaround is needed. Probe 2 is tracked in #163; probe 3 stays open until it runs.

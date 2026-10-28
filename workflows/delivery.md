# Workflow: delivery

The per-ticket loop (design §5, §6). This file is the single definition; the
local skill runs it now, and the setup skill later renders it into an Air
workflow graph. Names of repositories, stages, models and checks come from
`factory.yaml`.

The same loop is defined as a state machine in `workflows/delivery.sm.yaml`,
checked and run by `factory/sm.py` (`validate`, `next`, `simulate`);
`factory/drive.py` runs it against a tracker through the operations in
`docs/agents/issue-tracker.md`; `drive.py run <N>` executes its actions until
the ticket waits on a human. The local skill runs the machine, so the
machine is the definition of the order and the transitions; this table stays as
the readable summary and the Air render source, kept in step with the machine.

## Input

A tracker ticket in stage `ready`.

## Steps

| # | Step | Kind | Does | On failure |
| --- | --- | --- | --- | --- |
| 1 | preflight | script | Tracker auth works, the ticket is in `ready`, no branch or open PR for the ticket exists, the run's models are ready | stage `blocked`, stop |
| 2 | intake | agent: router (`intake`) | Classify, check the definition of ready, choose the risk path | `ready: no` → stage `blocked` with the question, stop |
| 3 | start | script | Stage `in-dev`; create a worktree on a new branch from the base branch | stage `failed`, stop |
| 4 | implement | agent: developer (`implement`), tier by risk path | Implement and commit | `blocked` → stage `blocked`, stop |
| 5 | checks | script | Run every `pre-review` check from `factory.yaml`; a check whose `requires:` command is missing is `skipped` | a blocking check fails → back to step 4 in `fix` mode with the output; counts as a fix round |
| 6 | review | agent: reviewers by risk, fresh sessions, read-only | `risk:low`: one `full` reviewer. `risk:high`: `behaviour` and `structure-security` reviewers in parallel, joined when both report. Each reads the changed files since the base in the worktree (re-review: since `reviewed_sha`, only the reviewers that blocked). Each runs in its own run dir and detached checkout of HEAD, outside the shared run dir. Models and fallbacks (used after a malformed handoff): `roles.reviewer.modes` | a reviewer with no handoff after retry and fallback → stage `failed` |
| 7 | gateway | decision | Merge: blocked if any reviewer blocks; risk is the highest any reviewer reports. `approved` → step 8. `changes-requested` → stage `changes-requested`, developer `fix` mode, then steps 5–6. Same blocking findings in two consecutive rounds, or more than `limits.fix_rounds` rounds → stage `blocked`, stop | |
| 8 | publish | script | Push the branch; open one PR per repository, linked to the ticket; post nits once as one PR comment; set risk and docs-impact labels from the review (a review that reports a higher risk relabels the ticket, noted in the PR body); stage `approved` | stage `failed`, stop |
| 9 | summary | agent: router (`summary`) | Check the result against the original request; post the summary on the ticket | — |
| 10 | merge | human | A human approves and merges the PR (gate 2). Stage `done` when every PR of the ticket is merged | |

## Handoffs

Each agent step ends with the structured block its role file defines. Steps
pass these blocks, never transcripts. The developer resumes its own session in
`fix` mode; the reviewer always starts fresh.

## External writes

Every push, label change, PR and comment carries the `[factory]` marker and a
`Factory-Run:` trailer. While `gates.confirm_external_writes` is `true`, each
write kind in `gates.confirm_external_writes_scope` is shown and waits for the
user's approval; a declined write stops the run with stage unchanged. Other
writes, including the push of the ticket's own branch, are auto-approved.
Merging stays with the human (`gates.pr_approval`).

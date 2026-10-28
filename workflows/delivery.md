# Workflow: delivery

The per-ticket loop (design §5, §6). This file is the single definition; the
local skill runs it now, and the setup skill later renders it into an Air
workflow graph. Names of repositories, stages, models and checks come from
`factory.yaml`.

The same loop is defined as a state machine in `workflows/delivery.sm.yaml`,
checked and run by `factory/sm.py` (`validate`, `next`, `simulate`);
`factory/drive.py` runs it against a tracker through the operations in
`docs/agents/issue-tracker.md`. Until the local skill switches to it, keep this
table and the machine in step.

## Input

A tracker ticket in stage `ready`.

## Steps

| # | Step | Kind | Does | On failure |
| --- | --- | --- | --- | --- |
| 1 | preflight | script | Tracker auth works, the ticket is in `ready`, no branch or open PR for the ticket exists, the run's models are ready | stage `blocked`, stop |
| 2 | intake | agent: router (`intake`) | Classify, check the definition of ready, choose the risk path | `ready: no` → stage `blocked` with the question, stop |
| 3 | start | script | Stage `in-dev`; create a worktree on a new branch from the base branch | stage `failed`, stop |
| 4 | implement | agent: developer (`implement`), tier by risk path | Implement and commit | `blocked` → stage `blocked`, stop |
| 5 | checks | script | Run every `pre-review` check from `factory.yaml` | a blocking check fails → back to step 4 in `fix` mode with the output; counts as a fix round |
| 6 | review | agent: reviewer, fresh session, read-only | Review the diff since the base (re-review: since `reviewed_sha`) | — |
| 7 | gateway | decision | `approved` → step 8. `changes-requested` → stage `changes-requested`, developer `fix` mode, then steps 5–6. Same blocking findings in two consecutive rounds, or more than `limits.fix_rounds` rounds → stage `blocked`, stop | |
| 8 | publish | script | Push the branch; open one PR per repository, linked to the ticket; post nits once as one PR comment; set risk and docs-impact labels from the review; stage `approved` | stage `failed`, stop |
| 9 | summary | agent: router (`summary`) | Check the result against the original request; post the summary on the ticket | — |
| 10 | merge | human | A human approves and merges the PR (gate 2). Stage `done` when every PR of the ticket is merged | |

## Handoffs

Each agent step ends with the structured block its role file defines. Steps
pass these blocks, never transcripts. The developer resumes its own session in
`fix` mode; the reviewer always starts fresh.

## External writes

Every push, label change, PR and comment carries the `[factory]` marker and a
`Factory-Run:` trailer. While `gates.confirm_external_writes` is `true`, each
write is shown and waits for the user's approval; a declined write stops the
run with stage unchanged.

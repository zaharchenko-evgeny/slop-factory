---
name: factory-troubleshoot
description: Find why a factory ticket or the whole factory is stuck, and repair it only with moves that the workflow allows. Use for "is the factory healthy", "why is ticket X stuck", "unstick X", stale or hung runs, a ticket in failed, blocked or needs-human, or a gate that refused an outcome.
---

# Factory troubleshoot

Read `docs/reading-the-workflow.md` in the team repository first (in Air: `~/factory-team/docs/`). It explains
stages, nodes, outcomes, effects and limits. This skill uses its terms.

Read the definition at the commit that runs: `git fetch origin`, then `git show origin/main:workflow.yaml` and
`git show origin/main:factory.yaml`. For a ticket that has a branch, also read that branch's files: its runs use them.

## 1. Collect the facts of one ticket

- Stage label, command labels, `risk:*`, `complexity:*`, `factory:needs-human`, the tracker's State.
- The time of the last label change and the last commit on its branch.
- The PR: state, head commit, mergeable or conflicting, behind `main` or not, `factory:ci-*` label, the project's
  own check runs and commit statuses, reviews at the head.
- `[factory]` comments on the ticket and the PR, newest first, and their `Factory-Run:` trailers.
- When you can read Air automation runs: the runs of the node that should act, their status and reported outcome.

## 2. Classify

Match the first row that fits. "Active stage" means a stage that is neither final nor waiting.

| Symptom | Signal | Cause to check |
| --- | --- | --- |
| Stale | Active stage, no label or commit event for more than `stale_hours` (`stale_command_minutes` for a command label) | The wake was lost, or the run died. Look for a `recover:` comment: `reconcile` re-fires once. |
| Hung run | An Air run of the node is still running after `run_timeout_minutes` | The agent waits on a command or a prompt. A `reconcile: ... re-fired` comment means one retry is done. |
| Missed wake | The label that wakes a node is there, its `when` holds, and no run started | The automation is missing, disabled or deployed from an old definition (deploy drift), or Air did not start it. |
| Guard not met | A run started and ended with no outcome (`factory begin` exit 10) | The node's `when` does not hold, for example blockers still open or the wrong stage. That is correct; fix the state, not the node. |
| Gate refusal | The run reported an outcome but the state did not change, or `[factory] Outcome stopped after these effects landed: ...` (only some effects applied) | The effect's own check failed: CI not green at the head, head moved, PR not approved. Report it; never work around it. |
| Run cap | `Stopped: ... reached limits.agent_runs_per_ticket` or `limits.fix_rounds` | Too many rounds. A person decides whether to rescope, then sets `ready`. |
| Stopped twice | `factory:needs-human`, `recover: stage=X: no recovery rule ...` or `reconcile: ... stopped: ... twice` | Recovery gave up. Find why the node did not answer before you resume. |
| Waiting | `blocked`, `needs-answers` or `draft` | A person must answer the comment that set it. Not a fault. |
| CI never starts | PR open, no check runs at the head for a long time | The code host's CI is down, out of quota or billing, or the PR conflicts (GitHub starts no CI on a conflict). |
| Wrong definition | The ticket branch is older than a `factory.yaml` or `workflow.yaml` change on `main` | Runs use the branch's definition. Merge `main` into the branch. |

## 3. Check the whole factory

- **Routines.** The last `reconcile` run finished within two of its intervals. On GitHub Issues the
  `[factory] reconcile heartbeat` comment on the map issue shows the time of the last pass. Today's `doctor-*` runs
  finished; open `Doctor: <probe> fails` tickets list what is broken.
- **Stuck tickets.** List the tickets in each active stage and classify each one with section 2.
- **Systemic.** Three or more tickets stuck the same way at the same time point at the factory, not the tickets:
  Air is down, CI is down, a token expired, or a deploy is missing. Fix that once, not each ticket.
- **Deploy drift.** `factory deploy --dry-run` (operator only) shows the automations that differ from the
  definition. A plan with changes means `main` is not fully deployed.

Verdict: **healthy** (routines run, nothing stuck), **degraded** (routines run, some tickets stuck) or **down**
(routines do not run, or a systemic cause).

## 4. Repair only with allowed moves

Propose one move, say why, and wait for confirmation when the person asked for it. Allowed moves:

- **Re-fire the wake:** remove the current stage or command label, then add it again. Do it once; `reconcile`
  already does it automatically, so a second stale finding needs a cause, not another re-fire.
- **A `human` node outcome:** set `triage` (scope it again) or `ready` (resume delivery). Setting `ready` also
  resets the `agent_runs_per_ticket` count.
- **Remove a leftover label:** a command label (`implement-*`, `fix`, `update`, `review-*`) that no node will answer
  in the current stage, or `factory:needs-human` once its cause is fixed.
- **Comment:** say what you found and what you changed, so the next reader does not repeat the work.
- **Merge `main` into the ticket branch** for a wrong definition, when you may push to that branch.

Never:
- set `approved` or `done`
- set or change `risk:*` or `complexity:*`
- approve, review or merge a PR
- re-run a gate that refused, hoping that it passes
- edit Air automations by hand

These are the gate's and the deploy's jobs.

## 5. Report and learn

Report the ticket, the classification with its evidence (labels, comment links, run ids), the move and its result.
When the cause is a factory fault (a node that leaves a ticket in its stage with no way out, a missing rule, a
routine that does not run), propose a ticket against the team's factory definition. If the definition cannot
express the fix, propose one against the engine and say why.

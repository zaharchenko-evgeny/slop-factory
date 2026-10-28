---
name: factory-delivery
description: Run the factory delivery workflow locally on one tracker ticket — router intake, developer, pre-review checks, independent reviewer, fix rounds, PR and router summary — with one pi subagent process per role. Use when the user asks to deliver, run the factory on, or push a ready ticket through the factory locally.
---

# Factory delivery (local target)

You are the orchestrator. You run the delivery state machine
(`workflows/delivery.sm.yaml`, engine `factory/sm.py`) for one ticket. You
never implement or review yourself: each agent step is a separate `pi -p`
process loaded with the role's files. You run the script steps and all
external writes.

Read first: `factory.yaml`, `workflows/delivery.sm.yaml`, `workflows/delivery.md`, `AGENTS.md`,
`docs/agents/issue-tracker.md`.

## Run with the driver

```bash
uv run --quiet factory/drive.py run "$N"                       # GitHub Issues tracker
uv run --quiet factory/drive.py run "$N" --tracker local:<effort dir>
```

It claims the ticket, runs every action the machine starts (preflight, start,
checks and publish itself; agent actions through `run_role.py`), writes stage
and vars after each step and releases the claim when the ticket reaches
`approved`, `blocked` or `failed`. It prints the steps; exit 0 means
`approved`. Then report (below), and post the summary, nits and cleanup by
hand until #36. The sections below are the manual fallback.

## Set up the run

```bash
cd "$(git rev-parse --show-toplevel)"   # the factory root; every path below is absolute
N=<ticket number>
RUN=$PWD/.factory/runs/$N       # handoffs and logs (targets.local.run_dir)
WT=$PWD/.factory/worktrees/$N   # the ticket's worktree (targets.local.worktree_dir)
RUN_ID="local:$(date +%Y%m%dT%H%M%S)-$N"
ME=$(gh api user --jq .login)     # the executor: claims are assignees
TR="uv run --quiet factory/github_tracker.py"
mkdir -p "$RUN"
```

Read the ticket with `gh issue view $N --json title,body,labels,url` and save
it to `$RUN/ticket.json` (for the branch name and the report; role inputs re-read it). Run every role's `roles/<role>/setup.sh`; a failure
is a preflight failure.

## Run a role

Write `$RUN/<step>.in.md`, then run the agent action through the engine:

```bash
uv run --quiet factory/run_role.py "$N" <action> [--step <step>]   # e.g. review_full --step review_full-2
```

It resolves the run dir, worktree, role files and model (`factory.yaml`, the
ticket's risk from `$RUN/sm.json`) as absolute paths, runs intake in the
factory root and every other action (summary and review included) inside
`$WT`, and writes `$RUN/<step>.out.md` and `$RUN/<step>.log`. The handoff
must be the last fenced block of the output with nothing after it. An empty
output, a missing handoff or text after it is logged in `<step>.log`; each call
runs the model once (and the fallback once after an empty output), and exit 3
means feed `<action>.error`: the machine's `retry` reruns the action. On exit 0 the
parsed handoff is in `$RUN/<step>.handoff.json`; build the event payload from
it. For the summary it appends each PR's body and CI checks
(`gh pr view`, read-only) to the input. It needs only macOS `/bin/bash` 3.2
features here: no `declare -A`, no `mapfile`.

| Role | Tools | Session flags |
| --- | --- | --- |
| router | `read,grep,find,ls` | `--no-session` |
| developer | `read,bash,edit,write,grep,find,ls` | `--session-id factory-$N-developer` (same id in fix mode, so it resumes) |
| reviewer | `read,bash,grep,find,ls` | `--no-session` (always fresh) |

The input file `<step>.in.md` holds: the mode, the base branch, and only the
handoff blocks that step needs. `run_role.py` appends the ticket, re-read with
`gh issue view $N --json title,body,comments,labels` just before the step, so
it carries the current stage; never paste an older copy. Never pass a transcript.
Never give the reviewer the developer's handoff; give it its mode (the
action's `mode`), the ticket, the branch, the base and, on a re-review, the
last `reviewed_sha`. Never paste the diff instead: the reviewer reads the
changed files in `$WT`.

Never repair or accept a handoff `run_role.py` rejected; the machine turns
`<action>.error` into stage `failed`.

## Drive the machine

The machine decides every next step; never pick one from the table in
`workflows/delivery.md`. Every stage, vars and claim write goes through the
adapter (`factory/github_tracker.py`), never a hand-written `gh issue edit`.

1. `$TR frontier` lists the tickets you may take; `$N` must be one of them
   (or already claimed by `$ME`). `$TR claim $N "$ME"`, the first write. If it
   fails, stop without writing: another executor holds the ticket.
2. Start the ticket file from the tracker:
   `printf '{"state":"%s","vars":%s}' "$($TR get-state $N)" "$($TR read-vars $N)" > "$RUN/sm.json"`.
   The stored vars carry the running actions and outputs under `_run`, so a
   stopped run resumes: feed the results of the actions still in `pending`.
3. Apply each event, starting with `stage.ready={run_id: "$RUN_ID"}`:

   ```bash
   uv run --quiet factory/sm.py next "$RUN/sm.json" "<event>" > "$RUN/step.json"
   jq .ticket "$RUN/step.json" > "$RUN/sm.json"
   [ "$(jq -r .step.from "$RUN/step.json")" = "$(jq -r .step.to "$RUN/step.json")" ] ||
     $TR set-state $N "$(jq -r .step.to "$RUN/step.json")"
   $TR write-vars $N "$(jq -c '.vars + {_run: {out, pending, retries, fork}}' "$RUN/sm.json")"
   ```

   `step.run` lists the actions to run next; run each (below) and feed its
   result back as `<action>.done|failed|error` with the handoff fields as the
   payload, e.g. `intake.done={ready: "yes", risk: high}`. Set the stage after
   every step, before you run the next actions: the stage is `in-review`
   while the reviewers run (in-dev → in-review → approved). Keep `$RUN/sm.json`:
   it holds the running actions and their outputs between steps.
4. When `step.wait` is `human` or `external`, `$TR release $N "$ME"` and end
   the run. A non-zero `sm.py` exit is a workflow error: stop with `failed`.

The machine chooses the reviewers by `risk` (see **review** below).

## Actions

Local details per action of `workflows/delivery.sm.yaml`:

- **preflight**: `gh auth status`; the ticket has the `ready` stage label;
  `git ls-remote --heads origin "<branch>"` and `gh pr list --head "<branch>"`
  are empty; `uv run --quiet factory/preflight.py` passes: every role's model
  gets a non-empty, non-error reply to a one-line `pi -p` call (any reply, not
  only the exact text `OK`). `pi auth check` alone is not enough: it reports
  `ready` for an invalid key.
- **intake split**: when slices are created from the router's `split:`, edit
  each comment whose items moved to a slice so its body starts with
  `[factory] Moved to #<slice>.`; `run_role.py` leaves those comments out of
  role inputs, so a slice's role sees only its own scope.
- **start**: base branch is the ticket's `base:<branch>` line if present,
  else the repository default. Branch name:
  `BRANCH=$(uv run --quiet factory/drive.py branch "$N" "$RUN/ticket.json")` (the body's
  `slug:` line, else a slug of the title);
  `git worktree add -b "$BRANCH" "$WT" "origin/<base>"`.
- **checks**: run each `checks[].run` with `bash -c` inside `$WT`, with
  `FACTORY_BASE` set to the base branch. A check whose `requires:` command
  is missing is `skipped`, not `pass`. Save the
  output in `$RUN/checks-<round>.md`. Only `blocking` checks send the work
  back to the developer (`checks.failed`); report `advisory` failures in the summary.
- **review**: `step.run` lists the reviewers of the fork `review`: one
  `review_full` on low risk; `review_behaviour` and
  `review_structure_security` on high risk; on a re-review only those whose
  last verdict blocked. Run each with its own step name (they may run in
  parallel) and feed each `<reviewer>.done|error`; the machine joins when all
  report. `run_role.py` picks each focus's model and fallback from
  `roles.reviewer.modes`. The payload is the handoff plus `blocking_ids`
  (the `id`s under `blocking:`). `reviewed_sha` must equal
  `git -C "$WT" rev-parse HEAD`. A handoff with `fallback_from` means the
  reviewer switched models; say so in the PR body:
  `Review: <action> fell back from <fallback_from> to <model>`.
- **publish**: `git -C "$WT" push -u origin "$BRANCH"`; `gh pr create` with
  title `#<N> <ticket title>` and a body with: what changed, checks, review
  verdict, `Closes #<N>` only if the router confirms completion,
  `[factory]` marker and `Factory-Run: $RUN_ID`. The machine raises the var
  `risk` to the highest risk a reviewer reports; when it is higher than the
  ticket's risk label (`tracker.risk_labels`), relabel the ticket
  (`gh issue edit $N --add-label <new> --remove-label <old>`) and say so in the
  PR body: `Risk: raised from <old> to <new> by the review`.
- **cleanup**: after the summary (it runs in `$WT`), `git worktree remove "$WT"`. Keep `$RUN`.

## External writes

Writes are: push, PR, issue or PR comment, label change, assignee change.
While `gates.confirm_external_writes` is `true`, before each write of a kind in
`gates.confirm_external_writes_scope` (today: none; pushing the ticket's own
`branch_pattern` branch needs no prompt, so runs can go unattended) show the user the exact
command and full text, then wait for an explicit approval. Batch the writes of
one step into one approval request. If the user declines, stop the run and
leave the stage as it is. Other writes (issue and PR text, comments, labels,
claims) are auto-approved; say them in the report. Never merge: that is the
human's gate.

## Report

At the end tell the user: the PR link, the stage, fix rounds, checks with
results, the review verdict, and the router's acceptance table. Say what was
not verified.

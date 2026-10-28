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
it to `$RUN/ticket.json`. Run every role's `roles/<role>/setup.sh`; a failure
is a preflight failure.

## Run a role

Write `$RUN/<step>.in.md`, then run the agent action through the engine:

```bash
uv run --quiet factory/run_role.py "$N" <action> [--step <step>]   # e.g. review --step review-2
```

It resolves the run dir, worktree, role files and model (`factory.yaml`, the
ticket's risk from `$RUN/sm.json`) as absolute paths, runs intake in the
factory root and every other action (summary and review included) inside
`$WT`, and writes `$RUN/<step>.out.md` and `$RUN/<step>.log`. An empty output
is logged in `<step>.log` and rerun once (the action's `retry`); exit 3 means
feed `<action>.error`. For the summary it appends each PR's body and CI checks
(`gh pr view`, read-only) to the input. It needs only macOS `/bin/bash` 3.2
features here: no `declare -A`, no `mapfile`.

| Role | Tools | Session flags |
| --- | --- | --- |
| router | `read,grep,find,ls` | `--no-session` |
| developer | `read,bash,edit,write,grep,find,ls` | `--session-id factory-$N-developer` (same id in fix mode, so it resumes) |
| reviewer | `read,bash,grep,find,ls` | `--no-session` (always fresh) |

The input file `<step>.in.md` holds: the mode, the ticket JSON, the base
branch, and only the handoff blocks that step needs. Never pass a transcript.
Never give the reviewer the developer's handoff; give it the ticket, the
branch, the base and, on a re-review, the last `reviewed_sha`.

Take the handoff from the last fenced block of `<step>.out.md`. If it is
missing or malformed, rerun the step once; then stop with stage `failed`.

## Drive the machine

The machine decides every next step; never pick one from the table in
`workflows/delivery.md`. Every tracker write goes through the adapter
(`factory/github_tracker.py`), never a hand-written `gh issue edit`.

1. `$TR frontier` lists the tickets you may take; `$N` must be one of them
   (or already claimed by `$ME`). `$TR claim $N "$ME"`, the first write. If it
   fails, stop without writing: another executor holds the ticket.
2. Start the ticket file from the tracker:
   `printf '{"state":"%s","vars":%s}' "$($TR get-state $N)" "$($TR read-vars $N)" > "$RUN/sm.json"`.
3. Apply each event, starting with `stage.ready={run_id: "$RUN_ID"}`:

   ```bash
   uv run --quiet factory/sm.py next "$RUN/sm.json" "<event>" > "$RUN/step.json"
   jq .ticket "$RUN/step.json" > "$RUN/sm.json"
   $TR set-state $N "$(jq -r .ticket.state "$RUN/sm.json")"   # only when it changed
   $TR write-vars $N "$(jq -c .ticket.vars "$RUN/sm.json")"
   ```

   `step.run` lists the actions to run next; run each (below) and feed its
   result back as `<action>.done|failed|error` with the handoff fields as the
   payload, e.g. `intake.done={ready: "yes", risk: high}`. Keep `$RUN/sm.json`:
   it holds the running actions and their outputs between steps.
4. When `step.wait` is `human` or `external`, `$TR release $N "$ME"` and end
   the run. A non-zero `sm.py` exit is a workflow error: stop with `failed`.

Reviewer selection stays as below until #25 moves it into the machine.

## Actions

Local details per action of `workflows/delivery.sm.yaml`:

- **preflight**: `gh auth status`; the ticket has the `ready` stage label;
  `git ls-remote --heads origin "<branch>"` and `gh pr list --head "<branch>"`
  are empty; for every model of the run, a one-line call succeeds:
  `pi -p --no-mcp --no-session --no-tools --model <model> "Reply with OK."`.
  `pi auth check` alone is not enough: it reports `ready` for an invalid key.
- **start**: base branch is the ticket's `base:<branch>` line if present,
  else the repository default. Branch name from `branch_pattern` with a short slug of the title;
  `git worktree add -b "$BRANCH" "$WT" "origin/<base>"`.
- **checks**: run each `checks[].run` with `bash -c` inside `$WT`, with
  `FACTORY_BASE` set to the base branch. Save the
  output in `$RUN/checks-<round>.md`. Only `blocking` checks send the work
  back to the developer (`checks.failed`); report `advisory` failures in the summary.
- **review**: one reviewer at `models.review`. `reviewed_sha` must equal
  `git -C "$WT" rev-parse HEAD`.
- **publish**: `git -C "$WT" push -u origin "$BRANCH"`; `gh pr create` with
  title `#<N> <ticket title>` and a body with: what changed, checks, review
  verdict, `Closes #<N>` only if the router confirms completion,
  `[factory]` marker and `Factory-Run: $RUN_ID`.
- **cleanup**: after the summary (it runs in `$WT`), `git worktree remove "$WT"`. Keep `$RUN`.

## External writes

Writes are: push, PR, issue or PR comment, label change, assignee change.
While `gates.confirm_external_writes` is `true`, before each write of a kind in
`gates.confirm_external_writes_scope` (today: push) show the user the exact
command and full text, then wait for an explicit approval. Batch the writes of
one step into one approval request. If the user declines, stop the run and
leave the stage as it is. Other writes (issue and PR text, comments, labels,
claims) are auto-approved; say them in the report. Never merge: that is the
human's gate.

## Report

At the end tell the user: the PR link, the stage, fix rounds, checks with
results, the review verdict, and the router's acceptance table. Say what was
not verified.

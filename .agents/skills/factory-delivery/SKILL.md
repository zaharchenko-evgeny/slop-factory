---
name: factory-delivery
description: Run the factory delivery workflow locally on one tracker ticket — router intake, developer, pre-review checks, independent reviewer, fix rounds, PR and router summary — with one pi subagent process per role. Use when the user asks to deliver, run the factory on, or push a ready ticket through the factory locally.
---

# Factory delivery (local target)

You are the orchestrator. You run `workflows/delivery.md` for one ticket. You
never implement or review yourself: each agent step is a separate `pi -p`
process loaded with the role's files. You run the script steps and all
external writes.

Read first: `factory.yaml`, `workflows/delivery.md`, `AGENTS.md`.

## Set up the run

```bash
N=<ticket number>
RUN=.factory/runs/$N            # handoffs and logs
WT=.factory/worktrees/$N        # the ticket's worktree
RUN_ID="local:$(date +%Y%m%dT%H%M%S)-$N"
mkdir -p "$RUN"
```

Read the ticket with `gh issue view $N --json title,body,labels,url` and save
it to `$RUN/ticket.json`. Run every role's `roles/<role>/setup.sh`; a failure
is a preflight failure.

## Run a role

Agent steps run inside the worktree (the repository root for intake), with the
model from `factory.yaml` (`models.<tier>` of the role):

```bash
pi -p --no-mcp --model "$MODEL" $SESSION_FLAGS --tools "$TOOLS" \
  --append-system-prompt roles/_common.md \
  --append-system-prompt roles/<role>/role.md \
  "$(cat "$RUN/<step>.in.md")" > "$RUN/<step>.out.md" 2>"$RUN/<step>.log"
```

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

## Steps

Follow the table in `workflows/delivery.md`. Local details:

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
  back to the developer; report `advisory` failures in the summary.
- **review**: `reviewed_sha` must equal `git -C "$WT" rev-parse HEAD`.
- **publish**: `git -C "$WT" push -u origin "$BRANCH"`; `gh pr create` with
  title `#<N> <ticket title>` and a body with: what changed, checks, review
  verdict, `Closes #<N>` only if the router confirms completion,
  `[factory]` marker and `Factory-Run: $RUN_ID`.
- **stage change**: remove the current `factory:*` stage label and add the new
  one in one `gh issue edit --remove-label … --add-label …` call.
- **cleanup**: after publish, `git worktree remove "$WT"`. Keep `$RUN`.

## External writes

Writes are: push, PR, issue or PR comment, label change. While
`gates.confirm_external_writes` is `true`, before each write show the user the
exact command and full text, then wait for an explicit approval. Batch the
writes of one step into one approval request. If the user declines, stop the
run and leave the stage as it is.

## Report

At the end tell the user: the PR link, the stage, fix rounds, checks with
results, the review verdict, and the router's acceptance table. Say what was
not verified.

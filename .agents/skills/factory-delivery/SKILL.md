---
name: factory-delivery
description: Run the factory delivery workflow locally on one tracker ticket — router intake, developer, pre-review checks, independent reviewer, fix rounds, PR and router summary — with one pi subagent process per role. Use when the user asks to deliver, run the factory on, or push a ready ticket through the factory locally.
---

# Factory delivery (local target)

A ticket is delivered with `uv run factory/drive.py run <N>` directly; an
orchestrator agent is not needed. Whoever runs it never implement, review or repair a handoff
yourself: the driver runs each role as its own `pi -p` process and does every
tracker, git and `gh` write. Each role starts lean
(`--no-skills --no-extensions --no-prompt-templates` plus its `skills.txt`), and the run
ends with a cost table.

Read first: `factory.yaml`, `workflows/delivery.md`, `AGENTS.md`.

## Run

From the factory root (`git rev-parse --show-toplevel`), with `N` a ticket from
the frontier (`uv run --quiet factory/github_tracker.py frontier`):

```bash
uv run --quiet factory/drive.py run "$N"                       # GitHub Issues tracker
uv run --quiet factory/drive.py run "$N" --tracker local:<effort dir>
```

The driver claims the ticket and runs the machine (`workflows/delivery.sm.yaml`)
until it waits on a human: preflight, intake, start, implement, checks, the
reviewers by risk, fix rounds, publish (push, PR with `Refs #N`, risk and
docs-impact labels, one nits comment), `ci` (blocking checks on the branch
merged with the base, then the CI watch once checks register; a failure is a
fix round with the failing log, then a re-review and a rebuilt PR body), then,
after the stage moves to `approved`, the summary (posted on the ticket; the PR
body becomes `Closes #N` when it says `complete: yes`) and, once CI passed, the
worktree cleanup. The reviewers of a high-risk fork run in parallel. It prints each step and exits 0 at `approved`. After the human merges,
`drive.py run "$N"` on the `approved` ticket (or `drive.py loop --once` for all
of them) moves it through `merged` (the retro entry, pushed to the
`knowledge` branch) to `done`; while a PR is open it prints
"waiting for pr.merged" and writes nothing. `drive.py loop` keeps every ticket
moving from the machine alone: it runs tickets in agent states (frontier
first) within `limits.concurrent_deliveries` and `limits.runs_per_day`, polls
the `factory.yaml` `events` detectors of the others, and stops when nothing
moves. Entering `blocked` or `failed` runs `diagnose` (a reason on the
ticket) and `notify` once. Run
artefacts stay in `.factory/runs/<N>/`.

If it stops in `blocked` or `failed`, do not continue by hand: report the last
step and the reason from the printed trace and `.factory/runs/<N>/`
(`<step>.log`, `<step>-attempt<k>.out.md`, `checks-<round>.md`). Once the
cause is fixed, `drive.py run "$N" --resume` restarts a `failed` ticket at the
step that failed. Exit 2 with "claimed by" means another executor holds the
ticket; nothing was written.

## External writes

While `gates.confirm_external_writes` is `true`, the kinds listed in
`gates.confirm_external_writes_scope` (today: none) need the user's approval
before the run. All other writes are auto-approved; name them in the report.
Never merge: that is the human's gate.

## Report

Tell the user: the PR link, the final stage, fix rounds, checks with results,
each review verdict, CI result, the router's acceptance table and the writes
made. Say what was not verified.

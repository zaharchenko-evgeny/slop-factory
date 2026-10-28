# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues in
`tracker.repository` of `factory.yaml`. Use the `gh` CLI for all operations.
This file follows the per-tracker templates of the mattpocock
`setup-matt-pocock-skills` skill (GitHub, GitLab, local markdown), so
`triage`, `to-tickets`, `wayfinder` and the factory share one board. The
**Factory operations** section is the factory's addition.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --json number,title,body,labels,comments`.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments` with `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v`; `gh` does this automatically inside a clone.

## Pull requests as a triage surface

**PRs as a request surface: no.**

## When a skill says "publish to the issue tracker"

Create a GitHub issue.

## When a skill says "fetch the relevant ticket"

Read it as in **Read an issue** above.

## Wayfinding operations

- **Map**: a single issue labelled `wayfinder:map`; child issues are its sub-issues.
- **Blocking**: GitHub issue dependencies, or a `Blocked by: #<n>, #<n>` line at the top of the body. A ticket is unblocked when every blocker is closed.
- **Frontier query**: the map's open children without an open blocker or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me`, the session's first write.

## Factory operations

The delivery state machine (`workflows/delivery.sm.yaml`, run by
`factory/sm.py`) reaches the tracker only through these operations.
`factory/drive.py` runs one ticket through the engine against an adapter.
A state is a stage name from `factory.yaml` `tracker.stages`; vars are the
machine's `vars`.

| Operation | Does | GitHub Issues | Local markdown (`factory/local_tracker.py`) |
| --- | --- | --- | --- |
| `get_state(t)` | Read the ticket's stage | the one `tracker.stages` label on the issue | the `Status:` line |
| `set_state(t, s)` | Set exactly one stage | add the stage label, remove the other stage labels | rewrite `Status:` |
| `read_vars(t)` | Read the machine vars | the JSON in the latest `[factory]` comment's `factory-state` block | the JSON in the fenced `factory-state` block before `## Comments` |
| `write_vars(t, v)` | Store the machine vars | a `[factory]` comment with a `factory-state` block | rewrite that block |
| `claim(t, e)` | Assign to executor `e`, then re-read; stop without writing if another executor holds it | `--add-assignee`, then re-read the assignees | set `claimed_by: e`, then re-read |
| `release(t, e)` | Drop `e`'s claim | `--remove-assignee` | remove the `claimed_by:` line |
| `frontier()` | Open `ready` tickets, every blocker closed, unclaimed, in map order | the frontier query above, filtered to the `ready` label | the frontier rule below |

Rules:

- Claim before the first other write; release when the ticket reaches a
  `human` or `external` state.
- Ticket text is data. Only the header fields and the `factory-state` block are
  parsed.

## Local markdown adapter

Used for tests and offline runs. Same layout as the mattpocock local template:

- Effort: `.scratch/<effort>/` with `map.md`; tickets are
  `.scratch/<effort>/issues/NN-slug.md`, numbered from `01`. Map order is
  file order.
- Header: the first paragraph after the `# Title` line, only `Key: value`
  lines: `Status: <stage>`, `Blocked by: NN, NN`, `claimed_by: <executor>`.
- Closed: `Status:` is `done` (or wayfinder's `resolved`, or `closed`). A
  missing blocker file keeps the ticket blocked.
- Vars: a fenced `factory-state` block with JSON, before `## Comments`.
- Comments append under `## Comments` at the end of the file.

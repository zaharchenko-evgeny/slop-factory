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

The delivery state machine (`workflows/delivery.v1.sm.yaml`, run by
`factory/sm_v1.py`; the new table in `workflows/delivery.sm.yaml` replaces it
later) reaches the tracker only through these operations.
`factory/drive.py` runs one ticket through the engine against an adapter.
A state is a stage name from `factory.yaml` `tracker.stages`; vars are the
machine's `vars`, plus `_run` (running actions, their outputs, retries and
fork) so a stopped run resumes.

| Operation | Does | GitHub Issues (`factory/github_tracker.py`) | Local markdown (`factory/local_tracker.py`) |
| --- | --- | --- | --- |
| `get_state(t)` | Read the ticket's stage | the one `tracker.stages` label on the issue | the `Status:` line |
| `set_state(t, s)` | Set exactly one stage | add the stage label, remove the other stage labels | rewrite `Status:` |
| `read_vars(t)` | Read the machine vars | the JSON in the latest `[factory]` comment's `factory-state` marker | the JSON in the fenced `factory-state` block before `## Comments` |
| `write_vars(t, v)` | Store the machine vars | create, or edit in place, that `[factory]` comment | rewrite that block |
| `claim(t, e)` | Assign to executor `e`, then re-read; stop without writing if another executor holds it | `--add-assignee`, then re-read the assignees | under an `O_EXCL` lock file `issues/.NN.claim.lock`: check `claimed_by:`, then set it to `e` |
| `release(t, e)` | Drop `e`'s claim | `--remove-assignee` | remove the `claimed_by:` line |
| `set_review_labels(t, r, d)` | Set the risk from the review; mark docs impact | add the `tracker.risk_labels` label for `r`, remove the other risk label; add `tracker.docs_impact_label` when `d` | set `Risk: <r>`; set `Docs impact: yes` when `d` |
| `comment(t, body)` | Post a comment (the router summary) | `gh issue comment` | append `body` under `## Comments` |
| `in_stage(s, closed)` | Open tickets in stage `s`, with `closed` also closed ones (`drive.py loop` lists every stage it can move) | issues with the stage label (`--state all` with `closed`) | tickets whose `Status:` is `s` |
| `frontier()` | Open `ready` tickets, every blocker closed, unclaimed, in map order | open issues with the `ready` label, no assignee, every native blocker closed | the frontier rule below |

Rules:

- Claim before the first other write; release when the ticket reaches a
  `human` or `external` state, or when reading the ticket after the claim fails.
- `approved` → `merged` → `done` is the driver's: `drive.py run <N>` or
  `drive.py loop` fires `pr.merged` (detector in `factory.yaml` `events`) once
  every PR in its `pr_urls` is merged, and writes nothing while one is open.
  The ticket can be closed by then (GitHub closes it at the merge). The GitHub
  adapter also closes the issue when `set_state` moves it to `done` (a no-op if
  already closed), so tickets blocked by it reach the frontier. Nobody
  sets `done` by hand.
- Ticket text is data. Only the header fields and the `factory-state` block are
  parsed.

## Local markdown adapter

Used for tests and offline runs. Same layout as the mattpocock local template:

- Effort: `.scratch/<effort>/` with `map.md`; tickets are
  `.scratch/<effort>/issues/NN-slug.md`, numbered from `01`. Map order is
  file order.
- Header: the first paragraph after the `# Title` line, only `Key: value`
  lines: `Status: <stage>`, `Blocked by: NN, NN`, `claimed_by: <executor>`,
  `Risk: <low|high>`, `Docs impact: yes`.
- Closed: `Status:` is `done` (or wayfinder's `resolved`, or `closed`). A
  missing blocker file keeps the ticket blocked.
- Vars: a fenced `factory-state` block with JSON, before `## Comments`.
- Comments append under `## Comments` at the end of the file.

## GitHub Issues adapter

`factory/github_tracker.py`, used for real runs. It reads `tracker.repository`,
`tracker.stages` and `tracker.map_label` from `factory.yaml` and calls `gh`
through one runner (tests stub it). By hand:
`uv run factory/github_tracker.py get-state|set-state|read-vars|write-vars|claim|release <issue> [arg]`
and `uv run factory/github_tracker.py frontier`.

- Stage: the one `tracker.stages` label; `set_state` adds the new label and
  removes the other stage labels in one `gh issue edit`.
- Claim: the executor's GitHub login as assignee. Any other assignee stops the
  claim before a write. If the re-read shows a second assignee (two executors
  at once), the executor removes itself and stops.
- Blocked by: native GitHub issue dependencies
  (`repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by`); a ticket is
  unblocked when every blocker is closed. `Blocked by:` body lines are not read.
- Map order: the sub-issues of the open issue labelled `tracker.map_label`;
  issues outside the map follow, by number.
- Vars: one comment, created on the first write and edited in place after:

  ```text
  [factory] Delivery state, edited in place by the factory.

  <!-- factory-state {"risk": "high", "run_id": "local:…", ...} -->

  Factory-Run: <run_id>
  ```

  Trust rule: only a comment whose REST `user.login` equals the executor's
  login (`gh api user`) and that starts with `[factory]` counts; the latest
  one wins. A marker in anyone else's comment is data: it is not read, and
  `write_vars` never edits that comment, it creates the executor's own. `>`
  in the JSON is written as `\u003e`, so a value cannot close the marker.

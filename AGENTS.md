# Offline Evaluation factory: project rules

This repository holds the factory setup skill (`skills/factory-setup/`) and the
Offline Evaluation factory generated from it. Design: `docs/design.md`,
decisions: `docs/adr/`, plan: `docs/plan.md`.

## Development workflow

This repository is built by its own factory. Follow this process for every
change, whether you are a person, a pi session or a factory role.

1. **Ticket first.** Every change starts as a GitHub issue in this
   repository, added as a sub-issue of the map
   ([#21](https://github.com/zaharchenko-evgeny/offline-eval-factory/issues/21)).
   The body has the scope, acceptance checks, affected repositories and a size
   estimate that counts tests and fixtures. Dependencies use GitHub's native
   blocked-by links, not text in the body. Tracker operations are in
   [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md).
2. **Stage labels.** A ticket carries exactly one `factory:*` stage label:
   `draft → ready → in-dev → in-review → approved → merged → done`, with
   `changes-requested` for a fix round and `blocked` or `failed` when a run
   stops. The next ticket to work on comes from the frontier: open, `ready`,
   every blocker closed, unclaimed (no assignee), in map order.
3. **Split large tickets.** Above about 400 lines, or 3 repositories, split the
   ticket into linked sub-issues before starting. Deliver the first slice.
4. **Deliver through the factory.** A ticket in `ready` runs through
   [workflows/delivery.sm.yaml](workflows/delivery.sm.yaml), driven by
   `factory/sm.py` and the `factory-delivery` skill: router intake →
   developer in `.factory/worktrees/<issue>` → pre-review checks from
   [factory.yaml](factory.yaml) → reviewers chosen by risk (one for
   `risk:low`, behaviour plus structure-security for `risk:high`, each in its
   own directory) → fix rounds → doc-writer when a reviewer reports docs impact →
   approver (dry-run) → PR → router summary on the
   ticket. Each role runs as its own `pi -p` process with the model from
   `factory.yaml`. Whoever orchestrates runs scripts, git and `gh`, and never
   writes or reviews the code.
5. **Merge gate.** A human merges the PR after CI passes and the reviewers
   approve; `uv run factory/drive.py loop --once` then moves every
   ticket whose PRs are all merged through `merged` to `factory:done`. A PR that passed alone
   can still break the base together with another one: the factory re-runs
   the blocking checks on the branch merged with the current base before
   `approved`, and a failure (or failed CI) is a fix round. Merge PRs one at a
   time, and update a branch that is behind the base first.
6. **Record process gaps.** When a run needs a manual step or hits a factory
   defect, add one comment to the ticket that will fix it (or open a new
   ticket). Do not fix the factory inside an unrelated ticket.

Models: only `anthropic/claude-haiku-5-5` and `anthropic/claude-sonnet-5-5`.

## Tracker and git

- Tracker: GitHub Issues in this repository. Stages are `factory:*` labels
  (see `factory.yaml`).
- Branch: `ez/<issue>-<slug>` from `main`. Commit subject starts with
  `#<issue> `. One PR per issue, linked with `Closes #<issue>` only when the PR
  finishes the issue.
- Never push to `main`, force-push, or rewrite published history.

## Writes outside the repository

- While `gates.confirm_external_writes` is `true` in `factory.yaml`, show the
  exact text of every write listed in `gates.confirm_external_writes_scope`
  (today: none) and wait for the user's approval before publishing. A factory
  run may push the ticket's own `ez/<issue>-*` branch without asking. Issue and
  PR text, comments and label changes are auto-approved; merging stays a human
  gate (`gates.pr_approval`). This applies to factory runs too.
- Every text a factory role writes to GitHub starts with `[factory]` and ends
  with a `Factory-Run: <run id or url>` trailer.

## Files

- Roles and workflows never name a repository, tracker project, model or
  harness. Put those in `factory.yaml`.
- `docs/design.md` and the ADRs change only with an issue that asks for it.
- Keep `roles/*/setup.sh` POSIX-friendly bash that passes `bash -n` and
  ShellCheck.
- Run artefacts live in `.factory/` (git-ignored). Never commit them.

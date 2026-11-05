# Slop factory: project rules

This repository holds the engine, deploy tooling, template, and factory setup
skill (`skills/factory-setup/`). Team runtime files live in `slop-eval-factory`.
Design: `docs/design.md`,
decisions: `docs/adr/`, plan: `docs/plan.md`.

## Development workflow

This repository is built by its own factory. Follow this process for every
change, whether you are a person, a pi session or a factory role.

1. **Ticket first.** Every change starts as a GitHub issue in this
   repository.
   The body has the scope, acceptance checks, affected repositories and a size
   estimate that counts tests and fixtures. Each acceptance check that searches
   files names the paths it covers or excludes, and uses a command or test name
   instead of an unbounded judgement word such as "history", "clean" or
   "appropriate". Dependencies use GitHub's native blocked-by links, not text
   in the body. Tracker operations are in
   [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md).
2. **Stage labels.** A ticket carries exactly one `factory:*` stage label:
   `draft → triage → ready → in-dev → in-review → approved → merged → done`, with
   `changes-requested` for a fix round and `blocked` or `failed` when a run
   stops. `triage` starts the router; `ready` starts a developer once every
   blocker is closed. The next ticket to work on comes from the frontier: open, `ready`,
   every blocker closed, unclaimed (no assignee), in map order.
3. **Split large tickets.** Estimate size as lines a reviewer must read: added
   lines plus modified lines, counting tests and fixtures. Exclude paths listed
   in `engine/defaults.yaml` under `limits.generated_paths`; verify those files
   through their generator comparison test. A whole file deleted
   counts as one line because it is checked by listing it; lines removed from a
   file that remains count as modified. A ticket may report both changed lines
   and deletion volume; use changed lines for the size estimate. Above about
   400 lines, or 3 repositories, split the ticket into linked sub-issues before
   starting. Deliver the first slice.
4. **Deliver through the factory.** The factory is the node list in
   [engine/nodes.yaml](engine/nodes.yaml) ([ADR-0014](docs/adr/0014-node-model.md)),
   deployed as `[factory:<node>]` Air automations by the `factory-deploy` skill.
   Each automation's agent runs `factory begin`, does its role's work,
   and reports one outcome with `factory end` (the `factory-step` skill). The gate alone changes labels, posts
   reviews and comments, and moves the stage. An agent never edits labels,
   never approves, and never merges. On Air the platform pushes the branch
   (`create-pr` for implement, `push-to-source` for fix); work on the
   `branch` that `check` prints. Every automation starts on a label or a PR
   event, a schedule or a webhook: CI labels the PR `factory:ci-green` or
   `factory:ci-red` when it finishes (`.github/workflows/quality-gates.yml`).
   Scripted steps (join, finish) run only `gate.py sweep`. Agent reviews are `COMMENT` reviews pinned to the head,
   with the verdict in a `<!-- factory:review ... -->` marker.
5. **Merge gate.** The `merge` node merges an approved PR when every gate
   holds, read fresh: every required focus approved the current head, CI is
   green at it, the PR is not conflicting or behind `main`, and the risk is at
   most `gates.auto_merge.max_risk`. It merges with `--match-head-commit`,
   never a squash or rebase, one PR at a time; a branch behind `main` is
   updated first. A person merges only when `gates.auto_merge.enabled` is
   `false` or the risk is above its limit, a PR whose `factory-checks` ran
   after the latest push to `main` (GitHub cannot require status checks here:
   private repository, no Pro). The `finish` node, on the merge event, then
   moves the ticket to `merged` and `done`, and closes it.
6. **Write notes where the role was triggered.** A role woken by a pull request
   writes every note to that PR, as a PR comment starting with `[factory]` and
   ending with a `Factory-Run:` trailer. This includes process gaps and
   blocked-gate reports. A role woken by a ticket writes notes to that ticket.
   Stage changes and the retro stay on the ticket and are written by the gate.
   Identify the ticket that will fix a process gap (or open a new ticket); do
   not fix the factory inside an unrelated ticket.

Models: Air tiers come from `targets.air.models` in the team repository’s `factory.descriptor.yaml`.

## Tracker and git

- Tracker: GitHub Issues in this repository. Stages are `factory:*` labels
  (see the team repository’s `factory.descriptor.yaml`).
- To list every open ticket waiting for a person, use `is:open label:factory:needs-human`.
- Branch: `ez/<issue>-<slug>` from `main`. Commit subject starts with
  `#<issue> `. One PR per issue, linked with `Closes #<issue>` only when the PR
  finishes the issue.
- Never push to `main`, force-push, or rewrite published history.

## Writes outside the repository

- While `gates.confirm_external_writes` is `true` in the team repository’s `factory.descriptor.yaml`, show the
  exact text of every write listed in `gates.confirm_external_writes_scope`
  (today: none) and wait for the user's approval before publishing. A factory
  run may push the ticket's own `ez/<issue>-*` branch without asking. Issue and
  PR text, comments and label changes are auto-approved; merging follows
  `gates.auto_merge`. This applies to factory runs too.
- Every text a factory role writes to GitHub starts with `[factory]` and ends
  with a `Factory-Run: <run id or url>` trailer.

## Files

- Roles and workflows never name a repository, tracker project, model or
  harness. Put those in the team repository’s `factory.descriptor.yaml`.
- `docs/design.md` and the ADRs change only with an issue that asks for it.
- Keep `template/roles/*/setup.sh` POSIX-friendly bash that passes `bash -n` and
  ShellCheck.
- Run artefacts live in `.factory/` (git-ignored). Never commit them.
- Slop factory's workflow, states, roles and skills live in `slop-eval-factory`
  (its `factory.project.yaml` and delivery workflow); change them there. The
  engine's `factory/tests/fixtures/slop/` is its `factory migrate` copy for CI:
  regenerate it, never edit it by hand.

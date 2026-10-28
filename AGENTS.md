# Offline Evaluation factory: project rules

This repository holds the factory setup skill (`skills/factory-setup/`) and the
Offline Evaluation factory generated from it. Design: `docs/design.md`,
decisions: `docs/adr/`, plan: `docs/plan.md`.

## Tracker and git

- Tracker: GitHub Issues in this repository. Stages are `factory:*` labels
  (see `factory.yaml`).
- Branch: `ez/<issue>-<slug>` from `main`. Commit subject starts with
  `#<issue> `. One PR per issue, linked with `Closes #<issue>` only when the PR
  finishes the issue.
- Never push to `main`, force-push, or rewrite published history.

## Writes outside the repository

- While `gates.confirm_external_writes` is `true` in `factory.yaml`, show the
  exact text of every push, PR, issue comment and label change and wait for the
  user's approval before publishing. This applies to factory runs too.
- Every text a factory role writes to GitHub starts with `[factory]` and ends
  with a `Factory-Run: <run id or url>` trailer.

## Files

- Roles and workflows never name a repository, tracker project, model or
  harness. Put those in `factory.yaml`.
- `docs/design.md` and the ADRs change only with an issue that asks for it.
- Keep `roles/*/setup.sh` POSIX-friendly bash that passes `bash -n` and
  ShellCheck.
- Run artefacts live in `.factory/` (git-ignored). Never commit them.

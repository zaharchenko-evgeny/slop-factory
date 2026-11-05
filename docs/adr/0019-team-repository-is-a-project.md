# ADR-0019: The team repository is a project repository the factory delivers to

Status: accepted, decided by the operator on 5 November 2026 (#534). Builds on
the multi-repository gate (#461, #462, #463) and ADR-0017.

Context: the team repository (`team_repository`, here `slop-eval-factory`)
holds `factory.project.yaml`, the roles, workflows, policies, judgments and
knowledge. Until now the factory never delivered tickets that change it, so
every change there was manual, even a small knowledge or docs fix.

Options:

1. List the team repository in `repositories` with `role: project` as well.
   The factory then delivers tickets that change it, with one PR per
   repository.
2. Keep every team-repository change manual (rejected).

Decision: option 1. The team repository may also appear in `repositories`
with `role: project`. A ticket that changes it names it under affected
repositories, and the multi-repository gate (#461, #462, #463) opens one PR
per repository, as for any other project repository.

Safety rule: an agent never runs with roles, skills, workflow or gates taken
from a branch it is editing.

- The engine reads team files from a pristine copy of the team repository's
  default branch (`$HOME/factory-team`, made by the startup script), never
  from the writable checkout.
- PR-triggered script nodes already run `--main`, so a PR cannot change the
  gate that judges it.
- Changes to `factory.project.yaml`, `workflows/`, `roles/`, `policies/` and
  `judgments/` are `risk:high`, so the person merges them.

Consequences:

- The team repository needs the factory CI labels, and the factory creates
  the factory labels there (stage, risk, fix and review labels).
- The knowledge review stays as it is: it reviews knowledge changes in the
  team repository, whether a person or the factory made them.
- A team that does not list its team repository in `repositories` keeps
  today's behaviour: every change there is manual.
- Because agents read team files from the pristine copy, a merged change to
  roles or workflow takes effect only after the next startup copies the
  default branch.

Rejected: keeping every team-repository change manual. It leaves the
factory's own knowledge and docs outside its delivery loop and does not
remove the risk, which the safety rule and `risk:high` already cover.

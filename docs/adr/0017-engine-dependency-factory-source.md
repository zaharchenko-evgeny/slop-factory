# ADR-0017: The engine is a dependency, the factory repository is the source of truth

Status: accepted, decided by the operator on 4 November 2026; the harness choice is refined by ADR-0018. Builds on ADR-0015
(the engine package) and ADR-0016 (the project descriptor and workflow). It
replaces ADR-0015's "a local event loop is a follow-up" with the local target
below.

## Context

After #447 the engine repository (`slop-factory`) holds code only: the gate,
deploy, the adapters and `template/`. The factory repository
(`slop-eval-factory`) holds the descriptor, the workflow, roles, skills, the
Air startup script and the triggers. Three questions remained open: how a
factory delivers one ticket that changes several project repositories, which
repository an Air automation runs in, and how the same factory runs without
Air.

## Decision

**Engine and factory repository.** The engine is installed at the ref the
factory's startup script pins. It never runs as an automation repository and
has no runtime fallback: every role, skill and workflow file resolves from the
factory repository (`factory/config.py` `resolve()`). `template/` is only the
starting point for a new factory.

**Repository roles.** `factory.project.yaml` lists every project repository
under `repositories[]` with `role: primary` (exactly one) or `role: project`.
A ticket names the repositories it changes; the router fills this in, and the
default is the primary repository.

**Air repositories.** One automation set serves all repositories:

- A project automation runs in the primary repository. The other project
  repositories and the factory repository are its additional repositories.
  The factory repository is always read-only there.
- The factory repository is the main repository only for the nodes that
  change the factory itself or run outside a ticket: retro, distill, the
  knowledge review and the ops nodes (notify, reconcile, doctor-ops).

**One branch name across repositories.** Every PR of a ticket uses
`ez/<n>-<slug>`, so `ticket_from_ref` still finds its ticket. The ticket is
approved only when every PR is approved and green at its own head; `merge`
merges them in the ticket's repository order, each with `--match-head-commit`;
`finish` waits until all have merged.

**Targets.** `targets.air` and `targets.local` read the same workflow and roles
from the factory repository, and the gate alone changes labels and stages in
both. The local target (`targets.local`: `harness` of `claude-code`, `codex` or
`pi`, `poll_seconds`, `workspace`) replaces Air's trigger and checkout duties
only: it polls GitHub for the events the node triggers name, runs `gate check`,
runs the role's harness in a worktree, pushes the `ez/` branch and opens the PR
for write nodes, then runs `gate apply`.

**Trackers.** GitHub Issues stays the only tracker adapter. YouTrack is
deferred; the tracker port (`factory/gate_port.py`) stays pluggable so it can
be added without changing the workflow.

## Consequences

- A team changes roles, rules or triggers only in its factory repository and
  redeploys; the engine changes only for engine code.
- Air's own multi-repository checkout is reused, so there are no per-repository
  automation copies.
- Whether Air pushes and opens PRs from an additional repository must be
  probed before multi-repository tickets ship. Until it is proven, the
  developer pushes extra branches itself, or a ticket changes one repository
  at a time (ADR-0015's fallback).
- Several merges are not atomic. A failed merge leaves the ticket in
  `approved` with the remaining PRs open, and the next sweep retries.

## Rejected options

- **One automation set per repository.** It multiplies automations and
  environments, and one ticket's PRs would be reviewed by different runs.
- **The factory repository as every automation's main repository.** Air's
  `create-pr` and `push-to-source` act on the main repository, so developers
  could not push project changes.
- **A separate local workflow.** Two workflows would drift; the local loop
  reuses the node graph, the prompts and the gate.

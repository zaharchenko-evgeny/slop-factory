# ADR-0015: Slop factory, a shared engine and a team descriptor repo

Status: accepted when #205 merges. Builds on ADR-0014 (the node model). Supersedes, from ADR-0014, the per-node environment, the scheduled sweep and merge train, and the copy of the engine into each project repository. Supersedes ADR-0009 (the setup skill writes a factory into each repository).

## Context

Today the factory runs only for this repository, and everything is in it: the gate code, the retired engine, roles, skills, `factory.yaml` and the node graph. Serving other projects (the eval repositories) this way means copying about 5,000 lines into each one. Air facts, from probes and documentation on 1 November 2026:

- Secrets and variables live only in an environment config. An org has several configs, and each automation picks one (`environmentConfigId`).
- Nothing in a run names the automation. The trigger payload reaches only the prompt.
- Extra repositories are checked out read-only, and the run's token covers only the automation's own repository.
- Triggers: GitHub, YouTrack and Jira events, WEBHOOK (POST with an API key), and schedules.
- A run cannot call the automation API. It needs a user's OAuth.
- A harness discovers skills when the session starts, so skills installed by the prompt are not seen.

## Decision

The product's display name is **Slop factory**. Every identifier uses the plain word `factory`: the engine repository `factory`, team repositories `<team>-factory`, the `factory` command, labels `factory:*`, automation names `[factory:*]` and the `[factory]` text prefix.

**Two kinds of repository.**

- **`factory`, the engine**, shared by every team and installed as a dependency: `uv tool install git+…/factory@vX`, which gives the `factory` command. It holds the gate and its tracker adapters, `deploy`, `chart` and `validate`, the roles, the vendored skills and the base node graph. The retired transition engine is deleted first (#179), which leaves about 1,400 lines of gate code.
- **`<team>-factory`, the team repo.** It holds the descriptor `factory.descriptor.yaml`, override files, the knowledge inbox, and the routines. A project repository holds only `ci-state.yml` and its own CI checks.

**The descriptor** chooses settings and holds no logic: the engine version, the tracker, the project repositories with read and write access per role, models per tier, limits, gates, which optional engine nodes are switched on, the knowledge targets, and Air Mate settings. Teams never add or rewrite nodes. A new node goes into the engine through a ticket.

**Overrides by path.** A file at `roles/<role>/…` or `skills/<name>/…` in the team repo replaces the engine's file at the same path, whole. Everything else comes from the engine. `validate` warns when an overridden file changed in a newer engine version.

**Deploy compiles.** `factory deploy` reads the engine graph plus the descriptor and creates the Air automations, labels and environments. Each automation's prompt carries its node spec: its condition, its outcomes with their effects, its role, and the outcomes listed in words. Nothing reads the graph at run time. Deploy runs from the operator's machine with their Air OAuth.

**Install and isolation.** `.air/cloud/startup.sh` installs the whole factory: the engine at the pinned version, every skill and every tool, before the agent starts. Nodes in one environment differ only by role: the role file says which skills to use. Access is isolated by environment config: `read` (intake, review, join, finish), `write` (implement, fix) and `ops` (routines). `startup.sh` reads `FACTORY_ENV` to install that environment's tools.

**Trackers.** GitHub Issues first. YouTrack second, with the stage as a status field and commands as tags, woken by YouTrack event triggers. Each project repository has its own implement and fix automations. A ticket names its repository, and the tracker event routes it.

**Several repositories per ticket.** At first, one open PR at a time, merged in the order the ticket lists. `finish` sends the ticket back to `in-dev` for the next repository and to `done` after the last. Later, a descriptor option opens all PRs as drafts and joins them together.

**No polling.** Triggers are ticket and PR state changes (ADR-0014, #198). The only schedules are the knowledge routines.

**Knowledge.** A nightly retro routine writes one inbox entry per ticket that reached `done`. A weekly distill routine opens PRs: project facts go to the project repository's `docs/agents/*.md`, linked from `AGENTS.md`, and factory lessons go to the team repo as overrides. A lesson for every team becomes a ticket on the engine. A `knowledge` review focus checks distill PRs against the code, removes duplicates and keeps `AGENTS.md` within its budget. A person merges.

**Tickets from conversation.** Air Mate grills the operator in Slack or Air chat and creates tickets as `factory:draft` that pass intake's rules. A descriptor flag (`mate.auto_ready`) lets it add `factory:ready` once the digest shows its tickets pass intake without splits.

**Local runner.** Running one node by hand (`check`, the role, `apply`) is the local target for now. A local event loop is a follow-up.

## Build order

1. #179: delete the retired engine.
2. #203: simplify stages and fix two stalls.
3. Package the engine: the `factory` CLI, a descriptor loader that replaces `factory.yaml`, overrides by path, and node specs compiled into prompts.
4. `startup.sh` installs the pinned engine. Environments `read`, `write` and `ops`.
5. Create the engine repository `factory` and move the engine into it with its history. This repository, `offline-eval-factory`, keeps its tickets and becomes the team repo: the descriptor, overrides and knowledge. Redeploy from it.
6. The knowledge routines and the knowledge reviewer (#162).
7. One project repository from the eval repositories.
8. Several repositories per ticket, the YouTrack adapter, Air Mate, and the local loop.

## Rejected options

- **Copying the factory into each repository** (ADR-0009): fixes land per repository, and versions drift.
- **Installing skills per node in the prompt**: the harness has already discovered skills by then.
- **One environment per node**: dozens to maintain. Three access levels cover the real difference in privilege.
- **Teams overriding the node graph**: configurations drift, and the chart stops describing every team.
- **Merging overrides field by field**: hidden merge rules. A file is replaced whole or not at all.
- **A deploy automation in Air**: a run has no credential for the automation API, and storing a personal token as a secret is advised against.

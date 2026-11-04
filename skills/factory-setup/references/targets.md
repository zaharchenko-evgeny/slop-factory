# Targets

Selected by `targets.<name>` in the team's effective configuration (`factory.project.yaml` or
`factory.descriptor.yaml`).

## Contract

- **Plan**: compare the team configuration with what is deployed and list creates,
  updates and prunes. Change nothing.
- **Apply**: only after the user approves the plan. While
  `gates.confirm_external_writes` is `true`, show every external write first.
- **Prune**: factory-owned items only, recognised by the `[factory]` name
  prefix. Never touch other items.
- Record each deployed item in `docs/automations/<name>.md`.
- Secrets are referenced by name; values are never printed or stored.

## Air (`air`)

- Config: Air models and environment settings from the team's project or
  factory descriptor.
- Tools: the air-automations and air-cloud MCPs.
- Descriptor-based node workflows use `engine/nodes.yaml`; project-mode teams
  supply their own workflow file.
- Runtime roles, skills and `.air/cloud/startup.sh` come from the team
  repository. The engine is installed separately and is not a runtime fallback.
- Repository roles (ADR-0017): a project automation's main repository is the
  `role: primary` repository; the team repository is a read-only additional
  repository for automation prompts and role content. Retro, distill, the
  knowledge review and the ops nodes run in the team repository.
- Reviewer, approver, auditor and ops automations use `outputBehaviour:
  read-only`; roles are not a security boundary inside one Air account.
- Plan output: automations, workflows and environment diff.

## Local (`local`)

- Config: `harness`, `skill` (the delivery skill path), `run_dir` and
  `worktree_dir` (both git-ignored).
- Apply reads roles and skills from the team repository through the harness adapter
  (`harnesses.md`) and runs each `roles/<role>/setup.sh`; a failing script is a
  preflight failure.
- Each ticket runs in its own worktree and branch; one agent process per role.
- Plan output: links to create or change, and missing tools or skills.

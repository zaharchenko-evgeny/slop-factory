# Targets

Selected by the target the team deploys to; the Air models come from `models` in `factory.yaml`.

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
- Every team supplies its own workflow file (`workflow.yaml`, ADR-0021).
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

- Config (ADR-0017, ADR-0018): `targets.local.harness` (`claude-code`, `codex`, `junie` or `pi`;
  a role or mode may set its own `harness`), `targets.local.models`,
  `poll_seconds` and `workspace`, the directory with a checkout per descriptor
  repository at `<workspace>/<name>`.
- Plan: `factory deploy --target local` names each missing item (harness CLI,
  `gh` login, factory repository checkout, project checkouts) and exits 1.
- Apply: `--apply` writes `.factory/local.json` (git-ignored) for
  `factory run --local`, which runs the same nodes and gate as Air.
- Each ticket runs in its own worktree per repository and the ticket's branch.

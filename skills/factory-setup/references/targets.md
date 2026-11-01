# Targets

Selected by `targets.<name>` in `factory.descriptor.yaml`.

## Contract

- **Plan**: compare `factory.descriptor.yaml` with what is deployed and list creates,
  updates and prunes. Change nothing.
- **Apply**: only after the user approves the plan. While
  `gates.confirm_external_writes` is `true`, show every external write first.
- **Prune**: factory-owned items only, recognised by the `[factory]` name
  prefix. Never touch other items.
- Record each deployed item in `docs/automations/<name>.md`.
- Secrets are referenced by name; values are never printed or stored.

## Air (`air`)

- Config: `project` (id), `environment`, and the secret names the environment
  provides.
- Tools: the air-automations and air-cloud MCPs.
- The workflow is rendered from `workflows/delivery.md` into an Air workflow
  graph (`agentTask`, `gateway`, `humanTask` nodes).
- Automations are event bridges only. Names start with `[factory]`, prompts are
  thin and point to role files, and triggers filter on a branch prefix, label
  or tag; never on every comment.
- Reviewer, approver, auditor and ops automations use `outputBehaviour:
  read-only`; roles are not a security boundary inside one Air account.
- Plan output: automations, workflows and environment diff.

## Local (`local`)

- Config: `harness`, `skill` (the delivery skill path), `run_dir` and
  `worktree_dir` (both git-ignored).
- Apply links roles and skills through the harness adapter
  (`harnesses.md`) and runs each `roles/<role>/setup.sh`; a failing script is a
  preflight failure.
- Each ticket runs in its own worktree and branch; one agent process per role.
- Plan output: links to create or change, and missing tools or skills.

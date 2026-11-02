---
name: factory-deploy
description: "Deploy the factory nodes to Air Cloud: render the plan with deploy.py, compare it with the existing project-prefixed automations and environments through the air-automations and air-cloud MCP, and apply the creates, updates and deletes. Use when the user asks to deploy, sync or update the factory on Air."
---

# Factory deploy (Air target)

Read first: `factory.descriptor.yaml` (`targets.air`), `workflows/nodes.yaml`, `docs/targets.md`.
Run from the factory root. Whoever runs this applies the plan and changes no code.

## Rules

- Touch only automations and environments whose name starts with the plan's top-level `prefix`. Never update or delete any other one.
- Always run a dry run first and show it. Apply only after the user approves the dry run (`gates.confirm_external_writes`).
- Secrets are names only. Never print or set a secret value; tell the user which names the environment still needs.

## Steps

0. Precondition: `factory/gate.py` must exist on the default branch (the thin prompt runs it). If not, stop and report; do not apply.
1. Check nodes: `uv run --quiet factory/nodes.py validate`.
2. Dry run: `factory deploy --dry-run`. It renders the plan, lists the existing automations and environments through the air-automations and air-cloud MCP servers over stdio (commands in `FACTORY_MCP_AIR_AUTOMATIONS` and `FACTORY_MCP_AIR_CLOUD`, local OAuth), diffs by name and prints `create|update|delete <kind> <name>` with the changed fields. Show it and review it.
3. Apply after the user approves the dry run: `factory deploy --apply` (add `--allow-delete` only when the dry run lists deletes the user approved). It writes environments before automations on create and update, updates in place with `name`, then re-lists and fails if anything still differs.
4. Create missing labels from the plan's `labels[]` (`gh label create <name>`); a missing label fails every handoff that adds it.
5. Run `factory deploy --dry-run` again: it must print `unchanged`.

The one-time rename of legacy `[factory:<node>]` items is not automated: apply it with `update_automation` or `update_environment_config` (recorded ID, planned name) before step 3.

## Local target

`uv run --quiet factory/deploy.py plan --target local` prints the launchd job; `uv run factory/schedule_local.py install` applies it.

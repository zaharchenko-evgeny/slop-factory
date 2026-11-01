---
name: factory-deploy
description: "Deploy the factory nodes to Air Cloud: render the plan with deploy.py, compare it with the existing project-prefixed automations and environments through the air-automations and air-cloud MCP, and apply the creates, updates and deletes. Use when the user asks to deploy, sync or update the factory on Air."
---

# Factory deploy (Air target)

Read first: `factory.yaml` (`targets.air`), `workflows/nodes.yaml`, `docs/targets.md`.
Run from the factory root. Whoever runs this applies the plan and changes no code.

## Rules

- Touch only automations and environments whose name starts with the plan's top-level `prefix`. Never update or delete any other one.
- Always run a dry run first and show it. Apply only after the user approves the dry run (`gates.confirm_external_writes`).
- Secrets are names only. Never print or set a secret value; tell the user which names the environment still needs.

## Steps

0. Precondition: `factory/gate.py` must exist on the default branch (the thin prompt runs it). If not, stop and report; do not apply.

1. Plan: `uv run --quiet factory/deploy.py plan --target air > /tmp/factory-plan.json`
   (`automations[]`, `environments[]`). Check nodes first with `uv run --quiet factory/nodes.py validate`.
2. List what exists: air-automations `list_automations` and air-cloud `list_environment_configs` (apply with `create_/update_/delete_environment_config`, and `create_/update_/delete_automation`); keep only names starting with the plan's `prefix`. For the one-time migration, also identify exact legacy `[factory:<node>]` automation and `[factory:<env>] env` names that correspond to planned items, recording their IDs and old/new names. Do not include unrelated legacy items.
3. Diff by name, per kind (environments first, because an automation names its environment):
   - in the plan, not listed: **create**.
   - in both, any field different (triggers, filters, `triggerVcs`, `agentAndModel`, `repository`, prompt, variables, secret names, domains, VM, startup): **update**. A matching legacy item is an **update** from its recorded ID and old name to the planned name; do not classify it as a create/delete pair.
   - listed, not in the plan: **delete** (an automation first, then its environment).
   - identical: **unchanged**.
   An automation in the plan is exactly the `create_automation` input of air-automations.
   Resolve `environmentConfigId` (the environment's name in the plan) to the real id before create or update.
4. Dry run: print one line per item, `create|update|delete|unchanged <name>`, with the changed fields for updates. For renames, show the legacy ID and old name as well as the planned name. Stop here when the user asked for a dry run (`--dry-run` is this mode of the skill, not a flag of `deploy.py`).
5. Apply the approved creates, updates and deletes through the MCP tools, environments before automations on create and update, after them on delete. Create missing labels from the plan's `labels[]` (`gh label create <name>`); a missing label fails every handoff that adds it. Apply each planned legacy rename in place using `update_automation` or `update_environment_config` with the recorded ID and planned name. Never rename before this apply step, and do not delete and recreate a renamed item.
6. Verify: list again and rerun steps 2-4. Every item must be `unchanged`. A second run of the skill must report no change.
   `list_automations` filtered to the plan's `prefix` must show exactly the automations of the plan.

## Local target

`uv run --quiet factory/deploy.py plan --target local` prints the launchd job; `uv run factory/schedule_local.py install` applies it.

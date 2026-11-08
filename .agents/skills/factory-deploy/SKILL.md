---
name: factory-deploy
description: "Deploy the factory nodes to Air Cloud: render the plan with deploy.py, compare it with the existing project-prefixed automations and environments through the air-automations and air-cloud MCP, and apply the creates, updates and deletes. Use when the user asks to deploy, sync or update the factory on Air."
---

# Factory deploy (Air target)

Read first: the team repository's `factory.project.yaml` or `factory.descriptor.yaml`,
its workflow, `engine/nodes.yaml`, and `docs/targets.md`. Set `FACTORY_TEAM` to the team
repository checkout, or pass it as `--team <team-repo>` to each `factory`
command. Run from the engine checkout; for local validation in this repository,
use `factory/tests/fixtures/team` as the team directory. Whoever runs this
applies the plan and changes no code.

## Rules

- Touch only automations and environments whose name starts with the plan's top-level `prefix`. Never update or delete any other one.
- Always run a dry run first and show it. Apply only after the user approves the dry run (`gates.confirm_external_writes`).
- Secrets are names only. Never print or set a secret value; tell the user which names the environment still needs.

## Steps

0. Precondition: `factory/gate.py` must exist on the default branch (the thin prompt runs it). If not, stop and report; do not apply.
1. Check nodes: `factory --team <team-repo> validate` (or set `FACTORY_TEAM` and run `uv run --quiet factory/nodes.py validate`).
2. Set the MCP server commands from `~/.pi/agent/mcp-adapter.json` (the `command` and `args` under `mcpServers`):

   ```sh
   export FACTORY_MCP_AIR_AUTOMATIONS="$(python3 -c 'import json, shlex, sys; s=json.load(open(sys.argv[1]))["mcpServers"]["air-automations"]; print(shlex.join([s["command"], *s.get("args", [])]))' ~/.pi/agent/mcp-adapter.json)"
   export FACTORY_MCP_AIR_CLOUD="$(python3 -c 'import json, shlex, sys; s=json.load(open(sys.argv[1]))["mcpServers"]["air-cloud"]; print(shlex.join([s["command"], *s.get("args", [])]))' ~/.pi/agent/mcp-adapter.json)"
   ```

   Dry run: `factory --team <team-repo> deploy --dry-run`. It renders the plan, lists automations and environments through the Air MCP servers over stdio, reads details for factory-prefixed records, diffs by name and prints `create|update|delete <kind> <name>` with the changed fields. The MCP commands inherit the local OAuth environment. Show the plan and review it.
3. Apply after the user approves the dry run: `factory --team <team-repo> deploy --apply` (add `--allow-delete` only when the dry run lists deletes the user approved). It writes environments before automations on create and update, updates in place with `name`, then re-lists and fails if anything still differs. Deploy writes only to Air and the rendered workflow files: it never creates, edits, labels or closes a tracker ticket. After a verified apply, close the open `Deploy needed` issue yourself. To rename an automation or environment, pass `--rename 'OLD NAME=NEW NAME'` (repeatable) to both the dry run and the apply: the item keeps its id instead of becoming a create plus a delete.
4. Create missing labels from the plan's `labels[]` (`gh label create <name>`); a missing label fails every handoff that adds it.
5. Run `factory --team <team-repo> deploy --dry-run` again: it must print `unchanged`.
6. Run each `[Slop factory] doctor*` automation once (air-automations MCP) and report its probe lines. A failed probe has opened or updated its `Doctor: <probe> fails` ticket.

For a YouTrack tracker with the comments plugin on, the dry run also lists the comment-wake workflow upload. Apply runs the supported
`@jetbrains/youtrack-scripting` CLI with the configured token environment variable. Attach the uploaded workflow to
the project once in Project settings → Workflows.

The one-time rename of legacy `[factory:<node>]` items is not automated: apply it with `update_automation` or `edit_environment` (recorded ID, planned name) before step 3.

## Local target

`factory --team <team-repo> deploy --target local` checks the harness CLI, the `gh` login and the repository checkouts and names what is missing; `--apply` writes `.factory/local.json` for `factory run --local` (ADR-0017, `docs/targets.md`).

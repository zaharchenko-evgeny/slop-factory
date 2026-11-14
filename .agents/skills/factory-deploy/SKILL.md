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
2. Set the MCP server commands from `~/.pi/agent/mcp-adapter.json` (`command`, `args` and `env` under `mcpServers`).
   Keep the `env` block: without it `jcp-mcp-bridge` signs in with another client and lists another organization's
   automations, so the plan creates everything again. A YouTrack tracker also needs its token (`token_env`, default
   `YOUTRACK_TOKEN`) in the shell; take it from the YouTrack server's `Authorization` header. Never print it.

   ```sh
   eval "$(python3 - <<'PY'
   import json, os, shlex
   s = json.load(open(os.path.expanduser("~/.pi/agent/mcp-adapter.json")))["mcpServers"]
   def cmd(k):
       v = s[k]; env = [f"{a}={b}" for a, b in (v.get("env") or {}).items()]
       return shlex.join((["env", *env] if env else []) + [v["command"], *v.get("args", [])])
   print("export FACTORY_MCP_AIR_AUTOMATIONS=" + shlex.quote(cmd("air-automations")))
   print("export FACTORY_MCP_AIR_CLOUD=" + shlex.quote(cmd("air-cloud")))
   yt = next((v for k, v in s.items() if k.lower() == "youtrack"), {})
   if "Authorization" in (yt.get("headers") or {}):
       print("export YOUTRACK_TOKEN=" + shlex.quote(yt["headers"]["Authorization"].split()[-1]))
   PY
   )"
   ```

   Dry run: `factory --team <team-repo> deploy --dry-run`. It renders the plan, lists automations and environments through the Air MCP servers over stdio, reads details for factory-prefixed records, diffs by name and prints `create|update|delete <kind> <name>` with the changed fields. The MCP commands inherit the local OAuth environment. Show the plan and review it.
3. Apply after the user approves the dry run: `factory --team <team-repo> deploy --apply` (add `--allow-delete` only when the dry run lists deletes the user approved). It writes environments before automations on create and update, updates in place with `name`, then re-lists and fails if anything still differs. Deploy writes only to Air and the rendered workflow files: it never creates, edits, labels or closes a tracker ticket. After a verified apply, close the open `Deploy needed` issue yourself. To rename an automation or environment, pass `--rename 'OLD NAME=NEW NAME'` (repeatable) to both the dry run and the apply: the item keeps its id instead of becoming a create plus a delete.
   Partial deploy: for a change limited to one node or environment, pass `--only <selector>` (repeatable) to both the dry run and the apply. A selector is `node:<node>` (every automation of that node), `env:<environment>` (that environment and its automations) or a name glob such as `'[Offline eval] notify *'`. Only matching items are written, deleted (still needs `--allow-delete`) and verified. Labels and tracker tags are created and updated, never removed. A selector that matches nothing exits 2. The dry run prints `partial: <selectors>` and `not deployed: N other changes`; run a full `--dry-run` afterwards to see what is still undeployed.
4. Create missing labels from the plan's `labels[]` (`gh label create <name>`); a missing label fails every handoff that adds it.
5. Run `factory --team <team-repo> deploy --dry-run` again: it must print `unchanged`.
6. Run each `[Slop factory] doctor*` automation once (air-automations MCP) and report its probe lines. A failed probe has opened or updated its `Doctor: <probe> fails` ticket.

For a YouTrack tracker with the comments plugin on, the dry run also lists the comment-wake workflow upload. Apply runs the supported
`@jetbrains/youtrack-scripting` CLI with the configured token environment variable. Attach the uploaded workflow to
the project once in Project settings → Workflows.

The one-time rename of legacy `[factory:<node>]` items is not automated: apply it with `update_automation` or `edit_environment` (recorded ID, planned name) before step 3.

## Project target (team-owned automations)

By default deploy creates personal automations. To deploy them to an Air project, set `targets.air.project_id` in the
team's `factory.yaml` to the project's UUID (a bad value fails `factory validate` with `bad-project-id`). Unset keeps the
personal behaviour. Every planned automation then carries `projectId`, and deploy reads and diffs only that project's
automations. The dry run prints `target: project <id>` or `target: personal`.

Two preconditions, both checked by a person before the first apply:

1. The Air organization is a team organization. A personal organization rejects `projectId` (AIRC-1855); apply stops with that reason.
2. The project already has its service account (AIRA-619); apply stops with that reason when it has none.

Deploy reads the project's environments through the air-backend REST API (`/env-configs?orgProjectId=<id>`), because
the air-cloud MCP sees personal environments only and an automation linked to a personal environment fails every run
with 404 `ENVIRONMENT_CONFIGURATION_NOT_FOUND` (AIRA-1629). For each planned environment the project lacks, the dry run
prints `create project environment <name> (copy of personal <id>)` or `(new)`. `--apply` creates them before any
automation: a copy duplicates the personal environment of the same name and repository into the project (the Air UI's
Change project, mode DUPLICATE: domains, variables and startup scripts come along; the personal one and its automations
stay as they are); a new one is created with the plan's name and repository. A copy brings the personal environment's variables and secrets. air-cloud's
`edit_environment` refuses a project environment, so deploy does not write a declared variable that the project
environment lacks: it prints `set by a person: <env> secret <NAME> (project environment)`. After the apply, run the dry
run again and set each name it lists as `set by a person` in the project environment.

Moving existing personal automations to a project is partly manual: set `project_id` and apply. Right after the apply,
pause every personal automation with the plan's prefix (air-automations `update_automation`, `paused: true`), or both
copies wake on each event. Delete them once the project copies have run (`--allow-delete` does not remove them:
deploy no longer lists personal ones). Open a follow-up ticket to automate it.

## Local target

`factory --team <team-repo> deploy --target local` checks the harness CLI, the `gh` login and the repository checkouts and names what is missing; `--apply` writes `.factory/local.json` for `factory run --local` (ADR-0017, `docs/targets.md`).

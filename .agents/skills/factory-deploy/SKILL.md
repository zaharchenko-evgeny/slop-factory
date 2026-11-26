---
name: factory-deploy
description: "Deploy, start and stop factory surfaces (Air Cloud and local/docker/docker-sandbox): render plans with deploy.py, sync Air automations and environments over REST, probe and register local surfaces and executors, and enable/disable or start/stop surfaces. Use when the user asks to deploy, sync, update, start or stop a factory."
---

# Factory deploy (Air and local surfaces)

Read first: the team repository's `factory.yaml`,
its `workflow.yaml`, and `docs/targets.md`. Set `FACTORY_TEAM` to the team
repository checkout, or pass it as `--team <team-repo>` to each `factory`
command. Run from the engine checkout; for local validation in this repository,
use `factory/tests/fixtures/v2` as the team directory. Whoever runs this
applies the plan and changes no code.

## Rules

- Touch only automations and environments whose name starts with the plan's top-level `prefix`. Never update or delete any other one.
- Always run a dry run first and show it. Apply only after the user approves the dry run (`gates.confirm_external_writes`).
- Secrets are names only. Never print or set a secret value; tell the user which names the environment still needs.

## Steps

0. Precondition: `factory/gate.py` must exist on the default branch (the thin prompt runs it). If not, stop and report; do not apply.
1. Check nodes: `factory --team <team-repo> validate` (or set `FACTORY_TEAM` and run `uv run --quiet factory/nodes.py validate`).
2. Sign in once: deploy calls the Air REST APIs (`air-automations-backend`, `air-backend` `env-configs`) with
   tokens from `jcp-mcp-bridge`, with the client and config directory of your Air login (`pi-lot`,
   `~/.config/jcp-mcp-bridge-pilot`). Check it with
   `JCP_MCP_API_URL=https://api.jetbrains.cloud JCP_MCP_CLIENT_ID=pi-lot JCP_MCP_REDIRECT_PATH=/space/auth JCP_MCP_CONFIG_DIR=~/.config/jcp-mcp-bridge-pilot jcp-mcp-bridge token --env prod --audience air-automations-backend >/dev/null && echo ok`;
   when it fails, sign in to Air through the bridge again. An unattended run (CI, a service account) sets
   `FACTORY_AIR_AUTOMATIONS_TOKEN` and `FACTORY_AIR_BACKEND_TOKEN` instead. No MCP server is needed; with both
   `FACTORY_MCP_AIR_AUTOMATIONS` and `FACTORY_MCP_AIR_CLOUD` set, deploy uses those MCP servers as before. A
   YouTrack tracker also needs its token (`token_env`, default `YOUTRACK_TOKEN`) in the shell: take it from the
   YouTrack server's `Authorization` header in `~/.pi/agent/mcp-adapter.json`. Never print a token.

   Dry run: `factory --team <team-repo> deploy --dry-run`. It renders the plan, lists automations and environments through the Air REST APIs (every page), reads details for factory-prefixed records, diffs by name and prints `create|update|delete <kind> <name>` with the changed fields. It writes nothing. Show the plan and review it.
3. Apply after the user approves the dry run: `factory --team <team-repo> deploy --apply` (add `--allow-delete` only when the dry run lists deletes the user approved). It writes environments before automations on create and update, updates in place with `name`, then re-lists and fails if anything still differs. Deploy writes only to Air and the rendered workflow files: it never creates, edits, labels or closes a tracker ticket. After a verified apply, close the open `Deploy needed` issue yourself. To rename an automation or environment, pass `--rename 'OLD NAME=NEW NAME'` (repeatable) to both the dry run and the apply: the item keeps its id instead of becoming a create plus a delete.
   Partial deploy: for a change limited to one node or environment, pass `--only <selector>` (repeatable) to both the dry run and the apply. A selector is `node:<node>` (every automation of that node), `env:<environment>` (that environment and its automations) or a name glob such as `'[Offline eval] notify *'`. Only matching items are written, deleted (still needs `--allow-delete`) and verified. Labels and tracker tags are created and updated, never removed. A selector that matches nothing exits 2. The dry run prints `partial: <selectors>` and `not deployed: N other changes`; run a full `--dry-run` afterwards to see what is still undeployed.
4. Create missing labels from the plan's `labels[]` (`gh label create <name>`); a missing label fails every handoff that adds it.
5. Run `factory --team <team-repo> deploy --dry-run` again: it must print `unchanged`.
6. Run each `<prefix>doctor*` automation once (Air UI, Run now) and report its probe lines. A failed probe has opened or updated its `Doctor: <probe> fails` ticket.

For a YouTrack tracker with the comments plugin on, the dry run also lists the comment-wake workflow upload. Apply runs the supported
`@jetbrains/youtrack-scripting` CLI with the configured token environment variable. Attach the uploaded workflow to
the project once in Project settings → Workflows.

The one-time rename of legacy `[factory:<node>]` items is not automated: pass `--rename 'OLD NAME=NEW NAME'` for each one in steps 2 and 3.

## Project target (team-owned automations)

By default deploy creates personal automations. To deploy them to an Air project, set `targets.air.project_id` in the
team's `factory.yaml` to the project's UUID (a bad value fails `factory validate` with `bad-project-id`). Unset keeps the
personal behaviour. Every planned automation then carries `projectId`, and deploy reads and diffs only that project's
automations. The dry run prints `target: project <id>` or `target: personal`.

Two preconditions, both checked by a person before the first apply:

1. The Air organization is a team organization. A personal organization rejects `projectId` (AIRC-1855); apply stops with that reason.
2. The project already has its service account (AIRA-619); apply stops with that reason when it has none.

Deploy reads the project's environments through the air-backend REST API (`/env-configs?orgProjectId=<id>`), because
a personal environment is not the project's and an automation linked to a personal environment fails every run
with 404 `ENVIRONMENT_CONFIGURATION_NOT_FOUND` (AIRA-1629). For each planned environment the project lacks, the dry run
prints `create project environment <name> (copy of personal <id>)` or `(new)`. `--apply` creates them before any
automation: a copy duplicates the personal environment of the same name and repository into the project (the Air UI's
Change project, mode DUPLICATE: domains, variables and startup scripts come along; the personal one and its automations
stay as they are); a new one is created with the plan's name and repository. A copy brings the personal environment's variables and secrets. air-cloud's
`edit_environment` refuses a project environment, so deploy does not write a declared variable that the project
environment lacks: it prints `set by a person: <env> secret <NAME> (project environment)`. After the apply, run the dry
run again and set each name it lists as `set by a person` in the project environment.

Moving existing personal automations to a project is partly manual: set `project_id` and apply. Right after the apply,
pause every personal automation with the plan's prefix (`PATCH /automations/<id>` with `{"paused": true}`), or both
copies wake on each event. Delete them once the project copies have run (`--allow-delete` does not remove them:
deploy no longer lists personal ones). Open a follow-up ticket to automate it.

## Local surfaces and executors (ADR-0017, ADR-0029)

A team can declare named surfaces under `surfaces:` and isolated executors (`host`, `docker`, `docker-sandbox`) under `executors:` in `factory.yaml` (with optional git-ignored machine overrides in `.factory/local.yaml`, which never override Air). See `docs/targets.md` and `skills/factory-setup/references/executors.md`.

1. **Validate:** `factory --team <team-repo> validate` checks surface names, executor configs, `max_parallel` (requires an isolated executor when `> 1`), and node overlap (`node-on-two-surfaces`).
2. **Prepare secrets & prerequisites:**
   - `host` executor: uses already-installed CLIs (`codex`, `junie`, `pi`, `claude`) and existing `gh` login with no extra secret exports.
   - `docker` / `docker-sandbox` (`sbx`): export a scoped GitHub token (issues & PRs write, contents read — never `gh auth token`) under the host variable mapped in `executors.<name>.secrets.GITHUB_TOKEN` (e.g. `FACTORY_SANDBOX_GH_TOKEN`). Export harness API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `JUNIE_API_KEY`) or set `auth_files: auto` to copy already-authenticated host harness logins into the sandbox.
   - For `docker-sandbox`, run `sbx login` and `sbx policy init balanced` once per machine.
3. **Probe & register:** `factory --team <team-repo> deploy --surface <name> --apply` (or `--target local --apply` for the default `local` instance) probes each executor, checks mapped tokens, verifies every harness CLI (`<cli> --version`) in one shared sandbox per isolated executor, checks `gh` auth and repository checkouts, and writes `.factory/local[-<name>].json`.

## Start and stop surfaces

Use `factory start` and `factory stop` to turn surfaces on or off without deleting anything (`--surface a,b` selects specific instances; omitting it uses `surface:` in `factory.yaml`):

- **Air surface:**
  - `factory --team <team-repo> stop --surface air [--only SELECTOR]... [--dry-run]` sets `paused: true` on deployed factory-prefixed automations.
  - `factory --team <team-repo> start --surface air [--only SELECTOR]... [--dry-run]` sets `paused: false` on deployed factory-prefixed automations.
- **Local surface:**
  - `factory --team <team-repo> start --surface <name> [--foreground] [--once] [--dry-run]` starts the polling loop in the background (PID in `.factory/local/<name>.pid`, logs in `.factory/local/<name>.log`; `--foreground` runs attached).
  - `factory --team <team-repo> stop --surface <name> [--force] [--timeout N] [--dry-run]` sends `SIGTERM` to drain in-flight runs cleanly and exit; `--force` aborts and immediately tears down active sandboxes.
  - Run `factory --team <team-repo> run --local --surface <name> --dry-run --once` before the first start to preview queued events.

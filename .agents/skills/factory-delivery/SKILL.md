---
name: factory-delivery
description: Run one factory node by hand on a GitHub ticket or PR — the gate check, the role's work, the handoff, the gate apply. Use when the user asks to run a factory node, or to push a ticket one step through the factory without waiting for its Air automation.
---

# Factory delivery (one node by hand)

On Air each node is a `[factory:<node>]` automation (`engine/nodes.yaml`,
[ADR-0014](../../../docs/adr/0014-node-model.md)). This skill does the same
steps yourself, for one node and one ticket. The gate alone changes labels,
posts reviews and comments, and moves the stage; you never do.

Read first: the team's `factory.project.yaml` or `factory.descriptor.yaml`, its
workflow, and `AGENTS.md`. Set `FACTORY_TEAM` to the team repository checkout,
or pass its directory with `factory --team <team-repo>`. For local validation
in this engine checkout, use `factory/tests/fixtures/team` as the team directory.

## Run

From the checkout (the code under review or fix), with `N` a GitHub ticket
or PR and `<node>` the node to run (a `factory:*` command label starts it on
Air). Use trusted engine code for the gate because `apply` runs with your
GitHub credentials; role files and skills always come from `FACTORY_TEAM`.

1. Fetch the trusted copy: `git fetch origin main`, then
   `git worktree add --detach <tmp>/factory-main origin/main`. `<tmp>` is a
   fresh temp directory (`mktemp -d`). Use `<tmp>/factory-main` for `gate.py`
   and your checkout only for the code. Point `FACTORY_TEAM` at the team
   repository checkout (or pass `--team <team-repo>` to `factory` commands)
   so the gate loads the team's config, roles and skills.
2. Save the trigger as JSON in a file, for example `<tmp>/event.json`: the
   GitHub event payload that would start the node (`gh api repos/<repo>/issues/<N>`
   wrapped as `{"eventType": "issues.labeled", "label": "<label>", "data": {"issue": ...}}`,
   or the PR payload under `pull_request`).
3. `uv run <tmp>/factory-main/factory/gate.py check --node <node> --event <file> [--tier <tier>]`.
   Exit 10 means the gate refused: report the reason and stop. Otherwise it
   prints `role_file`, `skills`, `branch` and `handoff_path`.
4. Read `role_file` from the team checkout and each listed skill from
   `$FACTORY_TEAM/skills/<name>/SKILL.md`. Do the role's work on `branch` in
   your project checkout (create it from the default branch when it does not
   exist). Commit; do not push. The engine's templates are not a runtime fallback.
5. Write the role's handoff to `handoff_path`.
6. `uv run <tmp>/factory-main/factory/gate.py apply --node <node> [--tier <tier>] --event <file> --handoff <handoff_path>`.

Scripted nodes (join, finish) are just `uv run <tmp>/factory-main/factory/gate.py sweep`.

If the gate reports `blocked` or `failed`, do not continue by hand: report the
reason. Run artefacts stay in `.factory/` (git-ignored).

## External writes

While `gates.confirm_external_writes` is `true`, the kinds listed in
`gates.confirm_external_writes_scope` (today: none) need the user's approval
before the run. All other writes are auto-approved; name them in the report.
Never merge: that is the human's gate.

## Report

Tell the user: the node, the gate's decision and the final stage, the handoff
status, checks with results, and the writes made. Say what was not verified.

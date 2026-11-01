---
name: factory-delivery
description: Run one factory node by hand on a GitHub ticket or PR — the gate check, the role's work, the handoff, the gate apply. Use when the user asks to run a factory node, or to push a ticket one step through the factory without waiting for its Air automation.
---

# Factory delivery (one node by hand)

On Air each node is a `[factory:<node>]` automation (`workflows/nodes.yaml`,
[ADR-0014](../../../docs/adr/0014-node-model.md)). This skill does the same
steps yourself, for one node and one ticket. The gate alone changes labels,
posts reviews and comments, and moves the stage; you never do.

Read first: `factory.yaml`, `workflows/delivery.md`, `AGENTS.md`.

## Run

From the checkout (the code under review or fix), with `N` a GitHub ticket
or PR and `<node>` the node to run (a `factory:*` command label starts it on
Air). The gate, the role file and the skills come from a fetched default
branch, never from the ticket or PR branch: a PR can change `gate.py` or its
imports, and `apply` runs with your GitHub credentials.

1. Fetch the trusted copy: `git fetch origin main`, then
   `git worktree add --detach <tmp>/factory-main origin/main`. `<tmp>` is a
   fresh temp directory (`mktemp -d`). Use `<tmp>/factory-main` for `gate.py`,
   the role file and the skills; use your checkout only for the code.
2. Save the trigger as JSON in a file, for example `<tmp>/event.json`: the
   GitHub event payload that would start the node (`gh api repos/<repo>/issues/<N>`
   wrapped as `{"eventType": "issues.labeled", "label": "<label>", "data": {"issue": ...}}`,
   or the PR payload under `pull_request`).
3. `uv run <tmp>/factory-main/factory/gate.py check --node <node> --event <file> [--tier <tier>]`.
   Exit 10 means the gate refused: report the reason and stop. Otherwise it
   prints `role_file`, `skills`, `branch` and `handoff_path`.
4. Read `role_file` and every listed skill from `<tmp>/factory-main`, and do the
   role's work on `branch` in your checkout (create it from the default branch
   when it does not exist). Commit; do not push.
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

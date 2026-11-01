# Workflow: delivery

The per-ticket loop (design §5, §6). The definition is the node list in
`workflows/nodes.yaml` ([ADR-0014](../docs/adr/0014-node-model.md)): each node
is a `[factory:<node>]` Air automation, started by a label or a PR event, that
runs `uv run factory/gate.py check`, does its role's work, writes its handoff
and runs `gate.py apply`. The gate moves the stage; the stage labels and the
tracker operations are in `docs/agents/issue-tracker.md`. Names of
repositories, stages, models and checks come from `factory.descriptor.yaml`.

By hand, use the `factory-delivery` skill: it runs one node the same way.

## Input

A tracker ticket in stage `ready`.

## Handoffs

Each agent step ends with the structured block its role file defines. Steps
pass these blocks, never transcripts. The reviewer always starts fresh.

## External writes

Every push, label change, PR and comment carries the `[factory]` marker and a
`Factory-Run:` trailer. While `gates.confirm_external_writes` is `true`, each
write kind in `gates.confirm_external_writes_scope` is shown and waits for the
user's approval; a declined write stops the run with stage unchanged. Other
writes, including the push of the ticket's own branch, are auto-approved.
Merging stays with the human (`gates.pr_approval`).

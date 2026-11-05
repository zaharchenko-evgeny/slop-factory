# State chart

Every v2 factory team gets a state chart of its delivery workflow in `workflows/delivery.md`, generated from the
team's files and checked. It is the picture a person reads to decide whether the workflow is right.

## Input and output

- Input: `factory.project.yaml` (`states` with `kind` and `store`) and the workflow it names (`workflows/delivery.yaml`).
- Output: the block between `<!-- state-chart:begin -->` and `<!-- state-chart:end -->` in `workflows/delivery.md`.
  The block holds a Mermaid `stateDiagram-v2` and a node table (node, wakes on, role, environment).
- The template's `workflows/delivery.md` already has the markers. Prose around the block is written by hand.

## Commands

```sh
uv run skills/factory-setup/scripts/state_chart.py write TEAM_DIR    # regenerate the block
uv run skills/factory-setup/scripts/state_chart.py verify TEAM_DIR   # check it; exit 1 on any error
uv run skills/factory-setup/scripts/state_chart.py print TEAM_DIR    # print the generated block
```

## How arrows are drawn

- Each state-changing outcome (one with a `state` effect) of an enabled node is one arrow per source state.
  The sources are the outcome's `from`, else the node's `when.state`, else `any` (a pseudo-state with arrows
  from every state).
- Label: `node · outcome`, then ` / +cmd` for each command the outcome adds and ` / -cmd` for each it removes.
- A node or outcome with `enabled: false` draws no arrow. A state that only disabled outcomes lead to is drawn
  as off (grey, dashed) and is not checked.
- Waiting states (`kind: waiting`) are drawn in yellow, final states (`kind: final`) in green. A final state
  with no arrow out gets an arrow to the end marker.
- A state with a `pr` store is a fact of the pull request. A node wakes on it, so it can start a path. It is
  never a dead end.

## What verify checks

| Error | Meaning | What to change |
|---|---|---|
| `chart-missing` | The doc has no markers | Add the markers, then run `write` |
| `chart-stale` | The block differs from what the files generate | Run `write`, then review the diff |
| `unknown-state` | An outcome names a state the project does not declare | Declare the state or fix the name |
| `no-start-state` | Every state has an incoming arrow | Some state must be entered from outside (a person, a PR fact) |
| `unreachable-state` | No path from a start state reaches it | Add the arrow, or delete the state |
| `dead-end-state` | An active state has no arrow out, so a ticket there never moves | Add the outcome that moves it, or make it `waiting` if a person acts |

A state with no incoming arrow is a start state, because a person or an event creates it (`draft` in the
template). Verify therefore reports it only when it has no way out.

Verify does not model-check timing or fairness, and it does not know which node wins when two outcomes
match the same state. Those need the formal check below.

## Show the chart to the person who decides

Give the Mermaid block (or the path to `workflows/delivery.md`) with the verify result. Say which states are
off, which states are waiting for a person, and which PR facts start paths. Name any error that is still open.

## Formal check (optional)

For a model-checked chart, use the `statechart` skill (TLA+ and TLC, drawn as Mermaid). Write the spec from
the same states and arrows that `state_chart.py print` shows, so the two charts agree. That route needs Java
and TLC, which the factory setup does not install; this skill's `verify` does not depend on it. The skill does not
generate the TLA+ spec yet.

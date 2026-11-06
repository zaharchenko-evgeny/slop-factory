# ADR-0020: Agents are named role and model bundles that nodes declare

Status: accepted, requested by #589 (slice 1 of 4: the model and the mention
allowlist). Builds on ADR-0014 (nodes) and ADR-0016 (project descriptor and
workflow).

Context: the factory's agents should talk to each other in ticket and PR
threads (`@factory:<persona> <text>`) so that a person can follow the dialog.
A node has a role and a tier, and the team's `roles:` maps the tier to a
model, but nothing names the agent behind a node or says which nodes are the
same agent, and nothing says who may talk to whom.

Terms:

- A **role** is what to do: `roles/<role>/role.md`, its skills and tools.
- A **node** is when and with which outcomes: one entry of the workflow.
- An **agent** is who does it: a top-level `agents:` entry in
  `factory.project.yaml` with a `persona`, a `role` and the role's per-role
  fields (`tier`, `tier_high_risk`, `fallback`, `modes`, `tools`), plus an
  optional `effort`. A node names it with `agent: <id>`; several nodes may
  name the same agent.

Decision:

1. `persona` is a lowercase slug (`^[a-z][a-z0-9-]*$`), unique across agents.
   It is the handle in `@factory:<persona>`.
2. A node without `agent:` gets its role's implicit agent: the role's `roles:`
   entry, with the role name as id and persona. The agent's fields replace
   the role's when deploy picks a model; a node's own `tier` still wins.
3. Validation (`factory validate`, and deploy) rejects an unknown agent id, an
   agent whose `role` differs from the node's, `agent:` on a `script` or
   `human` node, a bad or duplicate persona and a review focus the agent has
   no mode for.
4. `effort` goes to a target that takes a reasoning setting. Air automations
   take none today, so deploy warns and ignores it.
5. The mention allowlist is generated at deploy from the workflow:
   - Nodes A and B are neighbours when an outcome effect of one sets a stage
     in the other's `when.stage` or applies a label in its triggers. A
     disabled node or outcome counts: the graph is the declared workflow.
   - Agent X may mention agent Y when a node of X neighbours a node of Y and
     both act on the same subject: `pr` for a PR-triggered node, else
     `ticket`.
   - Routine and script nodes are never mentionable; `mention: false` takes a
     node out of the graph.
6. When the project declares `agents:`, each non-script node spec carries
   `agent`, `persona`, `mentions` (sorted personas) and `mentions_on`
   (`{persona: [ticket|pr]}`), and `factory begin` prints them. Without
   `agents:` the plan is byte-identical to before.

Consequences:

- The engine's own graph (`engine/nodes.yaml`) sets no `agent:`, so the
  engine keeps implicit agents only.
- An agent duplicates the tiers of its role's `roles:` entry; `roles:` stays,
  because roles without a declared agent and validation still read it.
- Later slices add the mention parser and reply mode, the agent `ask`
  outcome, and resuming the agent's session in reply mode.

Rejected: naming agents per node (one persona per automation). The same
developer implements, fixes and updates; a person should talk to one `dev`.

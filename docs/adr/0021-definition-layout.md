# ADR-0021: The team definition is factory.yaml, workflow.yaml and roles/

Status: accepted, requested by #667 (slice 1 of 6, #668: the layout and its
loader). Replaces the team side of ADR-0016 (project descriptor and workflow)
and ADR-0020 (agents) once slice 6 moves slop-eval-factory; the engine keeps
reading the old files until a later ticket removes them.

Context: a team defines its factory in `factory.project.yaml`, a workflow file
and `roles/<role>/` folders with `role.md`, `skills.txt`, `setup.sh` and
`handoff.schema.json`. A model tier is set in up to four places, review modes
are copied between `roles:` and `agents:`, three environments differ by one
domain, states and commands are listed as names and again as labels, and
`gates`, `knowledge`, `limits` and `notify` are top-level blocks that each
belong to one node. Air runs one fixed model per automation, so tiers and
focuses are hidden expansions of one node into several automations.

Decision:

1. **`factory.yaml` lists what the factory has**: `name`, `engine` (the pinned
   engine package), `tracker` (with `label_prefix`, the stage label prefix), `models` (aliases, the only place a model ID
   appears), `defaults` (`model`, `env`, `mcp`, `skills`, `settings`), `skills`
   (local `path`, or `source` + `path` + `sha256`), `roles` (`description`,
   `skills`, `mcp`), `agents` (a role + a model + `access` + `settings`; the
   `gate` script and `human` have no role), `environments` and `webhooks`.
2. **`workflow.yaml` says how it works**: `stages` (`final`,
   `waiting`), `settings` (inherited by every node: limits, the default
   `environment`) and `nodes`.
3. **`roles/<role>/`** holds `role.md` (instructions, Jinja), an optional
   `setup.sh` that installs the role's tools, and any file the role needs. No
   YAML lives there.
4. **A node** is an agent + an environment + `triggers` + `when` + `outcomes`,
   with optional `model`, `settings` and `enabled: false`; it has no prompt of
   its own: its role's `role.md` says what to do, from the node's settings
   (`mode`, `focus`). Every node
   is an agent node (#674-#678): the agent reports one outcome and the gate
   applies its static effects (`stage`, `label`, `unlabel`, `comment`,
   `review`, `merge`, `update-branch`, `create-issues`). States are stage
   labels, other labels and PR states; a label `<group>:<value>` (`risk:low`,
   `complexity:small`) holds one value per group. Routing is the labels
   outcomes add; variants are separate nodes; there is no matrix, router,
   tier or focus. Deterministic rules live in three places only: `when`, an
   outcome's `if` (facts: `stage`, `label`, `pr`, `ci`, `review`, `blockers`,
   `children`; when it fails the gate applies `else`, or refuses), and the
   effects' own checks (a merge needs an approved head with green CI). A step
   with no judgement runs a cheap agent with `run: decide`: `factory decide`
   applies the node's first outcome whose guards hold. Several triggers are OR.
5. *Superseded in part by ADR-0024.* **An environment** lists its repositories; exactly one is `main` (branches,
   PRs and the node's output apply there). A writable repository that is not
   `main` needs `token_env`, declared in `env`. An optional `setup` script
   installs the environment's tools.
6. **Merge order** of settings, env and mcp: `factory.yaml` defaults →
   environment → role → agent → `workflow.yaml` settings → node. A narrower
   layer overrides a value; `null` clears it.
7. **Templating happens once, at setup**, from the definition at the deployed
   SHA, with `StrictUndefined`. The context is `settings`, `node`, `agent`,
   `role`, `environment` and `factory`. Runtime data (the ticket, the PR) is
   never templated, and secret values never reach a template.
8. **No handoff schema files**: a node's result schema is generated from its
   outcomes and the fields their effects read. **No `checks`**: a project's
   checks are its own CI, which the factory reads as a state.
9. `factory validate` checks the layout (`factory/definition.py`) when a team
   root has `factory.yaml` and `workflow.yaml`.

Consequences: a team edits two YAML files and Markdown. Deploy can create one
Air automation per node with no hidden expansion. The gate needs new guard
kinds (`risk`, `complexity`) and the `ci: failed` trigger (#669, #670). The
setup skill's template and slop-eval-factory move to the layout (#672, #673).

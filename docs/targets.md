# Targets: how the factory runs on Air Cloud

The factory is the node list in `engine/nodes.yaml`
([ADR-0014](adr/0014-node-model.md)). Each node is deployed as a `[factory:<node>]`
Air automation by the `factory-deploy` skill. A node starts on a label or a pull
request event, a schedule or an Air webhook; its agent runs `gate.py check`, does its role's
work, writes the handoff and runs `gate.py apply`. The gate alone changes labels,
posts reviews and comments, and moves the stage. Scripted nodes (join, finish)
run only `gate.py sweep`. Automations carry the `[factory]` name prefix, so only
factory-owned automations are planned, updated or pruned.

Status: the Air target is implemented; the spike results are on #4. To run one node
by hand, see the `factory-delivery` skill.

## Runtime on Air

| Concern | Air |
| --- | --- |
| Harness | `claude-code` or `codex` with the models Air provides (`targets.air.models` in `factory.descriptor.yaml`) |
| Secrets | Project environment secrets, names only: `GITHUB_TOKEN`, decisions-kit auth |
| Tools | Installed by the settings repository's `.air/cloud/startup.sh`: `uv`, `gh`, `conftest`, `decide` |
| Overlap | Duplicate trigger firings are expected; the gate refuses a stale or duplicate event (exit 10) |
| Logs | Run page in Air plus the run files in the run's workspace |

## Repositories of an automation (ADR-0017)

| Automation | Main repository | Additional repositories |
| --- | --- | --- |
| Project nodes (intake, review, implement, fix, join, merge, finish, …) | the `role: primary` repository | every other descriptor repository at its `default_branch`, then the factory repository at `main` (read-only) |
| Retro, distill, knowledge review, ops nodes (notify, reconcile, doctor-ops) | the factory repository | every project repository in the descriptor, at its `default_branch` |

The prompt's "Project checkout" section names each checkout under `/workspaces/<name>`.
`deploy.py` writes `additionalRepositories` through the Air REST API (`Backend.set_additional`), because
the automations MCP does not expose it.

### Delivering to the team repository (ADR-0019)

To change the team repository through the factory, list it in `repositories` with `role: project`; any other role
is a `factory validate` error. It is then one project checkout like any other: write nodes change it on branch
`ez/<issue>-<slug>`, and it gets no read-only entry in `additionalRepositories`. The factory still reads its roles
and skills from the `factory-team` directory in `$HOME`, a `git worktree` of the checkout's `origin/<default branch>` that the
team's startup script creates (the template's `.air/cloud/startup.sh` shows how), and never from the checkout.
With `FACTORY_TEAM` unset, the engine uses the `factory-team` directory in `$HOME` when it exists. Otherwise nothing changes.

Open (#459): whether Air pushes a branch or opens a PR from an additional repository (`create-pr`,
`push-to-source` act on the main repository). Until a probe answers it, a ticket that changes a second
project repository needs its developer to push that branch itself, or it is split into one ticket per
repository.

## Label colors

`factory deploy` creates and recolors factory labels by role, with the defaults of `labels.colors` in
`engine/defaults.yaml`. The gate uses the same colors for a label it creates at a merge. A role, not a name, picks the
color, so a renamed or added label keeps a sensible one.

| Role | Color | Labels |
| --- | --- | --- |
| Stage (by stage name; a stage with no color uses its stage kind) | draft `cfd3d7`, triage `d4c5f9`, ready `c2e0c6`, in-dev `1d76db`, in-review `5319e7`, changes-requested `e99695`, approved `0e8a16`, split `bfdadc`, needs-answers and investigating `fbca04`, blocked `b60205`, done and not-actionable `ededed` | `tracker.stages` |
| Stage kind (fallback) | waiting `fbca04`, active `1d76db`, final `ededed` | `tracker.stage_kinds` |
| Command | `c5def5` | every other label of the deploy plan, `review_trigger`, `hold_label` |
| CI fact | `2da44e` green, `d73a4a` red | `tracker.pr_fact_labels` |
| Risk | `e6f4ea` low, `f9d0c4` high | `tracker.risk_labels` |
| Group | `complexity:*` `fbca04`, needs-human `d93f0b`, map `000000` | |
| Other | random 6-digit hex, chosen at create | any label with no mapped color |

A team overrides one label in `factory.yaml` with `labels: {overrides: {"factory:blocked": "000000"}}`. Deploy never
recolors an existing label that has no mapped color, so a random color stays stable.

## MCP connectors

An Air automation takes `mcpConnectors`, the MCP servers its agent gets (for example `youtrack`, `slack`).
`targets.air.mcp_connectors` lists the connector names the Air org has. A role (`roles.<role>.mcp`), a reviewer
mode (`roles.<role>.modes.<focus>.mcp`) or a workflow node (`mcp`) names the ones its agents get; a node's list
replaces its mode's, and a mode's replaces its role's. No list means no connectors.

- `factory validate` reports `unknown-mcp-connector: <where>: <name>` for a name missing from
  `targets.air.mcp_connectors`.
- `deploy.py` writes each automation's `mcpConnectors` and adds a "Your MCP servers" section to its prompt. The
  diff compares the field, so a connector added by hand in Air shows as a removal in `--dry-run`.
- `factory deploy --target local` prints a warning for each role whose connectors are not provided locally.
- A change of the create call needs a refreshed Air contract recording: the operator runs
  `factory contracts refresh`.

## MCP servers in the ADR-0021 layout

One `mcp` map, keyed by the server name the agent sees, in `defaults`, an environment, a role, an agent or a
node of `workflow.yaml`. The layers merge by key in that order; an entry replaces the inherited entry of the same
key as a whole, and `null` removes it.

```yaml
roles:
  developer:
    mcp:
      youtrack: { connector: internal-youtrack }       # Air connector -> the automation's mcpConnectors
      idea: { command: npx, args: ["-y", "@jetbrains/mcp-proxy"], env: { IDE_PORT: "${IDE_PORT}" } }   # custom -> .mcp.json
      docs: { url: "https://mcp.example/sse", headers: { Authorization: "Bearer ${DOCS_TOKEN}" } }
      glean: null
```

- Air connector names: `confluence`, `datadog`, `figma`, `gitlab`, `google_calendar`, `grafana`, `jira`, `linear`,
  `sentry`, `slack`, `teamcity`, `internal-teamcity`, `youtrack`, `internal-youtrack`, `internal-glean`.
  `targets.air.mcp_connectors` in `factory.yaml` replaces this default list.
- Custom entries (`command`/`args`/`env` or `url`/`headers`) have the `.mcp.json` `mcpServers` shape. They never go
  to `mcpConnectors`.
- Variant A: `factory setup` (run by `.air/cloud/startup.sh`, before the agent) writes the custom entries to
  `.mcp.json` in the checkout. Nothing is committed. `${VAR}` references stay unexpanded; Air supplies the values from
  the environment's secrets and variables. Setup runs per environment: it writes the entries of the node in
  `FACTORY_NODE` when set, else the union of the nodes of that environment, and fails when two nodes define one
  server name differently. Deploy does not set `FACTORY_NODE` yet, so nodes of one environment share the file.
- `factory validate` reports `unknown-mcp-connector`, `mcp-undeclared-env` (a `${VAR}` that no `env` declares at the
  same or an outer layer), `mcp-literal-secret` (a secret-like `env` or `headers` value that is not a `${VAR}`
  reference), `mcp-mixed-entry` (`connector` with `command`/`url`) and `bad-mcp-entry`.
- The prompt's "Your MCP servers" section lists both kinds by key.

## Local target (ADR-0017)

The same nodes run on a laptop with no Air account. `targets.local` in `factory.project.yaml` takes
`harness` (`claude-code`, `codex`, `junie` or `pi`), `models` (tier to model), `poll_seconds` and `workspace`
(default: the directory that holds the factory repository checkout). A role or reviewer mode may set its own
`harness` (`roles.<role>.harness`, `roles.<role>.modes.<focus>.harness`). Harnesses and `targets.local.models`
apply to this target only: on Air the platform runs every agent with `targets.air.models` (ADR-0018).

- `factory --team <factory repo> deploy --target local` checks the harness CLI on `PATH`, a `gh` login, the
  factory repository checkout, and a checkout of every descriptor repository at `<workspace>/<name>` whose
  `origin` is that repository. It names each missing item and exits 1. `--apply` writes `.factory/local.json`
  (harness, login, checkouts, the automations the loop serves).
- `factory run --local [--once] [--dry-run]` does Air's part and nothing else. It polls GitHub for the events
  the node triggers name: a label added to an issue or PR, a PR merged or pushed, an issue closed. For each new
  event it makes a worktree per repository in `.factory/worktrees/<n>/<repo>` and runs `factory begin` there; on
  exit 10 nothing runs. It then starts the harness with the node's Air prompt, so the agent runs `factory begin`
  and `factory end` and the gate alone moves the stage. For a write node it pushes the `ez/` branch, and for
  `create-pr` it runs `gh pr create`. Script nodes run `factory sweep --event`.
- Seen events are kept in `.factory/local/seen.json`. The first run takes earlier merges, closes and pushes as
  history and acts only on the labels that are set now. `--dry-run` lists the events without running or
  recording them.
- A run that fails before the harness starts (checkout, `factory begin`, a script's sweep) is tried again on
  the next polls, three runs in all. A run whose harness started is not repeated, as on Air; a failed push or
  `gh pr create` is printed in the run's result. A GitHub read that fails skips the poll (`--once` exits 1).
- A node with a `schedule <name>` wake runs when its cron (5 fields, UTC; `*`, numbers, lists, ranges and `*/n`) is
  due in the minute the loop polls, once per `schedule:<name>:<YYYY-MM-DDTHH:MM>` key in the seen events; `--dry-run`
  lists the due schedules. Day and weekday must both match. Nodes with `runner` (GitHub Actions) are not served.
- A node whose only wakes are `webhook <name>` is not served: a webhook comes from an Air sender, not the poll.
- Before the harness starts, the worktree gets the role's skills (`roles/<role>/skills.txt`) and `factory-step`
  in the harness's project skill directory (the `skills` folder of `.claude` for claude-code, of `.agents` for codex, pi and
  junie), listed in the worktree's `.git/info/exclude`.
- Not polled: `pull_request_review.submitted` and `issues.comment`. The reconcile sweep catches what those would
  have woken.

## Webhooks

A workflow declares `webhooks: {<name>: {description}}` and a node wakes on `webhook <name>`. Deploy plans the trigger
`{"triggerType": "WEBHOOK"}` with the node's `output` in `triggerVcs`; the agent takes the ticket number from the
`issue` field of the trigger payload. After `--apply`, deploy prints each webhook automation's name and trigger id (and
its URL when Air returns one) so a person can register it with the sender. The operator refreshes the Air contract
recording for a `WEBHOOK` create with `factory contracts refresh`.

## Known Air limits that shape the design

- Trigger filters are exact key/value matches with no OR or NOT; YouTrack values are case-sensitive.
- `pull_request.opened` and `synchronize` do not expose PR labels to filters (AIRA-970); a run started by them must check the branch prefix `ez/` and exit at once otherwise.
- Scheduled runs use the creator's token; an expired login makes schedules silent.
- Two triggers that fire together start two runs; the tracker claim makes the second one exit.
- A run that fails before its session starts cannot be rerun from its payload (AIRA-1300).
- Automation chaining (`Automation finished`, AIRA-1476) is not available yet; the tracker stages chain the steps instead.

## Probed Air facts (#152)

Probed on 31 October with personal probe automations on `ez/152-probes` (model codex/gpt-6-luna). The probe automations are deleted. ADR-0014 does not exist yet, so the consequences are below; move them there once it merges (#166).

| # | Question | Answer | Run link |
| --- | --- | --- | --- |
| 1 | A label added by `jetbrains-air[bot]` fires `issues.labeled` and `pull_request.labeled` | Yes, both, within 2 s (labels on #164, #165, #167) | [12a95b1b](https://air.jetbrains.cloud/org/05cf1a7f-6ab5-713b-abd3-29d0c8a05e2d/automations/4fd6cfca-9ffd-4569-89b1-04f593cbc223?run=12a95b1b-5aec-4ffe-a26b-0ee487f0aa89), [bb1ac000](https://air.jetbrains.cloud/org/05cf1a7f-6ab5-713b-abd3-29d0c8a05e2d/automations/4fd6cfca-9ffd-4569-89b1-04f593cbc223?run=bb1ac000-a636-44b5-9b33-11ab1af51611), [aee758ee](https://air.jetbrains.cloud/org/05cf1a7f-6ab5-713b-abd3-29d0c8a05e2d/automations/766d62c3-0f85-4dc4-b464-79e44bb664e6?run=aee758ee-ecdc-45d1-85ee-d29d64e1f9cd) |
| 2 | A YouTrack tag added by a run fires `tagAdded` | Not probed in a run: no YouTrack project for this repository. `scripts/youtrack_probe.py LME-1701` (#615) reads an issue read-only; its result is pasted on #163 after the merge | none |
| 3 | `push-to-source` accepts a merge commit from `main` | Yes: merge commit pushed by push-to-source, run 23839ea7 (#152) | run 23839ea7 |
| 4 | `.air/cloud/startup.sh` runs and `FACTORY_ENV` reaches it | Yes: `startup ran 2026-10-31T09:44:22Z FACTORY_ENV=probe`; the script is read from the automation's branch | [12a95b1b](https://air.jetbrains.cloud/org/05cf1a7f-6ab5-713b-abd3-29d0c8a05e2d/automations/4fd6cfca-9ffd-4569-89b1-04f593cbc223?run=12a95b1b-5aec-4ffe-a26b-0ee487f0aa89) |
| 5 | `pull_request.opened` fires for a PR the platform opens after `create-pr` | Yes: PR #167 opened by `app/jetbrains-air`; filter `pr_author: jetbrains-air[bot]` works | [69343c6a](https://air.jetbrains.cloud/org/05cf1a7f-6ab5-713b-abd3-29d0c8a05e2d/automations/9a76d1d9-e6d2-4d1b-a856-bc9242caeaf9?run=69343c6a-cf87-441a-9b8a-27474dbd1a25) |
| 6 | Two runs of one automation run at the same time | Yes: 09:44:05–09:44:50 and 09:44:06–09:44:55 overlap | runs 12a95b1b and bb1ac000 (links above) |
| 7 | Re-adding a present label fires no second event | Yes: the second add on #164 (exit 0) produced no event and no run | events of #164 |

Other findings and their consequences for the design (#150, #159):

- Concurrent runs of one automation are real: the join and the merge train must be idempotent and keyed by head SHA. The pilot sweep merges nothing (dry-run). The `sweep` node is a script, not an agent: the Air automation runs `uv run factory/gate.py sweep` (model `cheap`, does no work). The script lists the issues itself, open or closed, in `approved`, `merged` or `split`, plus `in-review` for the join backstop, and moves each ticket at most one stage per run (`approved` with a merged PR → `merged`; `merged` → one retro comment, `done`, issue closed; `split` with all children closed → `done`). A run with no new facts writes nothing; `--dry-run` prints the plan. The `conflict` and `answered` outcomes are disabled for the pilot.
- In a run `gh` acts as `jetbrains-air[bot]` through `GH_TOKEN`; `gh api user` answers 403, so a role must not call it.
- The `create-pr` agent named its branch `ez/152-probe-p5` from AGENTS.md, so the gate must check branch naming, not assume it.
- `result.summary` was null for 2 of 5 finished runs: the node's handoff goes through `gate apply`, not the run summary.
- On Air, `gate.py check --event <file>` also takes the raw trigger payload (`issues`, `pull_request` or `pull_request_review`); a PR whose head is not `ez/<n>-*` exits 10. Its output adds `branch` (the PR head, or `ez/<n>-<slug>` from the issue title) and `handoff_path` (`.factory/handoff/<node>.json`). The role works on `branch`, writes the handoff to `handoff_path`, then runs `gate.py apply --node <n> --handoff <handoff_path>`, which derives the outcome and payload (`--outcome` and `--payload` still work). The platform pushes.
- No answer is "no", so no workaround is needed. Probe 2 is tracked in #163.

## Notifications

The engine sends no notifications of its own. A team's `notify` node wakes on the states that need the owner
(stage `blocked` or `needs-answers`, label `factory:needs-human`); its cheap agent reports `notified` with a short
`message`, which the gate posts as a comment on that ticket or PR, mentioning `owner` from `factory.yaml`, or
`skipped`. Air run-end `outboundWebhooks` cannot wake an automation: Air signs them with an HMAC secret and sends no
`Authorization: ApiKey` header (AIRA-627). `recover --stale` labels the ticket `factory:needs-human` and assigns the owner.

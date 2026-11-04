# Targets: how the factory runs on Air Cloud

The factory is the node list in `engine/nodes.yaml`
([ADR-0014](adr/0014-node-model.md)). Each node is deployed as a `[factory:<node>]`
Air automation by the `factory-deploy` skill. A node starts on a label or a pull
request event, never on a schedule; its agent runs `gate.py check`, does its role's
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
| Tools | Installed in the environment's setup script: `uv`, `gh`, `conftest`, `decide`; `.air/cloud/startup.sh` installs `conftest` and `decide` |
| Overlap | Duplicate trigger firings are expected; the gate refuses a stale or duplicate event (exit 10) |
| Logs | Run page in Air plus the run files in the run's workspace |

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
| 2 | A YouTrack tag added by a run fires `tagAdded` | Not probed: no YouTrack project for this repository; moved to #163 | none |
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

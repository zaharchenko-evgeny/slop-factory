# ADR-0018: A harness is a local-target setting; Air runs its agents itself

Status: accepted, decided by the operator on 5 November 2026 (#523). Refines
ADR-0017's local target.

Context: every role's `setup.sh` listed `pi` in `need_tools`, so each Air
startup reported the role's tools as missing, although Air runs the agent and
makes every model call itself. The local target picked one harness for every
role (`targets.local.harness`) and read the top-level `models` map, which Air
never uses.

Decision:

- On Air, the platform runs each role's agent with `targets.air.models`. No role
  needs a harness CLI, and role `setup.sh` files name none.
- A harness is a local-target setting: `roles.<role>.modes.<focus>.harness`,
  else `roles.<role>.harness`, else `targets.local.harness`, else
  `claude-code`. Supported: `claude-code`, `codex`, `junie`, `pi`. The local
  adapter (an agent runner plugin, `factory/factory_plugins/<plugin>/runner.py`
  since #1071) builds each one's headless command.
- The local model map is `targets.local.models`; the top-level `models` map is
  still read by the local target when `targets.local.models` is absent. Node
  validation checks tiers against `targets.air.models`.

Consequences: `factory deploy --target local` checks the CLI of every harness
the roles use. A team that runs only on Air sets no harness and no local
models.

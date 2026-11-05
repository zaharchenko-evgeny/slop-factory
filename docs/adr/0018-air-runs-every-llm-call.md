# ADR-0018: Air Cloud runs every LLM call

Status: accepted, decided by the operator on 5 November 2026 (#523). Supersedes
the local target of ADR-0017 (`targets.local`, `factory run --local`).

Context: ADR-0017 added a local target that polls GitHub and runs each role
through a harness CLI (`pi`, `claude` or `codex`) on the operator's machine,
with its own model map. It doubled the ways a role runs, made every role's
`setup.sh` require `pi`, and put model calls outside Air's accounts, limits and
run history.

Decision: every factory role runs as an Air Cloud automation, and Air makes
every model call. The engine has no local target and no harness adapter; model
tiers come only from `targets.air.models`. A person still runs one node by
hand with the `factory-delivery` skill in their own session; that is a person
working, not a factory target.

Consequences:

- `factory/local.py`, `factory run --local`, `factory deploy --target local`
  and `targets.local` are gone; a descriptor with `targets.local` fails
  validation. The top-level `models` map is no longer read.
- Role `setup.sh` files need no harness tool, and role skills install into
  `~/.agents/skills` and `~/.claude/skills` only.
- The approver's decisions-kit `decide` still calls a model outside Air; the
  approver has no node, and moving its soft rules is a separate ticket.

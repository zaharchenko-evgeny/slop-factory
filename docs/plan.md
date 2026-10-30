# Implementation plan

Follows `docs/design.md` §15. Each step is one GitHub issue in this repository.
Roles are enabled in phases; each phase runs for about a week before the next.
The Air path follows the state chart in [statechart.md](statechart.md) and
[ADR-0013](adr/0013-labels-as-state-one-step-per-event.md). Steps 1–3 are done.

| Step | Issue | Delivers | Depends on |
| --- | --- | --- | --- |
| 1 | #3 | Minimal local factory: `factory.yaml`, `AGENTS.md`, router, developer and reviewer roles, `workflows/delivery.md`, local skill `factory-delivery` | design (#1) |
| 2 | #5 | CI gates for this repository: agnix, ShellCheck, jactionlint, Betterleaks, blocking | #3 |
| 3 | #4 | Spike: verify the Air capabilities in design §16 (done) | — |
| 4a | #117 | Role skills vendored at pinned commits (`skills/vendor/`, `skills.lock`) | — |
| 4b | #118 | Reviewer: typed findings, no deferred security, spec axis | — |
| 4c | #115 | The tracker closes a ticket's issue on `done` | — |
| 5 | #107 | `factory/gate.py`: the state gate every role agent calls (`check`, `move`) | #106, #117, #118 |
| 6 | #109 | The sweep | #107 |
| 7 | #7 | `deploy.py` and the `[factory]` Air automations | #107, #117 |
| 8 | #8 | The first ticket delivered end to end in Air | #7, #109 |
| 9 | #9 | Phase b roles: approver, doc-writer, retro, distill | #8 |
| 10 | #10 | Phase c routines: preflight, auditor, ops digest | #9 |
| 11 | #11 | Air Mate in Slack bound to the router | #8 |
| 12 | #12 | One small LME ticket end to end in a component repository | #8 |
| — | #108 | The local target on `gate.py`; deletes the old engine | #107 |
| — | #119 | Block a branch that exceeds its ticket's size estimate | — |
| — | #2 | Evaluate factory changes on Harbor tasks, once about 10 factory PRs are merged | #12 |

## Step 1 (#3): what is in and out

In:

- `factory.yaml` with this repository, the GitHub Issues tracker and its
  `factory:*` labels, the local pi target, model tiers, limits and local
  pre-review checks.
- `AGENTS.md` and `roles/_common.md`.
- `roles/{router,developer,reviewer}/` with `role.md`, `skills.txt`,
  `setup.sh` (checks tools and skills; installs nothing).
- `workflows/delivery.md` and `.agents/skills/factory-delivery/`, which runs
  each role as a separate `pi -p` process.
- `gates.confirm_external_writes: true`: every push, PR, comment and label
  change waits for the user until the gates are live.

Out, with the issue that adds it:

- Vendored, pinned skills (`skills.lock`) — #117; generation from templates — later.
- Betterleaks and the other blocking repository gates — #5.
- Designer, fixer as a separate role, approver, auto-approval — #9 and later.
- Claude and Codex harness rendering — later.

Done when: a ticket labelled `factory:ready` goes through developer → checks
→ reviewer → PR locally, and the router posts a summary on the ticket.

Known gaps after the #13 trial run:

- The reviewer runs on `anthropic/claude-haiku-5-5`, the same model family
  as the developer, because pi has no working OpenAI login. Switch
  `models.review` back to an OpenAI model once it has.
- `skills.txt` is not enforced: role processes also see the user's global
  skills.

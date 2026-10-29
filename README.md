# Offline Evaluation factory

A software factory that delivers GitHub Issues as reviewed pull requests, plus
the skill that generates it.

## What is here

- `skills/factory-setup/`: the reusable skill that sets up a factory.
- `factory.yaml`: repositories, tracker stages, models, gates, limits, checks.
- `roles/`: router, developer and reviewer role files, each with a `setup.sh`.
- `workflows/delivery.md`: the delivery workflow.
- `.agents/skills/factory-delivery/`: runs that workflow locally with `pi`.
- `docs/design.md`, `docs/plan.md`, `docs/adr/`: design, plan, decisions.

Project rules are in `AGENTS.md`.

## Run the factory locally on a ticket

1. Check the tools: run each `roles/<role>/setup.sh` (needs `pi`, `gh`, `git`).
   A failure stops the run.
2. Put the `factory:ready` label on the issue. That label starts a run.
3. Deliver the ticket directly: `uv run factory/drive.py run <N>`. No
   orchestrator agent is needed; the command prints a per-role cost table at
   the end (also in `.factory/runs/<N>/events.log`).
4. The driver follows [workflows/delivery.md](workflows/delivery.md): router
   intake, developer in `.factory/worktrees/<n>`, pre-review checks from
   `factory.yaml`, an independent reviewer, fix rounds, then the PR.

Run files stay in `.factory/` (git-ignored).

## Schedule the local loop

`uv run factory/schedule_local.py install` writes a launchd LaunchAgent
(`targets.local.schedule` in `factory.yaml`: label, interval, `PATH`) that runs
`uv run factory/drive.py loop --once` every interval under `lockf`, so cycles
never overlap. Log: `.factory/runs/loop/loop.log`. `status` shows whether the
job is loaded and the last log line; `uninstall` removes it.

launchd jobs do not inherit your shell's environment, so secrets come from the
macOS Keychain: `targets.local.schedule.env_from_keychain` maps each variable to
a Keychain service, and every cycle exports it from `security
find-generic-password` before it runs. A failed lookup logs the variable and
service names and the cycle runs nothing. Store the key once (it prompts for
the value):

```sh
security add-generic-password -s offline-eval-factory-anthropic -a "$USER" -w
```

To rotate it, add `-U` to the same command to update the item in place. The
next cycle picks up the new value; no reinstall is needed. `status` reports,
for each variable, whether its Keychain item exists.

## Approval gate

`gates.confirm_external_writes` is `true` in `factory.yaml`. Every write kind in
`gates.confirm_external_writes_scope` (today: none) is shown to you and waits
for your approval. Pushing the ticket's own branch, issue and PR text, comments
and label changes are auto-approved, so a run can go unattended. Merging stays
with you.

## More

- [Design](docs/design.md)
- [Plan](docs/plan.md)
- [Delivery workflow](workflows/delivery.md)

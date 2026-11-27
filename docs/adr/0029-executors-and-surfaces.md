# ADR-0029: Executors, and named local surfaces
Status: implemented locally (branch `ez/surfaces-executors`), not yet through the factory. Amends ADR-0018 (the harness is a local setting) and ADR-0026 (a new `executor` slot).
Context: the local surface runs every agent on the host, in worktrees under `.factory/worktrees`, and a factory has
one `surface:`. A team cannot choose where an agent runs (host, container, microVM) or deploy several surfaces.
Decision:
1. **Executor slot.** An executor plugin provides `executor` (`ports/executor.py`): `prepare`, `exec`, `collect`,
   `export`, `teardown`, `probe`, `reconcile`. A runner builds the argv, an executor runs it, so any harness runs under
   any executor. Air has none: `harness` or `executor` on an Air surface is a validation error.
2. **Instances.** `executors:` names instances; `type` picks the plugin (`host`, `docker`, `docker-sandbox`), the other
   keys are the plugin's options. Precedence for harness and executor: node mode, role, surface, then `claude-code` and
   `host`. `host` is the default and keeps today's behaviour.
3. **Surfaces.** `surfaces:` names instances of a surface plugin (`type`, `harness`, `executor`, `nodes:` filter,
   `max_parallel`). `targets.air` and `targets.local` stay as aliases. `factory deploy --surface a,b` picks what to
   deploy; `--target` is a deprecated alias. A node served by two surfaces of one selection is `node-on-two-surfaces`.
   `.factory/local.yaml` (git-ignored) overrides `surfaces:` and `executors:` by name; never an Air surface.
4. **Isolated workspace.** `docker` and `docker-sandbox` mount nothing from the host. `prepare` starts an empty
   sandbox; inside it the repositories are cloned from their remote URLs (an optional `seed:` image layer holds the
   clone), `git worktree add` makes the ticket's worktrees, and the engine, team files and role skills are copied in
   read-only. The engine comes from the host's main worktree, so a pull request cannot replace it (ADR-0026 section 2).
5. **Leaving the sandbox.** `factory begin/end` run inside, as on Air, with a scoped token (issues and PRs write,
   contents read). The host `export`s the committed branch as a bundle, pushes it and opens the PR with its own
   credentials, as the local surface does today. `collect` copies run files and logs out before `teardown`, also after
   a failure or timeout.
6. **Secrets.** `secrets: {NAME: HOST_VAR}` maps a sandbox variable to a differently named host variable; never
   `gh auth token`. Harness keys pass by name; login files are opt-in (`auth_files`). `docker-sandbox` uses
   `sbx secret` and falls back to `exec -e` with a doctor warning.
7. **Lifecycle and limits.** One sandbox per run, named `factory-<run id>`. `timeout`, `cpus`, `memory` on the instance;
   `nodes.<n>.timeout` overrides the timeout. `reconcile` removes orphans. `keep: true` is for debugging. `max_parallel`
   (default 4; `host` is 1) runs several sandboxes at once.
8. **Network.** `docker`: `default` or `none`. `docker-sandbox`: an allowlist per sandbox, default GitHub plus the model API.
9. **Plugins.** `host` (this ADR's first slice), `docker`, `docker-sandbox` (optional; closed-source `sbx`, login
   required, version pinned in the instance; see `docs/research/docker-sandboxes.md`).
Out of scope: `opencode`, a proxy for `docker` allowlists, a clone workspace on `host`, building images for `sbx`.
Slices: surfaces and the machine override; the executor port with `host` (done); `docker`; `docker-sandbox`; doctor
probes and the Air validation.
Implementation notes:
- `surfaces.py` holds the instances, the selection, the machine override (`config.load` applies `.factory/local.yaml`)
  and the validation (`factory validate` through `deploy.output_errors`).
- `run_isolated` in `factory_plugins/local/surface.py` is the sandbox run; `run_event` takes it when the role's
  executor is isolated. A host-side `factory begin --check-only` skips `no-go` events before booting a sandbox.
  `deploy --surface` probes each executor, checks mapped tokens, and runs `<cli> --version` in one shared sandbox per
  isolated executor. `factory start` / `factory stop` manage surfaces (Air: `paused: false/true` via REST; local:
  background loop with PID/log files and graceful `SIGTERM`/`SIGINT` drain + second-signal sandbox teardown). The loop
  runs parallel isolated runs without batching, streams `harness.log` live, and periodically calls `executor.reconcile()`.
- `docker-sandbox` injects `github`, `anthropic` and `openai` via `sbx secret set --sandbox --force` over stdin by default
  (`secrets_mode: auto`), falling back to `sbx exec -e` with a warning; supports `dockerfile:` via `docker build` +
  `docker image save` + `sbx template load` (with `--pull never`), `cpus` and `memory`; stops the VM on timeout before
  `collect` so run logs are preserved; and reconciles only sandboxes older than `timeout + 10 min` (from `sbx ls --json`
  `created_at` or the epoch in `factory-<ticket>-<epoch>-<seq>`).
- Verified live against `sbx` v0.54.1 (probe, prepare, policy, secret injection, put/make_writable, timeout+stop+collect,
  git bundle export, reconcile, teardown, and Dockerfile → `sbx template load`); full ticket harness run still pending.

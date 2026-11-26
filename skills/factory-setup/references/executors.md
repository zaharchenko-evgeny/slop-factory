# Executors

An executor says where a local node's agent runs (ADR-0029). Air has none: the platform runs the agent.
The harness (`references/harnesses.md`) builds the command; the executor runs it, so any harness runs in any executor.
Choose per node mode, then role, then surface (`targets.local.executor` or `surfaces.<n>.executor`), else `host`.

## Which one

| Type | Use when | Needs |
| --- | --- | --- |
| `host` (default) | you trust the roles; fastest | nothing |
| `docker` | agents must not touch your machine | Docker daemon, an image |
| `docker-sandbox` | microVM isolation, a network allowlist | `sbx` (closed source, Docker login), a template image or Dockerfile |

## Configure (`factory.yaml`)

```yaml
surface: [air]
surfaces:
  air:    {type: air}
  laptop: {type: local, executor: box, harness: claude-code, nodes: [implement, fix], max_parallel: 4}
executors:
  box:
    type: docker
    dockerfile: executors/box/Dockerfile   # in the team repository; built once per file content
    secrets: {GITHUB_TOKEN: FACTORY_SANDBOX_GH_TOKEN}   # sandbox name: host variable name
    cpus: 2
    memory: 4g
    timeout: 3600          # a node's `timeout_minutes` (or `timeout`) overrides it
    network: default       # or none; an allowlist needs docker-sandbox
    # cache: [uv]          # opt-in named volumes (sets UV_CACHE_DIR=/cache/uv), no workspace state
    # auth_files: [claude-code]   # opt-in: copies the harness login in; doctor-level risk, prefer API keys
    # keep: true           # debugging: the container is committed and stopped, not removed
  sbx:
    type: docker-sandbox
    dockerfile: executors/sbx/Dockerfile       # built via docker and loaded into sbx once per file hash
    # template: registry.example/factory-box:1 # alternative: a full OCI image ref (no local docker needed)
    sbx_version: "0.54.1"
    secrets: {GITHUB_TOKEN: FACTORY_SANDBOX_GH_TOKEN}
    network: {allow: [pypi.org]}               # on top of github.com, api.github.com and the harness model API
    timeout: 3600
```

Names only, never values, go in YAML. A machine override in the git-ignored `.factory/local.yaml` merges by name
(`executors:` and `surfaces:`), for example another image on one laptop. It never changes an Air surface.

## Steps

1. Copy `executors/box/Dockerfile` (for `docker`) or `executors/sbx/` (for `docker-sandbox`; see its `README.md`) from the
   template into the team repository; add your harness CLI.
2. Create a GitHub token with issues and pull requests write and contents read only. Export it under a name that is not
   `GITHUB_TOKEN`: `export FACTORY_SANDBOX_GH_TOKEN=...`. Never map `gh auth token`.
3. Harness API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, ...) pass by name when set on the host. On `docker-sandbox`,
   `github`, `anthropic` and `openai` are injected per sandbox with `sbx secret set --sandbox --force` over stdin by
   default (`secrets_mode: auto`), falling back to `sbx exec -e` with a warning if unavailable.
4. Add the executor and a local surface as above; run `factory validate`.
5. `factory deploy --surface laptop` probes the executor, verifies mapped secrets, and runs `<cli> --version` inside it
   for each harness (one shared sandbox per executor). Fix every `missing` line, then `--apply`.
6. Start and stop surfaces uniformly with `factory start` and `factory stop`:
   - Local surface: `factory start --surface laptop` starts the polling loop in the background (PID in `.factory/local/laptop.pid`, log in `.factory/local/laptop.log`; pass `--foreground` or use `factory run --local --surface laptop` for foreground). `factory stop --surface laptop` drains in-flight runs cleanly (`--force` aborts and tears down active sandboxes).
   - Air surface: `factory start --surface air` enables deployed automations (`paused: false`); `factory stop --surface air` disables them (`paused: true`). Supports `--dry-run` and `--only`.

## Using already-installed agents

- **`host` executor:** any harness already installed and logged in on your machine (`codex`, `junie`, `pi`, `claude`) works directly with zero extra secret exports — it inherits your shell environment, `gh` auth, and `~/.codex`, `~/.junie`, `~/.pi`, `~/.claude` logins.
- **Isolated executors (`docker` / `docker-sandbox`):**
  - Export a scoped `GITHUB_TOKEN` (mapped via `secrets: {GITHUB_TOKEN: FACTORY_SANDBOX_GH_TOKEN}`).
  - To reuse already-authenticated host harness logins instead of exporting model API keys, set `auth_files: auto` (or `auth_files: [codex, junie, pi, claude-code]`) on the executor. Both `docker` and `docker-sandbox` fix ownership inside the sandbox so the CLI can read/write its config. On `docker-sandbox`, you can also log in via `sbx secret set` host-side OAuth.

## How a run works

Nothing is mounted. The sandbox clones each repository from its remote (reusing `/repos/<name>` if baked in as a seed
layer), makes the worktrees with `git worktree add`, gets the engine, team files and skills copied in, runs `factory begin`
and the harness, and is removed. The host then exports only the committed branch diff (`origin/<base>..branch`) as a
bundle, pushes it and opens the PR with its own credentials; push rights never enter the sandbox. Unpushed commits in your
checkout are invisible, as on Air. Run files and the harness log land in `.factory/runs/<id>/`, also after a failure or
timeout (on timeout, the container or microVM is stopped before `collect`, then removed in `teardown`). A timeout is exit
124 and a failed run. Orphaned containers and `sbx` sandboxes older than `timeout + 10 minutes` are cleaned up
automatically by `reconcile` on startup and periodically while the loop runs.

## Validation codes

`unknown-executor`, `bad-max-parallel`, `host-parallel` (`max_parallel` above 1 needs an isolated executor),
`air-surface-has-<key>`, `unknown-surface`, `node-on-two-surfaces` (filter with `nodes:`).

## Known gaps

- `sbx` v0.54.1 lifecycle, `policy allow network`, `secret set` proxy injection, `stop` + `cp` after timeout, `ls --json`
  (`created_at`), and `dockerfile` → `sbx template load` have been smoke-tested live on Apple silicon, but a full
  end-to-end harness ticket run (clone → `factory begin` → harness → `export` → push/PR) has not yet been run against
  a live ticket on `docker` or `docker-sandbox`. Smoke-test on a throwaway ticket before relying on it.
- `sbx` is closed source, needs a Docker login, runs on Apple silicon, Ubuntu 24.04+ and Windows 11 only, and its CLI
  flags/JSON output have changed between releases (renamed in 0.48.0); pin `sbx_version`.
- `auth_files` copies from home paths assumed per harness (`~/.claude`, `~/.codex`, `~/.junie`, `~/.pi/agent/auth.json`);
  check them for your CLI version, and prefer API keys.
- No `opencode` runner and no allowlist proxy for `docker` (both out of scope in ADR-0029).

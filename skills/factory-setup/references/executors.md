# Executors

An executor says where a local node's agent runs (ADR-0029). Air has none: the platform runs the agent.
The harness (`references/harnesses.md`) builds the command; the executor runs it, so any harness runs in any executor.
Choose per node mode, then role, then surface (`targets.local.executor` or `surfaces.<n>.executor`), else `host`.

## Which one

| Type | Use when | Needs |
| --- | --- | --- |
| `host` (default) | you trust the roles; fastest | nothing |
| `docker` | agents must not touch your machine | Docker daemon, an image |
| `docker-sandbox` | microVM isolation, a network allowlist | `sbx` (closed source, Docker login), a template image |

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
    timeout: 3600          # a node's `timeout` overrides it
    network: default       # or none; an allowlist needs docker-sandbox
    # cache: [uv]          # opt-in named volumes, no workspace state
    # auth_files: [claude-code]   # opt-in: copies the harness login in; doctor-level risk, prefer API keys
    # keep: true           # debugging: the container is committed and stopped, not removed
  sbx:
    type: docker-sandbox
    template: registry.example/factory-box:1   # a full image ref; the plugin builds nothing
    sbx_version: "0.54.1"
    network: {allow: [pypi.org]}               # on top of github.com and api.github.com
    timeout: 3600
```

Names only, never values, go in YAML. A machine override in the git-ignored `.factory/local.yaml` merges by name
(`executors:` and `surfaces:`), for example another image on one laptop. It never changes an Air surface.

## Steps

1. Copy `executors/box/Dockerfile` from the template into the team repository; add your harness CLI.
2. Create a GitHub token with issues and pull requests write and contents read only. Export it under a name that is not
   `GITHUB_TOKEN`: `export FACTORY_SANDBOX_GH_TOKEN=...`. Never map `gh auth token`.
3. Harness API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, ...) pass by name when set on the host.
4. Add the executor and a local surface as above; run `factory validate`.
5. `factory deploy --surface laptop` probes the executor and runs `<cli> --version` inside it for each harness. Fix every
   `missing` line, then `--apply`; `factory run --local --surface laptop` starts the loop.

## How a run works

Nothing is mounted. The sandbox clones each repository from its remote, makes the worktrees with `git worktree add`, gets
the engine, team files and skills copied in, runs `factory begin` and the harness, and is removed. The host then exports
the committed branch as a bundle, pushes it and opens the PR with its own credentials; push rights never enter the
sandbox. Unpushed commits in your checkout are invisible, as on Air. Run files and the harness log land in
`.factory/runs/<id>/`, also after a failure or timeout. A timeout is exit 124 and a failed run.

## Validation codes

`unknown-executor`, `bad-max-parallel`, `host-parallel` (`max_parallel` above 1 needs an isolated executor),
`air-surface-has-<key>`, `unknown-surface`, `node-on-two-surfaces` (filter with `nodes:`).

## Known gaps

- Never run against a real Docker daemon or `sbx`: only the commands are tested with a fake runner. Smoke-test on a
  throwaway ticket before relying on it.
- `docker-sandbox` passes secrets with `sbx exec -e`, readable inside the VM, not `sbx secret`: which GitHub credentials
  `sbx` can inject is unconfirmed. Use a scoped, short-lived token.
- `docker-sandbox` `reconcile` removes every `factory-*` sandbox (`sbx ls` shows no age); run it with the loop stopped.
  `docker` `reconcile` removes containers older than `timeout` plus 10 minutes.
- Whether a killed `sbx exec` client stops the process in the VM is untested; the plugin removes the VM instead.
- `sbx` is closed source, needs a Docker login, runs on Apple silicon, Ubuntu 24.04+ and Windows 11 only; pin its version.
- Parallel runs go in batches: a batch waits for its slowest run before the next poll.
- `auth_files` copies from home paths assumed per harness (`~/.claude`, `~/.codex`, ...); check them.
- No `opencode` runner and no allowlist proxy for `docker`; a `docker-sandbox` image cannot be built from a Dockerfile yet.
- Loading the image into `sbx` is manual (publish it, or `sbx template load`).
- The `sbx` version, flags and JSON output change between releases (renamed in 0.48.0).

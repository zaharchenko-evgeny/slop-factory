# `docker-sandbox` (`sbx`) executor template

This template builds a custom image for the `docker-sandbox` executor (ADR-0029): one isolated microVM per factory run, with a per-sandbox network allowlist and host-proxy secret injection.

## 1. Prerequisites

- Supported host: macOS 14+ (Apple silicon), Ubuntu 24.04+ with KVM, or Windows 11 x64.
- Install `sbx` and log in to Docker (required even for local microVMs):
  ```bash
  brew install docker/tap/sbx        # macOS; see docs.docker.com/ai/sandboxes/install/ for Linux/Windows
  sbx version                        # record this version in `sbx_version:` in factory.yaml
  sbx login                          # or: echo "$DOCKER_PAT" | sbx login --username <id> --password-stdin
  sbx policy init balanced           # required once so non-interactive runs do not prompt
  ```
- If you build from `Dockerfile` locally (instead of pulling a published `template:`), `docker` must also be installed so the executor can run `docker build` and `docker image save` before `sbx template load`.

## 2. Configure `factory.yaml`

Copy `Dockerfile` into your team repository (for example `executors/sbx/Dockerfile`), uncomment your harness CLI install line, and declare the executor:

```yaml
surfaces:
  laptop-sbx:
    type: local
    executor: sbx
    harness: <your-harness>                # e.g. codex, junie, pi, or your configured harness
    nodes: [implement, fix]
    max_parallel: 4

executors:
  sbx:
    type: docker-sandbox
    dockerfile: executors/sbx/Dockerfile   # built & loaded into sbx once per file hash (uses `--pull never`)
    # template: registry.example/factory-sbx:1  # alternative: a published OCI ref (no local docker needed)
    sbx_version: "0.54.1"                  # pin the CLI version; `factory deploy` checks it
    cpus: 2
    memory: 4g
    timeout: 3600
    secrets:
      GITHUB_TOKEN: FACTORY_SANDBOX_GH_TOKEN
    network:
      allow: [pypi.org, files.pythonhosted.org]  # added on top of GitHub + your harness model API
    # secrets_mode: auto                   # default: uses `sbx secret set --sandbox --force` via stdin
    # keep: true                           # debugging: leave the sandbox after the run (never snapshot it)
```

## 3. Image build & load

- **Automatic (`dockerfile:`):** on the first run for a given Dockerfile hash, the executor runs `docker build`, saves a temporary tar with `docker image save`, and imports it with `sbx template load`. Subsequent runs reuse the cached `factory-sbx-<hash>:latest` template and start sandboxes with `--pull never`.
- **Manual (`template:`):** build and load (or push) the image yourself:
  ```bash
  docker build -t my-org/factory-sbx:1 -f executors/sbx/Dockerfile executors/sbx
  docker image save my-org/factory-sbx:1 -o /tmp/factory-sbx.tar
  sbx template load /tmp/factory-sbx.tar
  rm /tmp/factory-sbx.tar
  ```
  Then reference `template: my-org/factory-sbx:1` (and `pull: never` if only loaded locally) in `factory.yaml`.

## 4. Secrets & network

- **Secrets:** export a scoped GitHub token (issues and pull requests write, contents read only — never `gh auth token`) under a separate variable such as `FACTORY_SANDBOX_GH_TOKEN`, plus your harness API key (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, etc.).
  - By default (`secrets_mode: auto`), the executor injects `github`, `anthropic`, and `openai` per sandbox via `sbx secret set <service> --sandbox <name> --force` over stdin so the host proxy injects auth headers and raw tokens stay out of the VM (`GH_TOKEN` uses `sbx`'s sentinel; model API keys pass `proxy-managed` as a non-secret sentinel).
  - If `sbx secret set` fails for a service (or you set `secrets_mode: env`), it falls back to `sbx exec -e` (value passed via process env, never argv, though readable inside the VM) and logs a warning.
- **Network:** every sandbox automatically allows `github.com`, `api.github.com`, and the active harness's model API host (`api.anthropic.com`, `api.openai.com`, `api.jetbrains.ai`), plus any hostnames in `network.allow`.

## 5. Smoke-test

1. Probe prerequisites and verify the harness CLI inside a sandbox:
   ```bash
   export FACTORY_SANDBOX_GH_TOKEN=...
   export ANTHROPIC_API_KEY=...
   factory deploy --surface laptop-sbx --apply
   ```
2. Run one poll pass against a throwaway ticket:
   ```bash
   factory run --local --surface laptop-sbx --once
   ```
3. Inspect `.factory/runs/<ticket>-<epoch>-<seq>/harness.log` (and verify any secrets warnings). If you enabled `keep: true` to debug, clean up manually with `sbx rm --force <name>`.

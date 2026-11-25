# Docker Sandboxes (`sbx`) research

Researched from docs.docker.com and github.com/docker/sbx-releases only, read-only (no install, no state-changing commands).
"unknown" = docs silent. Doc base: https://docs.docker.com/ai/sandboxes/ . Docs pages fetched as `<page>.md`.

## 1. CLI, install, platforms, requirements
- CLI is `sbx`. Standalone: no Docker Desktop or Docker Engine needed. https://docs.docker.com/ai/sandboxes/install/
- Install: macOS `brew install docker/tap/sbx`; Windows `winget install -h Docker.sbx` (or MSI); Ubuntu `apt install docker-sbx` after `curl -fsSL https://get.docker.com | sudo REPO_ONLY=1 sh`; manual tarball/.deb from https://github.com/docker/sbx-releases . Then `sbx login` (Docker OAuth; required).
- Platforms: macOS 14+ Apple silicon only; Windows 11 x64 (Windows Hypervisor Platform); Ubuntu 24.04+ x64/arm64 with KVM and user in `kvm` group. Ubuntu derivatives unsupported. Nested virtualisation needed inside VMs. (install page)
- Isolation is a microVM per sandbox with its own Linux kernel and private Docker Engine. https://docs.docker.com/ai/sandboxes/security/isolation/
- Headless login: `echo $DOCKER_PAT | sbx login --username <id> --password-stdin` (PAT, Read scope). https://docs.docker.com/ai/sandboxes/workflows/automation/
- Also a cloud variant (`sbx --cloud ...`, Docker-managed compute, API/SDK). Local and cloud have separate secrets, policies, templates. https://docs.docker.com/ai/sandboxes/cloud/local-vs-cloud/
- Version drift: v0.48.0 renamed flags and JSON output (see automation page). Pin the `sbx` version.

## 2. Lifecycle
- Commands: `sbx create [--name N] AGENT [PATH...]`, `sbx run`, `sbx exec`, `sbx stop`, `sbx rm [--force]`, `sbx ls --json`, `sbx prune`. https://docs.docker.com/ai/sandboxes/usage/
- Per-run create and destroy works: `sbx create --name ci-task --clone claude .` -> `sbx exec ci-task -- ...` -> `sbx rm --force ci-task` is the documented CI pattern (automation page). `sbx run --rm` removes after the agent exits.
- Sandboxes persist until removed. After `create` a sandbox auto-stops when no session is attached; `exec` starts a stopped sandbox. `run -d` keeps it running.
- Arbitrary commands: yes. `sbx exec [-e K=V] [--env-file] [-u user] [-w dir] [-i] [-t] SANDBOX CMD [ARG...]` (https://docs.docker.com/reference/cli/sbx/exec/). `sbx run shell` gives an agent-less sandbox. A plain `exec` does not start a shell: wrap in `bash -c` for redirects or `/etc/sandbox-persistent.sh` vars.
- Headless: `exec` runs in the foreground only, no `--detach`. `--no-input` makes prompting commands fail. Confirmations fail with exit 1 when stdin is not a TTY, so use `--force`. Create workspace dirs first (no TTY = failure). `sbx policy init <preset>` must be run once, else `run` errors non-interactively.
- Exit codes: `exec` returns the command's status; 125 = could not run (e.g. missing sandbox); 127 = missing executable (local). `run --rm` exits with the agent's status. (automation page)
- Timeouts: no per-exec timeout flag. Only `lifecycle.*.timeout` for host hooks in env files and `egress.stalledConnectionTimeoutSeconds` (0-3600). Cloud sandboxes have TTL (`sbx ttl`). Local TTL: unknown.
- Output: results on stdout, progress/errors on stderr; `--format json|yaml`, `-q`, `--no-progress`.
- Extras seen in the CLI index (not read in detail): `sbx schedule`, `sbx ssh`, `sbx events`, `sbx env` (environment files), `sbx diagnose`.

## 3. Filesystem model
- Host mount is optional. `sbx run` mounts CWD by default at the same absolute path (virtiofs passthrough, live, read-write). Extra workspaces `PATH[:TARGET][:ro]`. https://docs.docker.com/ai/sandboxes/architecture/
- NO host mount: yes, since v0.42.0. `sbx create --name X AGENT` with no path = mountless. Default dir `/home/agent/workspace` (template WORKDIR). Clone repos inside; push to a remote. https://docs.docker.com/ai/sandboxes/workflows/git/
- Clone mode `--clone`: private git clone in the sandbox, host repo read-only at `/run/sandbox/source`, a `sandbox-<name>` remote added to the host repo (`git fetch sandbox-<name>`). Needs a git primary workspace, fixed at create time, not from a non-main worktree. Removal drops the clone.
- Persistence: the whole VM (packages, images, in-sandbox files, history) persists across stop/start until `rm`. Each sandbox has its own Docker daemon and image cache. Only agent-skills store (and kit volumes) are shared/outlive removal.
- Snapshot/export: `sbx template save SANDBOX name:tag [--output x.tar]` saves the container filesystem (not mounts, not `/var/lib/docker`; secrets in files get baked in; user agent config files are recreated). `sbx template load`, `sbx run -t name:tag`. `sbx move` copies local<->cloud. Whole-VM or in-sandbox Docker-image snapshot: unknown.
- Copy: `sbx cp SRC DST` with one side `SANDBOX:/abs/path`, files or dirs, not sandbox-to-sandbox. Reference page not read in full; whether `cp` preserves ownership and modes: unknown.

## 4. Images and customisation
- Built-in agents map to `docker.io/sbx/<agent>:latest` (Debian). Older `docker/sandbox-templates:<variant>` (Ubuntu, user `agent` with sudo, git, Docker CLI, Node, Python/uv, Go, Java). https://docs.docker.com/ai/sandboxes/customize/author/base-images/
- Custom: Kits. v3 workload = YAML descriptor + Dockerfile (`FROM docker/sandbox-templates:...` or `docker.io/sbx/shell-image:latest`, or your own Linux image). Mixins add tools with `--kit`. Templates via `--template/-t <full image ref>` (no implicit docker.io). Kits are published as OCI images. https://docs.docker.com/ai/sandboxes/customize/
- Preinstall tools (uv, gh, git, claude/codex/pi): yes by Dockerfile or `template save` after manual setup. Install system packages as root, user tools as `agent`. `uv` and `git` ship in sandbox-templates; `gh` and `pi`: unknown. Agents also have sudo plus apt/pip/npm at runtime (needs network allow rules).
- Private-registry pulls use `sbx secret set --registry`; registry mirror settings exist. `pullPolicy` default `always`.

## 5. Network
- Default: all outbound TCP (HTTP/S, SSH) blocked unless allowed; UDP off; ICMP blocked; DNS via internal resolver enforcing policy. https://docs.docker.com/ai/sandboxes/security/defaults/
- Traffic goes through a host-side proxy. Policy = preset (`allow-all`, `balanced`, `deny-all`; set with `sbx policy init`, non-interactive CLI needs it) plus rules.
- Rules: `sbx policy allow|deny network "host,*.pypi.org"`, per sandbox `--sandbox N`, `--deny-network` on create/run, L7 `--method/--path` (local only). Org policies override local. https://docs.docker.com/ai/governance/local/network-rules/
- Limits: hostname matching by SNI/Host. A denied host sharing an IP with an allowed one can still be reached. Cloud has separate network-only policy.
- Upstream proxy: `sbx settings set proxy|proxy.sandbox|proxy.daemon|no_proxy.*`; follows OS proxy by default; experimental, HTTP(S) only. https://docs.docker.com/ai/sandboxes/configuration/
- Ports: not reachable from the host by default; `--publish` / `sbx port publish`.

## 6. Credentials
- Model: host proxy injects the real credential into outbound HTTP headers; sandbox sees a sentinel (`proxy-managed`). https://docs.docker.com/ai/sandboxes/configuration/credentials/
- `sbx secret set <service> [--sandbox N]` (global by default; interactive, or `-t <token>`, `--ref op://...` / AWS ARN, `--command`); `sbx secret import --all` reads standard env vars (e.g. in CI); `sbx secret set-custom` (experimental) for domain + env var. OAuth logins for Claude Code, Codex, Cursor etc. run host side.
- Stored in macOS Keychain / Windows Credential Manager / Linux Secret Service; headless Linux falls back to a 0700 file under `~/.config/com.docker.sandboxes`.
- Plain env: `-e K[=V]`, `--env-file` on create/run/exec. Readable inside the VM, so not for secrets. Storing keys in the sandbox FS bakes them into templates.
- GitHub token / `gh`: see https://docs.docker.com/ai/sandboxes/workflows/authentication/ (not read in detail). Cloud has a separate secret store.

## 7. Resources and parallelism
- `--cpus N` (default 0 = all host CPUs, capped at 16 on Linux arm64); memory via env-file `sandboxOptions.memory` (`8g`); `sbx create/run` memory flag: unknown. Env-file doc: https://docs.docker.com/ai/sandboxes/configuration/environment-files/
- Parallel: many sandboxes allowed (distinct `--name`, same or different workspace). No documented cap. Each costs a VM plus its own Docker store and disk. Disk quota: unknown.

## 8. Supported agents
- Docs list: Claude Code, Codex, Copilot, Cursor, Devin, Docker Agent, Droid, Kiro, OpenCode, Shell. https://docs.docker.com/ai/sandboxes/agents/
- The sbx-releases README also names Gemini CLI. The docs list omits it, so treat Gemini as unconfirmed. Pi CLI is not listed; it could be added through a Shell sandbox or a custom kit (untested).
- Headless agent use: `sbx exec NAME -- claude --dangerously-skip-permissions -p "..."`.

## 9. Limitations, licensing, cost
- `sbx` and local compute are free, including commercial use. Cloud is pay-as-you-go. Org governance is a separate paid plan. Model-provider charges are separate. https://docs.docker.com/ai/sandboxes/
- License of the CLI: closed source. The sbx-releases repo license is NOASSERTION (GitHub API); binaries only. Terms text: unknown.
- Login to Docker is mandatory (even for local); needs network to Docker endpoints; usage telemetry on (opt out `SBX_NO_TELEMETRY=1` or `sbx settings set telemetry.disabled true`). https://docs.docker.com/ai/sandboxes/faq/
- macOS Apple silicon only; no Intel Mac, no Linux other than Ubuntu 24.04+.
- `exec` cannot detach; confirmations need `--force`; JSON schema changed in 0.48.0.
- Avoid network or cloud-synced folders as workspaces (slow passthrough). A direct mount gives the agent write access to host `.git` hooks. Host Docker daemon is never reachable.
- Docker daemon data is per sandbox, so no layer sharing between sandboxes (disk heavy).
- Cloud: no host paths, no L7 rules, TTL expiry.

## Implications for a factory executor plugin (prepare/exec/collect/export/teardown)
- prepare: `sbx policy init` once per host; `sbx create --name factory-<run> shell|claude [--cpus N]` with no path (mountless) or `--clone` for the target repo. Pin the `sbx` version and parse `--json`.
- prepare: bake `uv`, `gh`, `git`, and agent CLIs into a kit/template image (`-t`); seed secrets with `sbx secret set` or `import --all`, never `-e`.
- exec: `sbx exec -w DIR -e K=V NAME -- cmd` is a fully scriptable executor. Use its exit status; treat 125 and 127 as infrastructure errors. Wrap shell syntax in `bash -c`.
- exec: there is no built-in timeout, so the plugin enforces it by killing the `sbx exec` client and then `sbx rm --force`. Whether the in-VM process dies on client kill: unknown, so test it.
- collect/export: `sbx cp NAME:/abs/path ./out` for artifacts (absolute paths only), or `git fetch sandbox-<name>` in clone mode. A mountless sandbox must push to a remote or use `cp`.
- export: `sbx template save` for reusable environments, but keep credentials out of the filesystem.
- network: `deny-all` or `balanced` plus per-sandbox `sbx policy allow network --sandbox NAME host,...` for model API, GitHub, and PyPI; remember the SNI/IP-sharing caveat.
- teardown: always `sbx rm --force NAME` in a `finally`; run `sbx prune` for strays; removal deletes in-VM state and per-sandbox policy rules.
- capacity: microVM per run needs macOS arm64 or Ubuntu with KVM, and no cap is documented, so limit concurrency by CPU, RAM, and disk. Cloud (`--cloud`) is the alternative where there is no virtualisation, but it costs money and has no host paths.
- risk: closed binary, mandatory Docker login, and fast-moving CLI flags. A DOCKER PAT must be provisioned on the runner.

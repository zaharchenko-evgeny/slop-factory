# Environment variables

One row per variable. Tests exclude `factory/tests/`; `test_readme.py` fails when a `FACTORY_*` name in `factory/*.py` or `engine/template-v1/` has no row here.

| Variable | Set by | Read in | Required |
|---|---|---|---|
| `FACTORY_ENV` | Air deploy: the only variable of a planned environment (`factory deploy` owns an environment's whole variable set) | `factory/doctor.py`, `factory/factory_plugins/air/deploy.py` (written to the plan) | required in an Air run (`read`, `write` or `ops`) |
| `FACTORY_NODE` | Air deploy: only in the environment of a node with custom MCP servers | `factory/team_setup.py` | optional; the node whose custom MCP servers `factory setup` writes to `.mcp.json`; unset: no file |
| `FACTORY_NODE_REPO` | `factory setup` (from the checkout's `origin`: the main repository of the automation copy, ADR-0024) | setup scripts (`environments/*/setup.sh`, `roles/*/setup.sh`) | optional; unset without an `origin` |
| `FACTORY_TEAM` | the startup wrapper of `factory` (internal) | `factory/config.py`, `factory/cli.py`, `factory/factory_plugins/local/surface.py`, `engine/template-v1/roles/` | optional; defaults to the `factory-team` directory in the home directory when it exists, else the working directory |
| `FACTORY_ENGINE_DIR` | tests and development | `engine/template-v1/.air/cloud/startup.sh` | optional; an engine checkout whose `factory-step` skill the startup script installs; defaults to the `factory-main` directory in the home directory |
| `FACTORY_ENGINE` | tests and development | `factory/config.py` | optional; defaults to this checkout |
| `FACTORY_WRITE_TOKEN` | a person, in Air: a secret a team declares in the environments that write a `role: project` repository | `factory/factory_plugins/github/tracker.py` (the GH_TOKEN of a write to a `role: project` repository), `factory/doctor.py` | optional; the fallback token of `issues_token_env` |
| `issues_token_env` (a setting; e.g. `FACTORY_ISSUES_TOKEN`) | a person, in Air: the secret it names, with Issues read/write on every repository; `factory deploy` adds it to every environment | `factory/factory_plugins/github/tracker.py` (the GH_TOKEN of the gate's issue, label and comment writes to a `role: project` repository, before `FACTORY_WRITE_TOKEN`), `factory/doctor.py` (`write:label:<repo>`), `factory/factory_plugins/air/deploy.py` (by name) | optional; without it project writes use `FACTORY_WRITE_TOKEN` |
| `FACTORY_GH` | tests and development | `factory/factory_plugins/github/tracker.py` | optional; defaults to `gh` |
| `FACTORY_DRY` | tests and development | no code reads it today | optional |
| `FACTORY_BASE` | CI (`.github/workflows/quality-gates.yml`) for the descriptor checks | the checks' `run` commands | optional; defaults to `main` |
| `FACTORY_MCP_AIR_AUTOMATIONS` | the operator, for `factory deploy` and `factory contracts refresh` | `factory/factory_plugins/air/deploy.py`, `factory/factory_plugins/air/contracts.py` | required for those commands |
| `FACTORY_MCP_AIR_CLOUD` | the operator, for `factory deploy` and `factory contracts refresh` | `factory/factory_plugins/air/deploy.py`, `factory/factory_plugins/air/contracts.py` | required for those commands |

## Team variables

A team declares its own variables and secrets per environment in `factory.project.yaml`:

```yaml
environments:
  write:
    variables: {YOUTRACK_URL: https://example.youtrack.cloud}   # name: non-secret value
    secrets: [YOUTRACK_TOKEN]                                   # names only, never values
```

- `factory validate` rejects a variable whose name starts with `FACTORY_` (reserved for the factory).
- `factory deploy` plans an environment's variables as the declared ones plus `FACTORY_ENV`. A person sets each secret's value in Air;
  `deploy --dry-run` lists one that is missing as `set by a person: <env> secret <NAME>` and does not fail.
  Air's API cannot delete a variable: `deploy` prints `remove by hand in Air: <env> variable <NAME>` for one the plan lacks,
  does not fail on it, and a person removes it in the environment's settings.
- `factory doctor` probes `env:<NAME>` for every declared variable and secret of `FACTORY_ENV`: `ok` when set and non-empty,
  `missing` otherwise. A missing probe fails the doctor.

# What the Air image provides

`uv`, `gh`, `git`, `python3`, `node`/`npx` and `curl` are installed; `conftest` and `shellcheck` are not. The startup script downloads `uv` and `gh` only when missing. Startup is checked on Air itself by the `doctor` automations (#610).

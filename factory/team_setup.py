"""`factory setup --environment <name>` (ADR-0021): what the Air startup script runs after it installs the engine.

It validates the definition, installs the skills of every role that runs in the environment (the engine's own
skills too) under `~/.agents/skills/`, checking each sourced skill against its sha256, then runs the
environment's `setup` script and each role's `roles/<role>/setup.sh`. It then writes `.mcp.json` in the checkout
with the custom MCP servers of the node in `FACTORY_NODE`; without it, no file. The checkout's repository is the
automation's main one: scripts get it as `FACTORY_NODE_REPO` (ADR-0024).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import config
import definition

ENGINE_SKILLS = ("factory-step", "factory-troubleshoot")


def tree_hash(d):
    """The sha256 of a skill directory: every file's path and bytes, sorted (as scripts/vendor-skills.py)."""
    h = hashlib.sha256()
    for f in sorted(p for p in Path(d).rglob("*") if p.is_file()):
        h.update(f.relative_to(d).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def _fetch_run(cmd, attempts=4, wait=2, sleep=time.sleep, clean=None):
    """Run network command `cmd`; retry a failure with backoff (wait, 2*wait, ...) after removing `clean`: a GitHub
    429 or a network blip is transient (offline-eval-factory#84). The last failure raises with git's or npm's stderr."""
    for i in range(attempts):
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode == 0:
            return
        if i + 1 < attempts:
            if clean:
                shutil.rmtree(clean, ignore_errors=True)
            sleep(wait * 2 ** i)
    raise RuntimeError(f"{' '.join(cmd)} failed {attempts} times (exit {p.returncode}): {(p.stderr or '').strip()[-500:]}")


def _npm(name, version, spec, tmp):
    _fetch_run(["npm", "pack", f"{name}@{version}", "--pack-destination", tmp])
    subprocess.run(["tar", "-xzf", str(next(Path(tmp).glob("*.tgz"))), "-C", tmp], check=True)
    return Path(tmp) / "package" / spec["path"]


def _git(name, version, spec, tmp):
    src = f"{tmp}/src"
    _fetch_run(["git", "clone", "--quiet", f"https://github.com/{name}.git", src], clean=src)
    subprocess.run(["git", "-C", src, "checkout", "--quiet", version], check=True)
    return Path(src) / spec["path"]


SOURCES = {"npm": _npm, "github": _git}  # a skill source is a fetch scheme, not a tracker


def fetch(spec, tmp):
    """The directory of a sourced skill (`github:owner/repo@commit` or `npm:package@version`), fetched into `tmp`."""
    kind, _, ref = spec["source"].partition(":")
    name, _, version = ref.rpartition("@")
    Path(tmp).mkdir(parents=True, exist_ok=True)
    if kind not in SOURCES:
        raise ValueError(f"unknown skill source: {spec['source']}")
    return SOURCES[kind](name, version, spec, tmp)


def roles_in(defn, environment):
    """The roles of the enabled agent nodes that run in `environment` (a node's `environments` list counts)."""
    f, w = defn["factory"], defn["workflow"]
    out = []
    for name, n in (w.get("nodes") or {}).items():
        role = ((f.get("agents") or {}).get(n.get("agent")) or {}).get("role")
        if role and n.get("enabled") is not False and environment in definition.node_environments(defn, name) and role not in out:
            out.append(role)
    return out


def write_mcp_json(defn, checkout, node=None):
    """Write `<checkout>/.mcp.json` with the custom MCP servers of `node`. `${VAR}` references stay as written.
    Nothing is written without a node or when it has none: setup runs per environment, so a node's servers
    must not reach other nodes (deploy gives a node with custom servers an environment of its own)."""
    servers = definition.mcp_custom(definition.mcp(defn, node)) if node else {}
    if servers:
        (Path(checkout) / ".mcp.json").write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n")
    return sorted(servers)


def install(defn, environment, home, fetcher=fetch, run=subprocess.run, checkout=None, node=None, node_repo=None):
    """Install the environment's skills and run its setup scripts; returns the lines it printed. Raises SystemExit
    with the reasons when the definition is invalid or a skill's sha256 differs. `node_repo` (the repository of the
    automation copy, ADR-0024) is exported to the setup scripts as `FACTORY_NODE_REPO`."""
    errs = definition.errors(defn)
    if environment not in (defn["factory"].get("environments") or {}):
        errs.append(f"unknown-environment: {environment}")
    if errs:
        raise SystemExit("factory setup: " + "; ".join(errs))
    root, target = defn["root"], Path(home) / ".agents/skills"
    target.mkdir(parents=True, exist_ok=True)
    roles = roles_in(defn, environment)
    skills = {}
    for role in roles:
        skills.update(definition.role_skills(defn, role))
    lines = []
    for name in ENGINE_SKILLS:
        _copy(config.engine_root() / "skills" / name, target / name)
        lines.append(f"skill {name}: engine")
    with tempfile.TemporaryDirectory() as tmp:
        for name, spec in skills.items():
            if "source" in spec:
                src = fetcher(spec, f"{tmp}/{name}")
                if tree_hash(src) != spec["sha256"]:
                    raise SystemExit(f"factory setup: skill {name}: sha256 differs from factory.yaml")
                lines.append(f"skill {name}: {spec['source']}")
            else:
                src = root / spec["path"]
                lines.append(f"skill {name}: {spec['path']}")
            _copy(src, target / name)
    env = defn["factory"]["environments"][environment]
    scripts = ([root / env["setup"]] if env.get("setup") else []) + \
              [root / "roles" / r / "setup.sh" for r in roles if (root / "roles" / r / "setup.sh").is_file()]
    for script in scripts:
        extra = {"env": {**os.environ, "FACTORY_NODE_REPO": node_repo}} if node_repo else {}
        run(["bash", str(script)], check=True, cwd=root, **extra)
        lines.append(f"ran {script.relative_to(root)}")
    if node is not None and node not in (defn["workflow"].get("nodes") or {}):
        raise SystemExit(f"factory setup: unknown node {node}")
    lines += set_credentials(defn, environment, checkout or Path.cwd(), run=run)
    written = write_mcp_json(defn, checkout or Path.cwd(), node)
    if written:
        lines.append(f".mcp.json: {', '.join(written)}")
    return lines


SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def set_credentials(defn, environment, checkout, run=subprocess.run, environ=os.environ):
    """Make `git push` work in each additional checkout of `environment` that is `write: true` with a `token`
    (ADR-0027): its git credential helper answers with the secret the `token` names. The helper reads the secret
    from the environment when git asks, so the value is never written to disk or printed. The main checkout and a
    checkout without a `token` keep Air's credential. Returns the lines it printed."""
    root = Path(os.environ.get("FACTORY_WORKSPACES") or Path(checkout).resolve().parent)
    lines = []
    for r in definition.repositories(defn, environment)[1:]:
        if not (r["write"] and r["token"]):
            continue
        where = root / r["repo"].rsplit("/", 1)[-1]
        if not SECRET_NAME.fullmatch(r["token"]):
            raise SystemExit(f"factory setup: {r['repo']}: `token` is not a secret name")
        if not (where / ".git").exists():
            lines.append(f"credential {r['repo']}: no checkout at {where}")
        elif not environ.get(r["token"]):
            lines.append(f"credential {r['repo']}: {r['token']} is not set")
        else:
            if not REPO_NAME.fullmatch(r["repo"]):
                raise SystemExit(f"factory setup: {r['repo']}: not an owner/repo name")
            # answer only `get` for https://github.com/<repo>, with or without `.git`; stay silent for any other remote
            helper = ('!f() { test "$1" = get || return 0; i=$(cat); '
                      'for l in protocol=https host=github.com; do printf "%s\\n" "$i" | grep -qxF "$l" || return 0; done; '
                      f'printf "%s\\n" "$i" | grep -qxF -e "path={r["repo"]}" -e "path={r["repo"]}.git" || return 0; '
                      f'echo username=x-access-token; echo "password=${r["token"]}"; }}; f')
            run(["git", "-C", str(where), "config", "--local", "credential.helper", ""], check=True)  # drop Air's helper
            run(["git", "-C", str(where), "config", "--local", "credential.useHttpPath", "true"], check=True)
            run(["git", "-C", str(where), "config", "--local", "--add", "credential.helper", helper], check=True)
            lines.append(f"credential {r['repo']}: {r['token']}")
    return lines


def checkout_repo(checkout):
    """`owner/repo` of the checkout's `origin`: the main repository of the automation copy; None without one."""
    url = subprocess.run(["git", "-C", str(checkout), "remote", "get-url", "origin"], capture_output=True, text=True)
    return definition.full_name(url.stdout.strip()) if url.returncode == 0 and url.stdout.strip() else None


def _copy(src, dst):
    if not (Path(src) / "SKILL.md").is_file():
        raise SystemExit(f"factory setup: no SKILL.md in {src}")
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)


def main(argv):
    if len(argv) != 2 or argv[0] != "--environment":
        print("usage: factory setup --environment <name>")
        return 2
    if not definition.present(config.team_root()):
        print("factory setup: the team root has no factory.yaml and workflow.yaml (ADR-0021)")
        return 2
    for line in install(definition.load(config.team_root()), argv[1], Path.home(), node=os.environ.get("FACTORY_NODE"),
                       node_repo=checkout_repo(Path.cwd())):
        print(line)
    return 0

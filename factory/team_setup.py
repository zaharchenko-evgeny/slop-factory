"""`factory setup --environment <name>` (ADR-0021): what the Air startup script runs after it installs the engine.

It validates the definition, installs the skills of every role that runs in the environment (the engine's own
skills too) under `~/.agents/skills/`, checking each sourced skill against its sha256, then runs the
environment's `setup` script and each role's `roles/<role>/setup.sh`.
"""
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

import config
import definition

ENGINE_SKILLS = ("factory-step",)


def tree_hash(d):
    """The sha256 of a skill directory: every file's path and bytes, sorted (as scripts/vendor-skills.py)."""
    h = hashlib.sha256()
    for f in sorted(p for p in Path(d).rglob("*") if p.is_file()):
        h.update(f.relative_to(d).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def fetch(spec, tmp):
    """The directory of a sourced skill (`github:owner/repo@commit` or `npm:package@version`), fetched into `tmp`."""
    kind, _, ref = spec["source"].partition(":")
    name, _, version = ref.rpartition("@")
    Path(tmp).mkdir(parents=True, exist_ok=True)
    if kind == "npm":
        subprocess.run(["npm", "pack", f"{name}@{version}", "--pack-destination", tmp], check=True, capture_output=True)
        subprocess.run(["tar", "-xzf", str(next(Path(tmp).glob("*.tgz"))), "-C", tmp], check=True)
        return Path(tmp) / "package" / spec["path"]
    if kind == "github":
        src = f"{tmp}/src"
        subprocess.run(["git", "clone", "--quiet", f"https://github.com/{name}.git", src], check=True)
        subprocess.run(["git", "-C", src, "checkout", "--quiet", version], check=True)
        return Path(src) / spec["path"]
    raise ValueError(f"unknown skill source: {spec['source']}")


def roles_in(defn, environment):
    """The roles of the enabled agent nodes that run in `environment`."""
    f, w = defn["factory"], defn["workflow"]
    default = (w.get("settings") or {}).get("environment")
    out = []
    for n in (w.get("nodes") or {}).values():
        role = ((f.get("agents") or {}).get(n.get("agent")) or {}).get("role")
        if role and n.get("enabled") is not False and (n.get("environment") or default) == environment and role not in out:
            out.append(role)
    return out


def install(defn, environment, home, fetcher=fetch, run=subprocess.run):
    """Install the environment's skills and run its setup scripts; returns the lines it printed. Raises SystemExit
    with the reasons when the definition is invalid or a skill's sha256 differs."""
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
        run(["bash", str(script)], check=True, cwd=root)
        lines.append(f"ran {script.relative_to(root)}")
    return lines


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
    for line in install(definition.load(config.team_root()), argv[1], Path.home()):
        print(line)
    return 0

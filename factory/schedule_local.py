#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Schedule the local factory loop with a launchd LaunchAgent (factory.yaml targets.local.schedule).

The job runs `uv run factory/drive.py loop --once` from the factory root every interval under
`lockf -t 0`, so a cycle started while another runs exits at once (75) without running the driver.
Output goes to <run_dir>/loop/loop.log. `env_from_keychain` (variable → Keychain service) wraps the cycle
in /bin/sh, which exports each variable from `security find-generic-password` at run time; a failed
lookup logs the variable and service names and exits 1 before anything runs. Values never reach the plist.

CLI: uv run factory/schedule_local.py install | uninstall | status"""
import os
import getpass
import plistlib
import re
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
UNITS = {"s": 1, "m": 60, "h": 3600}


def seconds(interval: str) -> int:
    m = re.fullmatch(r"(\d+)([smh])", str(interval))
    if not m:
        raise SystemExit(f"bad schedule interval: {interval!r} (use 10m, 1h, 30s)")
    return int(m[1]) * UNITS[m[2]]


def render(config: dict, root: Path = ROOT) -> dict:
    local = config["targets"]["local"]
    sched = local["schedule"]
    loop_dir = root / local.get("run_dir", ".factory/runs") / "loop"
    log = str(loop_dir / "loop.log")
    cycle = ["/usr/bin/lockf", "-t", "0", str(loop_dir / "loop.lock"),
             "uv", "run", "factory/drive.py", "loop", "--once"]
    return {
        "Label": sched["label"],
        "ProgramArguments": wrap(cycle, sched.get("env_from_keychain") or {}),
        "WorkingDirectory": str(root),
        "StartInterval": seconds(sched["interval"]),
        "EnvironmentVariables": {"PATH": os.path.expandvars(sched["path"])},
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def wrap(cycle: list[str], secrets: dict) -> list[str]:
    """The cycle as is, or a /bin/sh script that exports each Keychain secret first."""
    if not secrets:
        return cycle
    lines = []
    for var, service in secrets.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(var)):
            raise SystemExit(f"bad env_from_keychain variable: {var!r}")
        svc = shlex.quote(str(service))
        fail = shlex.quote(f"keychain lookup failed: {var} (service {service})")
        lines.append(f'{var}=$(security find-generic-password -s {svc} -a "$(id -un)" -w 2>/dev/null)'
                     f" || {{ echo {fail}; exit 1; }}; export {var}")
    return ["/bin/sh", "-c", "\n".join([*lines, "exec " + shlex.join(cycle)])]


def keychain_has(service: str) -> bool:
    return subprocess.run(["security", "find-generic-password", "-s", service, "-a", getpass.getuser()],
                          capture_output=True).returncode == 0


def plist_path(label: str) -> Path:
    return Path.home() / "Library/LaunchAgents" / f"{label}.plist"


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] not in ("install", "uninstall", "status"):
        print(__doc__.split("CLI: ")[1])
        return 2
    config = yaml.safe_load((ROOT / "factory.yaml").read_text())
    job = render(config)
    label, path = job["Label"], plist_path(job["Label"])
    target = f"gui/{os.getuid()}"
    if argv[0] == "install":
        Path(job["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(plistlib.dumps(job))
        launchctl("bootout", f"{target}/{label}")
        r = launchctl("bootstrap", target, str(path))
        if r.returncode:
            print(r.stderr.strip())
            return 1
        print(f"installed {label} every {config['targets']['local']['schedule']['interval']}")
    elif argv[0] == "uninstall":
        launchctl("bootout", f"{target}/{label}")
        path.unlink(missing_ok=True)
        print(f"removed {label}")
    else:
        loaded = launchctl("print", f"{target}/{label}").returncode == 0
        print(f"{label}: {'loaded' if loaded else 'not loaded'}")
        log = Path(job["StandardOutPath"])
        lines = log.read_text().splitlines() if log.exists() else []
        print("last cycle line: " + (lines[-1] if lines else "none"))
        for var, service in (config["targets"]["local"]["schedule"].get("env_from_keychain") or {}).items():
            print(f"keychain {var} ({service}): {'yes' if keychain_has(str(service)) else 'no'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

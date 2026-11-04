#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The local target (ADR-0017): the factory's nodes on a laptop, with the same workflow, roles and gate as Air.

Usage: factory deploy --target local [--apply]
Checks the `targets.local.harness` CLI on PATH, a `gh` login, the factory repository checkout and a checkout of
every descriptor repository at `<targets.local.workspace>/<name>` whose `origin` is that repository. It names each
missing item and exits 1. `--apply` records the result in `.factory/local.json` for `factory run --local`.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod  # noqa: E402
import deploy as deploy_mod  # noqa: E402
import nodes as nodes_mod  # noqa: E402

HARNESS_CLI = {"claude-code": "claude", "codex": "codex", "pi": "pi"}
STATE = Path(".factory/local.json")


def plan_local(nodes, config):
    """Each automation the local loop serves, named and triggered as on Air; scheduled and manual-only nodes are not."""
    out = []
    for name, n in nodes["nodes"].items():
        if n.get("enabled") is False or n["role"] == "human" or n.get("runner"):
            continue
        events = [t for t in n["triggers"] if not str(t).startswith("schedule ") and str(t) != "manual"]
        for automation, tier_arg, tier, focus in deploy_mod.variants(name, n, config["roles"]):
            if events:
                out.append({"automation": automation, "node": name, "tier": tier, "focus": focus,
                            "triggers": [deploy_mod.trigger(t, tier_arg, None, focus) for t in events],
                            "output": n.get("output", "read-only")})
    return out


def same_repository(url, origin):
    """True when a git `origin` (https or ssh) names the repository at `url`."""
    norm = lambda u: re.sub(r"(\.git)?/*$", "", re.sub(r"^git@([^:]+):", r"https://\1/", u.strip())).lower()  # noqa: E731
    return norm(url) == norm(origin)


def checks(config, run=subprocess.run, which=shutil.which):
    """(missing items, the state `--apply` records)."""
    local = (config.get("targets") or {}).get("local") or {}
    harness = local.get("harness") or "claude-code"
    team = config_mod.team_root()
    workspace = Path(os.path.expanduser(local.get("workspace") or str(team.parent)))
    missing, login, checkouts = [], None, {}
    if not which(HARNESS_CLI[harness]):
        missing.append(f"harness: `{HARNESS_CLI[harness]}` ({harness}) is not on PATH")
    if not which("gh"):
        missing.append("gh: not on PATH")
    elif run(["gh", "auth", "status"], capture_output=True, text=True).returncode != 0:
        missing.append("gh: not logged in (run `gh auth login`)")
    else:
        login = run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True).stdout.strip() or None
    if not (team / ".git").exists():
        missing.append(f"factory repository: {team} is not a git checkout")
    for r in config["repositories"]:
        path = workspace / r["name"]
        got = run(["git", "-C", str(path), "remote", "get-url", "origin"], capture_output=True, text=True)
        if got.returncode != 0 or not same_repository(r["url"], got.stdout):
            missing.append(f"checkout: {r['name']} at {path} with origin {r['url']}")
        else:
            checkouts[r["name"]] = str(path)
    return missing, {"harness": harness, "login": login, "poll_seconds": local.get("poll_seconds", 60),
                     "factory": str(team), "workspace": str(workspace), "checkouts": checkouts}


def deploy(argv, run=subprocess.run, which=shutil.which):
    if set(argv) - {"--apply", "--dry-run"}:
        print(__doc__)
        return 2
    config = config_mod.load()
    missing, state = checks(config, run, which)
    for item in missing:
        print(f"missing {item}")
    if missing:
        return 1
    plan = plan_local(nodes_mod.load(), config)
    print(f"local: {len(plan)} automations, harness {state['harness']}, checkouts {', '.join(state['checkouts'])}")
    if "--apply" in argv:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({**state, "automations": [p["automation"] for p in plan]}, indent=2) + "\n")
        print(f"wrote {STATE}")
    return 0


if __name__ == "__main__":
    sys.exit(deploy(config_mod.pop_team(sys.argv[1:])))

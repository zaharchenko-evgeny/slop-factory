#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Run one agent action of the delivery machine as a `pi -p` process.

Usage: run_role.py <ticket> <action> [--step NAME]. The engine owns every path:
the factory root, the run dir (`targets.local.run_dir/<ticket>`) and the
worktree (`targets.local.worktree_dir/<ticket>`). It reads `<step>.in.md` and
writes `<step>.out.md` and `<step>.log` in the run dir. Role, mode and retry
come from the machine, the model from factory.yaml and the ticket's risk.
Intake runs in the factory root, every other action in the ticket worktree.
An empty output is logged and rerun up to the action's `retry`; exit 3 then
means `<action>.error`. The summary input gets each PR's body and CI checks,
fetched read-only with `gh pr view`.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

import sm

TOOLS = {"router": "read,grep,find,ls", "developer": "read,bash,edit,write,grep,find,ls",
         "reviewer": "read,bash,grep,find,ls"}


def pr_data(urls):
    gh = os.environ.get("FACTORY_GH", "gh")
    parts = []
    for url in urls:
        r = subprocess.run([gh, "pr", "view", url, "--json", "url,title,state,body,statusCheckRollup"],
                           capture_output=True, text=True)
        parts.append(r.stdout.strip() if r.returncode == 0 else f"gh pr view {url} failed: {r.stderr.strip()}")
    return "\n\n## PR data (gh pr view, read-only)\n\n```json\n" + "\n".join(parts) + "\n```\n"


def run(root, ticket, action, step=None):
    root = Path(root).resolve()
    config = yaml.safe_load((root / "factory.yaml").read_text())
    m = sm.load_machine(root / "workflows/delivery.sm.yaml", root / "factory.yaml")
    spec, local = m["actions"][action], config["targets"]["local"]
    role, step = spec["role"], step or action
    run_dir = root / local["run_dir"] / str(ticket)
    state_file = run_dir / "sm.json"
    tvars = json.loads(state_file.read_text())["vars"] if state_file.exists() else {}
    rcfg = config["roles"][role]
    tier = rcfg.get("tier_high_risk") if spec.get("tier_by") == "risk" and tvars.get("risk") == "high" else None
    model = config["models"][tier or rcfg["tier"]]
    session = (["--session-id", f"factory-{ticket}-{role}"] if role == "developer" else ["--no-session"])
    cwd = root if action == "intake" else root / local["worktree_dir"] / str(ticket)
    if not cwd.is_dir():
        raise SystemExit(f"run_role.py: {action} needs the worktree {cwd}")
    prompt = (run_dir / f"{step}.in.md").read_text()
    if action == "summary" and tvars.get("pr_urls"):
        prompt += pr_data(tvars["pr_urls"])
    cmd = [os.environ.get("FACTORY_AGENT", "pi"), "-p", "--no-mcp", "--model", model, *session,
           "--tools", TOOLS[role], "--append-system-prompt", str(root / "roles/_common.md"),
           "--append-system-prompt", str(root / "roles" / role / "role.md"), prompt]
    out, log = run_dir / f"{step}.out.md", run_dir / f"{step}.log"
    log.write_text("")
    retry = spec.get("retry", 0)
    for attempt in range(retry + 1):
        with out.open("w") as o, log.open("a") as e:
            subprocess.run(cmd, cwd=cwd, stdout=o, stderr=e)
        if out.read_text().strip():
            return 0
        with log.open("a") as e:
            e.write(f"factory: empty output from {action} (attempt {attempt + 1}/{retry + 1})"
                    f"{'; retrying' if attempt < retry else '; giving up'}\n")
    return 3


def main(argv=None):
    p = argparse.ArgumentParser(prog="run_role.py", description=__doc__.splitlines()[0])
    p.add_argument("ticket")
    p.add_argument("action", help="an agent action of workflows/delivery.sm.yaml")
    p.add_argument("--step", help="file stem in the run dir, e.g. review-2 (default: the action)")
    args = p.parse_args(argv)
    return run(sm.ROOT, args.ticket, args.action, args.step)


if __name__ == "__main__":
    sys.exit(main())

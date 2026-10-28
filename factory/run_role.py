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
come from the machine, the model from factory.yaml (`roles.<role>.modes.<mode>`
when the mode has an entry, as each reviewer focus does) and the ticket's risk.
Each reviewer is its own action, so several reviewers run as separate calls.
Intake runs in the factory root, every other action in the ticket worktree.
The output must end with the handoff: the last fenced block, with nothing
after it. Each call runs the model once: an empty output or a missing or
trailing-text handoff is logged and the call exits 3 (`<action>.error`); the
engine's `retry` of the action is the only rerun. If the output was empty and
the mode has a `fallback` tier, the same call runs once more on the fallback
model; the switch is logged and kept in the handoff as `fallback_from`. A valid
handoff is also written as `<step>.handoff.json` (`handoff` = its first line,
then each top-level `key: value`; a key with an indented block gets the block
text). Every input gets the ticket re-read with `gh issue view --json
title,body,comments,labels` (comments starting `[factory] Moved to #` left
out), or the text the caller passes (`ticket_text`, the local tracker's file);
the summary input also gets each PR's body and CI checks, fetched read-only
with `gh pr view`.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml

import sm

TOOLS = {"router": "read,grep,find,ls", "developer": "read,bash,edit,write,grep,find,ls",
         "reviewer": "read,bash,grep,find,ls"}


class HandoffError(ValueError):
    pass


def parse_handoff(text):
    """The trailing fenced block as a dict; HandoffError when it is missing or text follows it."""
    lines = text.rstrip().splitlines()
    if not lines:
        raise HandoffError("empty output")
    if not lines[-1].startswith("```"):
        raise HandoffError("text after the last fenced block" if any(x.startswith("```") for x in lines)
                           else "no fenced handoff block")
    start = next((i for i in range(len(lines) - 2, -1, -1) if lines[i].startswith("```")), None)
    body = [x for x in lines[start + 1:-1] if x.strip()] if start is not None else []
    if not body or not re.fullmatch(r"[A-Z][A-Z-]*", body[0].strip()):
        raise HandoffError("the last fenced block does not start with a handoff name like REVIEWER")
    out, key, blocks = {"handoff": body[0].strip()}, None, {}
    for line in body[1:]:
        m = re.match(r"([a-z_]+):(?:\s+(.*))?$", line)
        if m:
            key, value = m.group(1), (m.group(2) or "").strip()
            out[key] = "" if value == "|" else value
        elif key and (line[:1].isspace() or line.startswith("- ")):
            blocks.setdefault(key, []).append(line)
        else:
            raise HandoffError(f"malformed handoff line {line!r}")
    for k, block in blocks.items():
        out[k] = textwrap.dedent("\n".join(block))
    return out


def pr_data(urls):
    gh = os.environ.get("FACTORY_GH", "gh")
    parts = []
    for url in urls:
        r = subprocess.run([gh, "pr", "view", url, "--json", "url,title,state,body,statusCheckRollup"],
                           capture_output=True, text=True)
        parts.append(r.stdout.strip() if r.returncode == 0 else f"gh pr view {url} failed: {r.stderr.strip()}")
    return "\n\n## PR data (gh pr view, read-only)\n\n```json\n" + "\n".join(parts) + "\n```\n"


MOVED = "[factory] Moved to #"


def ticket_data(repo, n):
    """The ticket as it is now (`gh issue view`, read-only), re-read before every role input so the
    stage labels are current. Comments marked as moved to a split slice are left out."""
    gh = os.environ.get("FACTORY_GH", "gh")
    r = subprocess.run([gh, "issue", "view", str(n), "-R", repo, "--json", "title,body,comments,labels"],
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"run_role.py: gh issue view {n} failed: {r.stderr.strip()}")
    t = json.loads(r.stdout)
    t["comments"] = [c for c in t.get("comments") or [] if not (c.get("body") or "").startswith(MOVED)]
    return "\n\n## Ticket (gh issue view, read just before this step)\n\n```json\n" + json.dumps(t, indent=2) + "\n```\n"


def run(root, ticket, action, step=None, ticket_text=None):
    root = Path(root).resolve()
    config = yaml.safe_load((root / "factory.yaml").read_text())
    m = sm.load_machine(root / "workflows/delivery.sm.yaml", root / "factory.yaml")
    spec, local = m["actions"][action], config["targets"]["local"]
    role, step = spec["role"], step or action
    run_dir = root / local["run_dir"] / str(ticket)
    state_file = run_dir / "sm.json"
    tvars = json.loads(state_file.read_text())["vars"] if state_file.exists() else {}
    rcfg = config["roles"][role]
    rcfg = (rcfg.get("modes") or {}).get(spec.get("mode")) or rcfg
    tier = rcfg.get("tier_high_risk") if spec.get("tier_by") == "risk" and tvars.get("risk") == "high" else None
    model = config["models"][tier or rcfg["tier"]]
    fallback = config["models"][rcfg["fallback"]] if rcfg.get("fallback") else None
    session = (["--session-id", f"factory-{ticket}-{role}"] if role == "developer" else ["--no-session"])
    cwd = root if action == "intake" else root / local["worktree_dir"] / str(ticket)
    if not cwd.is_dir():
        raise SystemExit(f"run_role.py: {action} needs the worktree {cwd}")
    prompt = (run_dir / f"{step}.in.md").read_text() + (
        ticket_data(config["tracker"]["repository"], ticket) if ticket_text is None else
        f"\n\n## Ticket (local tracker file, read just before this step)\n\n~~~~markdown\n{ticket_text}\n~~~~\n")
    if action == "summary" and tvars.get("pr_urls"):
        prompt += pr_data(tvars["pr_urls"])
    out, log = run_dir / f"{step}.out.md", run_dir / f"{step}.log"
    err = None
    for i, use in enumerate((model, fallback)):
        if use is None or (i and str(err) != "empty output"):
            break
        if i:
            with log.open("a") as e:
                e.write(f"factory: {action} falls back from {model} to {fallback}\n")
        cmd = [os.environ.get("FACTORY_AGENT", "pi"), "-p", "--no-mcp", "--model", use, *session,
               "--tools", TOOLS[role], "--append-system-prompt", str(root / "roles/_common.md"),
               "--append-system-prompt", str(root / "roles" / role / "role.md"), prompt]
        with out.open("w") as o, log.open("a") as e:
            subprocess.run(cmd, cwd=cwd, stdout=o, stderr=e)
        try:
            handoff = parse_handoff(out.read_text())
        except HandoffError as e_:
            err = e_
            with log.open("a") as e:
                e.write(f"factory: {err} from {action} on {use}\n")
            continue
        if i:
            handoff["fallback_from"] = model
        handoff["model"] = use
        (run_dir / f"{step}.handoff.json").write_text(json.dumps(handoff, indent=2))
        return 0
    return 3


def main(argv=None):
    p = argparse.ArgumentParser(prog="run_role.py", description=__doc__.splitlines()[0])
    p.add_argument("ticket")
    p.add_argument("action", help="an agent action of workflows/delivery.sm.yaml")
    p.add_argument("--step", help="file stem in the run dir, e.g. review_full-2 (default: the action)")
    args = p.parse_args(argv)
    return run(sm.ROOT, args.ticket, args.action, args.step)


if __name__ == "__main__":
    sys.exit(main())

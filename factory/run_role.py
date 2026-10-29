#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Run one agent action of the delivery machine as a process of its role's harness (factory/harnesses.py).

Usage: run_role.py <ticket> <action> [--step NAME]. The engine owns every path:
the factory root, the run dir (`targets.local.run_dir/<ticket>`) and the
worktree (`targets.local.worktree_dir/<ticket>`). It reads `<step>.in.md` and
writes `<step>.out.md` and `<step>.log` in the run dir. Role, mode and retry
come from the machine, the model from factory.yaml (`roles.<role>.modes.<mode>`
when the mode has an entry, as each reviewer focus does) and the ticket's risk.
Each reviewer is its own action, so several reviewers run as separate calls.
An action with `cwd: root` (intake, diagnose) runs in the factory root, every other
action in the ticket worktree.
The output must end with the handoff: the last fenced block, or a final
unfenced block starting with its header line (`REVIEWER`), with nothing after
it. Each attempt's output is kept as `<step>-attempt<k>.out.md`. Each call runs the model once: an empty output or a missing or
trailing-text handoff (or a bad first line) is logged; when the mode has a
`fallback` tier the same call runs once more on the fallback model (the switch
is logged and kept in the handoff as `fallback_from`), else the call exits 3
(`<action>.error`) and the engine's `retry` of the action is the only rerun.
A reviewer runs in a private temporary dir outside the factory root: its own
run dir (copies of its role files, out and log, copied back to the shared run
dir afterwards) and a detached checkout of the worktree's HEAD, recorded as the
handoff's `reviewed_sha`; it sees only its own input. A valid
handoff is also written as `<step>.handoff.json` (`handoff` = its first line,
then each top-level `key: value`; a key with an indented block gets the block
text). Every input gets the ticket re-read with `gh issue view --json
title,body,comments,labels` (comments starting `[factory] Moved to #` left
out), or the text the caller passes (`ticket_text`, the local tracker's file), or nothing for an
action with `ticket: none` (distill, which runs in `<worktree_dir>/distill`);
the summary input also gets each PR's body and CI checks, fetched read-only
with `gh pr view`.
Every pi call (the default harness) gets the environment of factory.yaml `targets.local.model_access` (factory/model_access.py).
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import yaml

import harnesses
import model_access
import sm

TOOLS = {"router": "read,grep,find,ls", "developer": "read,bash,edit,write,grep,find,ls",
         "reviewer": "read,bash,grep,find,ls", "doc-writer": "read,bash,edit,write,grep,find,ls",
         "retro": "read,grep,find,ls", "distill": "read,edit,write,grep,find,ls"}


class HandoffError(ValueError):
    pass


HEADER = re.compile(r"[A-Z][A-Z-]*")
KNOWN = re.compile(r"DEVELOPER|DISTILL|DOC-WRITER|RETRO|REVIEWER|ROUTER-[A-Z]+")


def parse_handoff(text):
    """The trailing handoff as a dict: the last fenced block, or the final unfenced block that starts
    with its header line (`REVIEWER`); HandoffError when it is missing or text follows it."""
    lines = text.rstrip().splitlines()
    if not lines:
        raise HandoffError("empty output")
    if lines[-1].startswith("```"):
        start = next((i for i in range(len(lines) - 2, -1, -1) if lines[i].startswith("```")), None)
        body = [x for x in lines[start + 1:-1] if x.strip()] if start is not None else []
    else:  # unfenced: from the first known header line (column 0) to the end; any later line that is
        # not a `key:` line, an indented continuation or a `- ` item, or any fence, fails below
        start = next((i for i, x in enumerate(lines) if KNOWN.fullmatch(x)), None)
        if start is None or start == len(lines) - 1 or any(x.startswith("```") for x in lines[start:]):
            raise HandoffError("text after the last fenced block" if any(x.startswith("```") for x in lines)
                               else "no handoff block")
        body = [x for x in lines[start:] if x.strip()]
    if not body or not HEADER.fullmatch(body[0].strip()):
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


def parse_pi_output(raw):
    return harnesses.ADAPTERS["pi"].parse(raw)


def skill_dirs(root, role):
    """The dir of each name in the role's skills.txt, found where setup.sh looks."""
    dirs = []
    for name in (root / "roles" / role / "skills.txt").read_text().split():
        for d in (root / ".agents/skills", Path.home() / ".agents/skills", Path.home() / ".pi/agent/skills",
                  Path.home() / ".pi/agent/npm/node_modules"):
            hit = next(d.glob(f"**/{name}/SKILL.md"), None) if d.is_dir() else None
            if hit:
                dirs.append(hit.parent)
                break
    return dirs


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
    harness = harnesses.harness_of(config, role, spec.get("mode"))
    ad = harnesses.adapter(harness)
    cwd = root if spec.get("cwd") == "root" else root / local["worktree_dir"] / str(ticket)
    if not cwd.is_dir():
        raise SystemExit(f"run_role.py: {action} needs the worktree {cwd}")
    prompt = (run_dir / f"{step}.in.md").read_text() + ("" if spec.get("ticket") == "none" else
        ticket_data(config["tracker"]["repository"], ticket) if ticket_text is None else
        f"\n\n## Ticket (local tracker file, read just before this step)\n\n~~~~markdown\n{ticket_text}\n~~~~\n")
    if action == "summary" and tvars.get("pr_urls"):
        prompt += pr_data(tvars["pr_urls"])
    env = None
    if harness == "pi":  # model_access only reroutes pi's Claude provider
        try:
            env = {**os.environ, **model_access.pi_env(config, root)}
        except model_access.ModelAccessError as e:
            raise SystemExit(f"run_role.py: model access: {e}")
    if role != "reviewer":
        return attempts(root, run_dir, cwd, step, action, role, (model, fallback), prompt, ticket, {}, adapter=ad, env=env)
    # Each reviewer gets a private dir outside the factory root: its own run dir (role files, out,
    # log) and a detached checkout of the reviewed SHA, so parallel reviewers never share a path.
    # shortcut: no OS sandbox, a reviewer with bash can still read any absolute path; add one if
    # reviewers start reaching outside their dir.
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True,
                         check=True).stdout.strip()
    private = Path(tempfile.mkdtemp(prefix=f"factory-{ticket}-{step}-")).resolve()
    checkout = private / "checkout"
    try:
        subprocess.run(["git", "worktree", "add", "-q", "--detach", str(checkout), sha], cwd=cwd,
                       capture_output=True, text=True, check=True)
        own = private / "run"
        own.mkdir()
        code = attempts(root, own, checkout, step, action, role, (model, fallback), prompt, ticket,
                        {"reviewed_sha": sha}, adapter=ad, keep_dir=run_dir, events=run_dir, env=env)
        for f in own.glob(f"{step}.*"):
            shutil.copy(f, run_dir / f.name)
        return code
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(checkout)], cwd=cwd, capture_output=True)
        shutil.rmtree(private, ignore_errors=True)


def attempts(root, run_dir, cwd, step, action, role, models, prompt, ticket, extra, adapter=None, keep_dir=None,
             events=None, env=None):
    """Run the model, then the fallback (if any) after any malformed handoff; write the handoff.
    Each attempt's output is also kept as `<step>-attempt<k>.out.md` in `keep_dir` (default: run_dir),
    numbered across fallbacks and the engine's retries."""
    out, log = run_dir / f"{step}.out.md", run_dir / f"{step}.log"
    prompts = [root / "roles/_common.md", root / "roles" / role / "role.md"]
    if role == "reviewer":  # private copies: the reviewer gets no path into the factory root
        prompts = [Path(shutil.copy(f, run_dir / f"{i}-{f.name}")) for i, f in enumerate(prompts)]
    model = models[0]
    for i, use in enumerate(models):
        if use is None:
            break
        if i:
            with log.open("a") as e:
                e.write(f"factory: {action} falls back from {model} to {use}\n")
        call = harnesses.Call(str(ticket), role, use, TOOLS[role], prompt, prompts, skill_dirs(root, role), run_dir, cwd)
        with log.open("a") as e:
            raw = subprocess.run(adapter.command(call), cwd=cwd, stdout=subprocess.PIPE, stderr=e, text=True,
                                 env=env).stdout
        text, usage = adapter.parse(raw, call)
        out.write_text(text)
        if usage:
            with (events or run_dir).joinpath("events.log").open("a") as ev:  # one short append per call
                ev.write(json.dumps({"event": "usage", "step": step, "role": role, "model": use, **usage}) + "\n")
        keep = keep_dir or run_dir
        k = 1 + len(list(keep.glob(f"{step}-attempt[0-9]*.out.md")))
        shutil.copy(out, keep / f"{step}-attempt{k}.out.md")
        try:
            handoff = parse_handoff(out.read_text())
        except HandoffError as err:
            with log.open("a") as e:
                e.write(f"factory: {err} from {action} on {use}\n")
            continue
        if i:
            handoff["fallback_from"] = model
        handoff.update(extra, model=use)
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

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
The handoff is JSON validated against `roles/<role>/handoff.schema.json`: a role with a write tool writes
it to the file named by $FACTORY_HANDOFF (`<step>.handoff-draft.json`, the path is also in its prompt), a read-only
role ends its output with it; prose around it is ignored. An invalid or missing one is rerun once with the error in the
prompt, then the fallback. The legacy text handoff stays accepted for one release: the last fenced block, or a final
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
import time
from pathlib import Path

import yaml

import harnesses
import model_access
import runlog
import sm_v1 as sm

TOOLS = {"router": "read,grep,find,ls", "developer": "read,bash,edit,write,grep,find,ls",
         "reviewer": "read,bash,grep,find,ls", "doc-writer": "read,bash,edit,write,grep,find,ls",
         "retro": "read,grep,find,ls", "distill": "read,edit,write,grep,find,ls"}


class HandoffError(ValueError):
    pass


class InvalidHandoff(HandoffError):
    """A JSON handoff that is present but fails its schema."""


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


WRITERS = ("developer", "doc-writer", "distill")  # roles with a write tool: they write $FACTORY_HANDOFF
TYPES = {"string": str, "array": list, "object": dict}


def json_objects(text):
    """Every top-level JSON object in `text`, prose and fences around them skipped."""
    dec, i, out = json.JSONDecoder(), 0, []
    while (i := text.find("{", i)) != -1:
        try:
            obj, i = dec.raw_decode(text, i)
            out.append(obj)
        except ValueError:
            i += 1
    return out


def validate(obj, schema):
    """Errors of `obj` against the subset of JSON Schema the handoff schemas use: required, enum and
    type (a name or a list of names) per property."""
    errors = [f"missing field {k!r}" for k in schema.get("required", []) if k not in obj]
    for k, p in schema.get("properties", {}).items():
        if k not in obj:
            continue
        types = [p["type"]] if isinstance(p.get("type"), str) else p.get("type", [])
        if "enum" in p and obj[k] not in p["enum"]:
            errors.append(f"field {k!r} must be one of {', '.join(p['enum'])}, not {obj[k]!r}")
        elif types and not any(isinstance(obj[k], TYPES[t]) for t in types):
            errors.append(f"field {k!r} must be of type {' or '.join(types)}")
    return errors


def flat(v):
    """A JSON value as the text the key: value format gives (a list is `- ` lines, a dict one item)."""
    if not isinstance(v, list):
        return str(v)
    def item(x):
        if not isinstance(x, dict):
            return f"- {x}"
        rows = [f"{k}: {flat(y)}" for k, y in x.items()]
        return "\n".join(f"{'- ' if i == 0 else '  '}{r}" for i, r in enumerate(rows))
    return "\n".join(item(x) for x in v)


def json_handoff(role, root, text, file_text=None):
    """None when the output has no JSON handoff (the legacy text format is tried), else the handoff dict;
    HandoffError with the validation errors when it is invalid. The file wins over the output text."""
    found = [o for o in json_objects(file_text if file_text is not None else text) if isinstance(o, dict)]
    found = [o for o in found if "handoff" in o]
    if not found:
        if file_text is None:
            return None
        raise InvalidHandoff("the handoff file holds no JSON object with a `handoff` field")
    obj = found[-1]
    schemas = json.loads((root / "roles" / role / "handoff.schema.json").read_text())
    if obj["handoff"] not in schemas:
        raise InvalidHandoff(f"unknown handoff {obj['handoff']!r}, one of {', '.join(schemas)}")
    errors = validate(obj, schemas[obj["handoff"]])
    if errors:
        raise InvalidHandoff("invalid JSON handoff: " + "; ".join(errors))
    return {k: v if k == "handoff" else flat(v) for k, v in obj.items()}


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
    m = sm.load_machine(root / "workflows/delivery.v1.sm.yaml", root / "factory.yaml")
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
    rid = tvars.get("run_id") or ""
    if role != "reviewer":
        return attempts(root, run_dir, cwd, step, action, role, (model, fallback), prompt, ticket, {}, adapter=ad, env=env,
                        rid=rid)
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
                        {"reviewed_sha": sha}, adapter=ad, keep_dir=run_dir, events=run_dir, env=env, rid=rid)
        for f in own.glob(f"{step}.*"):
            shutil.copy(f, run_dir / f.name)
        return code
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(checkout)], cwd=cwd, capture_output=True)
        shutil.rmtree(private, ignore_errors=True)


def attempts(root, run_dir, cwd, step, action, role, models, prompt, ticket, extra, adapter=None, keep_dir=None,
             events=None, env=None, rid=""):
    """Run the model, then the fallback (if any) after any malformed handoff; write the handoff.
    Each attempt's output is also kept as `<step>-attempt<k>.out.md` in `keep_dir` (default: run_dir),
    numbered across fallbacks and the engine's retries."""
    out, log = run_dir / f"{step}.out.md", run_dir / f"{step}.log"
    prompts = [root / "roles/_common.md", root / "roles" / role / "role.md"]
    if role == "reviewer":  # private copies: the reviewer gets no path into the factory root
        prompts = [Path(shutil.copy(f, run_dir / f"{i}-{f.name}")) for i, f in enumerate(prompts)]
    model = models[0]
    hfile = run_dir / f"{step}.handoff-draft.json"
    extra_env = {"FACTORY_HANDOFF": str(hfile)} if role in WRITERS else {}
    note = (f"\n\n## Handoff\n\nWrite the handoff as one JSON object (schema in your role file) to {hfile} with "
            "your write tool, as the last thing you do.\n" if extra_env else
            "\n\n## Handoff\n\nEnd your reply with the handoff as one JSON object (schema in your role file).\n")
    evlog = (events or run_dir) / "events.log"
    for i, use in enumerate(models):
        if use is None:
            break
        if i:
            runlog.emit(evlog, rid, "fallback", step=step, role=role, **{"from": model, "to": use})
            with log.open("a") as e:
                e.write(f"factory: {action} falls back from {model} to {use}\n")
        retry_note, handoff = "", None
        for _ in range(2):  # a JSON handoff that fails its schema gets one retry with the error in the prompt
            call = harnesses.Call(str(ticket), role, use, TOOLS[role], prompt + note + retry_note, prompts,
                                  skill_dirs(root, role), run_dir, cwd)
            keep = keep_dir or run_dir
            k = 1 + len(list(keep.glob(f"{step}-attempt[0-9]*.out.md")))
            runlog.emit(evlog, rid, "step.start", step=step, role=role, model=use, attempt=k)
            t0 = time.monotonic()
            hfile.unlink(missing_ok=True)
            with log.open("a") as e:
                raw = subprocess.run(adapter.command(call), cwd=cwd, stdout=subprocess.PIPE, stderr=e, text=True,
                                     env={**(env or os.environ), **extra_env} if extra_env else env).stdout
            text, usage = adapter.parse(raw, call)
            out.write_text(text)
            if usage:
                runlog.emit(evlog, rid, "usage", step=step, role=role, model=use, **usage)
            shutil.copy(out, keep / f"{step}-attempt{k}.out.md")
            try:
                handoff = json_handoff(role, root, text, hfile.read_text() if hfile.exists() else None)
                if handoff is None:  # legacy fenced text, accepted for one release
                    handoff = parse_handoff(out.read_text())
            except HandoffError as err:
                with log.open("a") as e:
                    e.write(f"factory: {err} from {action} on {use}\n")
                runlog.emit(evlog, rid, "step.end", step=step, role=role, model=use, attempt=k,
                            duration_s=round(time.monotonic() - t0, 1), outcome="empty" if not text.strip() else "bad-handoff")
                retry_note = f"\n\n## Your previous handoff was rejected\n\n{err}. Fix it and write the handoff again.\n"
                continue
            break
        else:
            handoff = None
        if handoff is None:
            continue
        runlog.emit(evlog, rid, "step.end", step=step, role=role, model=use, attempt=k,
                    duration_s=round(time.monotonic() - t0, 1), outcome="ok")
        if i:
            handoff["fallback_from"] = model
        handoff.update(extra, model=use)
        (run_dir / f"{step}.handoff.json").write_text(json.dumps(handoff, indent=2))
        return 0
    return 3


def main(argv=None):
    p = argparse.ArgumentParser(prog="run_role.py", description=__doc__.splitlines()[0])
    p.add_argument("ticket")
    p.add_argument("action", help="an agent action of workflows/delivery.v1.sm.yaml")
    p.add_argument("--step", help="file stem in the run dir, e.g. review_full-2 (default: the action)")
    args = p.parse_args(argv)
    return run(sm.ROOT, args.ticket, args.action, args.step)


if __name__ == "__main__":
    sys.exit(main())

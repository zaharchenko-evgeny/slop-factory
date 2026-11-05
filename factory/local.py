#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The local target (ADR-0017): the factory's nodes on a laptop, with the same workflow, roles and gate as Air.

Usage: factory deploy --target local [--apply]
       factory run --local [--once] [--dry-run]
`deploy` checks the `targets.local.harness` CLI and `factory` on PATH, a `gh` login, the factory repository checkout
and a checkout of every descriptor repository at `<targets.local.workspace>/<name>` whose `origin` is that
repository. It names each missing item and exits 1. `--apply` records the result in `.factory/local.json`.
`run` does Air's part for each new event a node trigger names: a worktree per repository in
`.factory/worktrees/<n>/<repo>`, `factory begin` there (exit 10: nothing runs), the harness with the node's Air prompt
(the agent runs `factory begin` and `factory end` itself), then for a write node the push of its `ez/` branch and,
for `create-pr`, `gh pr create`. Script nodes run `factory sweep --event`. Seen events are kept in
`.factory/local/seen.json`; `--dry-run` lists the events without running or recording them.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod  # noqa: E402
import deploy as deploy_mod  # noqa: E402
import nodes as nodes_mod  # noqa: E402
from gate_port import ticket_from_ref  # noqa: E402

HARNESS_CLI = {"claude-code": "claude", "codex": "codex", "junie": "junie", "pi": "pi"}


def role_harness(config, role, focus=None):
    """The harness a role runs in on the local target: its mode's, else the role's, else `targets.local.harness`.
    Air ignores it: there the platform runs the agent with `targets.air.models`."""
    r = (config.get("roles") or {}).get(role) or {}
    mode = ((r.get("modes") or {}).get(focus) or {}) if focus else {}
    local = (config.get("targets") or {}).get("local") or {}
    return mode.get("harness") or r.get("harness") or local.get("harness") or "claude-code"


def local_models(config):
    """Tier -> model on the local target: `targets.local.models` (the older top-level `models` still works)."""
    return ((config.get("targets") or {}).get("local") or {}).get("models") or config.get("models") or {}
STATE, SEEN, WORKTREES = Path(".factory/local.json"), Path(".factory/local/seen.json"), Path(".factory/worktrees")
TRIES_FILE = Path(".factory/local/tries.json")  # failed runs per event that are tried again
FACTORY = ["uv", "run", "--quiet", str(Path(__file__).resolve().parent / "cli.py")]
WRITE_OUTPUTS = ("create-pr", "push-to-source")


def plan_local(nodes, config):
    """Each automation the local loop serves, named and triggered as on Air; scheduled and manual-only nodes are not."""
    out = []
    for name, n in nodes["nodes"].items():
        if n.get("enabled") is False or n["role"] == "human" or n.get("runner"):
            continue
        events = [t for t in n["triggers"] if not str(t).startswith("schedule ") and str(t) != "manual"]
        for automation, tier_arg, tier, focus in deploy_mod.variants(name, n, config["roles"]):
            if events:
                out.append({"automation": automation, "node": name, "role": n["role"], "tier": tier, "focus": focus,
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
    harnesses = sorted({role_harness(config, e["role"], e["focus"]) for e in plan_local(nodes_mod.load(), config)
                        if e["role"] != "script"} or {role_harness(config, None)})
    team = config_mod.team_root()
    workspace = Path(os.path.expanduser(local.get("workspace") or str(team.parent)))
    missing, login, checkouts = [], None, {}
    for harness in harnesses:
        if not which(HARNESS_CLI[harness]):
            missing.append(f"harness: `{HARNESS_CLI[harness]}` ({harness}) is not on PATH")
    if not which("factory"):
        missing.append("factory: not on PATH (the agent runs `factory begin` and `factory end`; `uv tool install` the engine)")
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
    return missing, {"harnesses": harnesses, "login": login, "poll_seconds": local.get("poll_seconds", 60),
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
    print(f"local: {len(plan)} automations, harnesses {', '.join(state['harnesses'])}, checkouts {', '.join(state['checkouts'])}")
    if "--apply" in argv:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({**state, "automations": [p["automation"] for p in plan]}, indent=2) + "\n")
        print(f"wrote {STATE}")
    return 0


def sh(run, argv, **kw):
    return run(argv, capture_output=True, text=True, **kw)


def lines(run, *args):
    done = sh(run, ["gh", *args])
    if done.returncode:
        raise RuntimeError(f"gh {' '.join(args)}: {done.stderr.strip()}")
    return [json.loads(x) for x in done.stdout.splitlines() if x.strip()]


def poll(plan, repo, run):
    """(event id, plan entry, Air-like trigger payload) for each event the plan's triggers name, from GitHub reads.
    A label event's id is GitHub's id of its latest `labeled` event, so a label added again is a new event."""
    events, cache = [], {}

    def read(*args):
        if args not in cache:
            cache[args] = lines(run, *args)
        return cache[args]
    for entry in plan:
        for t in entry["triggers"]:
            kind, label = t["eventType"], t["filters"].get("label")
            if kind in ("issues.labeled", "pull_request.labeled") and label:
                what = "issue" if kind.startswith("issues") else "pr"
                for item in read(what, "list", "-R", repo, "--label", label, "--state", "open", "--json",
                                 "number,headRefName" if what == "pr" else "number", "--jq", ".[]"):
                    n = item["number"]
                    ids = [e["id"] for e in read("api", "--paginate", f"repos/{repo}/issues/{n}/events", "--jq",
                                                 '.[] | select(.event == "labeled") | {id, name: .label.name}')
                           if e["name"] == label]
                    body = {"issue": {"number": n}} if what == "issue" else {"pull_request": {"number": n, "head": {"ref": item["headRefName"]}}}
                    if ids:
                        events.append((f"labeled:{ids[-1]}", entry, {"eventType": kind, "action": "labeled", "label": {"name": label}, **body}))
            elif kind in ("pull_request.merged", "pull_request.synchronize"):
                merged = kind.endswith("merged")
                for item in read("pr", "list", "-R", repo, "--state", "merged" if merged else "open", "--limit", "20",
                                 "--json", "number,headRefName,headRefOid", "--jq", ".[]"):
                    pr = {"number": item["number"], "head": {"ref": item["headRefName"], "sha": item["headRefOid"]}}
                    events.append((f"merged:{item['number']}" if merged else f"sync:{item['number']}:{item['headRefOid']}", entry,
                                   {"eventType": kind, "action": "closed" if merged else "synchronize",
                                    "pull_request": {**pr, **({"merged": True} if merged else {})}}))
            elif kind == "issues.closed":
                for item in read("issue", "list", "-R", repo, "--state", "closed", "--limit", "20", "--json", "number,closedAt", "--jq", ".[]"):
                    events.append((f"closed:{item['number']}:{item['closedAt']}", entry,
                                   {"eventType": kind, "action": "closed", "issue": {"number": item["number"]}}))
    return events


def harness_argv(harness, model, write, prompt):
    """The headless run of a harness (skills/factory-setup/references/harnesses.md); read-only nodes get no edit tools."""
    if harness == "claude-code":
        tools = "Bash,Read,Edit,Write,Grep,Glob" if write else \
            "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git status:*),Bash(factory:*)"
        # the prompt comes first: `--allowedTools` takes every following word that is not an option
        return ["claude", "-p", prompt, "--output-format", "json", "--model", model.split("/")[-1], "--allowedTools", tools,
                *(["--permission-mode", "acceptEdits"] if write else [])]
    if harness == "codex":  # `factory begin` and `end` call GitHub, so even a reviewer's sandbox needs the network
        return ["codex", "exec", "--json", "-m", model.split("/")[-1], "--sandbox", "workspace-write",
                "-c", "sandbox_workspace_write.network_access=true", prompt]
    if harness == "junie":  # Junie keeps its terminal tools; a read-only node is held by the gate and by no push
        return ["junie", "--task", prompt, "--model", model.split("/")[-1], "--output-format", "json"]
    return ["pi", "-p", "--model", model, prompt]


def worktree(run, checkout, path, start, branch=None):
    """A worktree of `checkout` at `path` on `branch` (from `start`), or detached at `start` for a read-only node.
    Returns git's error, or "" when the worktree is ready."""
    if not path.exists():
        done = sh(run, ["git", "-C", str(checkout), "worktree", "add", "--detach", str(path), start])
        if done.returncode:
            return done.stderr.strip() or "git worktree add failed"
    done = sh(run, ["git", "-C", str(path), "checkout", "--quiet", *(["-B", branch] if branch else ["--detach"]), start])
    return done.stderr.strip() or "git checkout failed" if done.returncode else ""


def run_event(entry, payload, state, config, graph, run):
    """Air's part of one node run on this laptop; the gate decides everything else."""
    event = Path(".factory/local/events") / f"{entry['automation']}.json"
    event.parent.mkdir(parents=True, exist_ok=True)
    event.write_text(json.dumps(payload))
    team = ["--team", state["factory"]]
    node = graph["nodes"][entry["node"]]
    if node["role"] == "script":
        return "swept" if sh(run, [*FACTORY, *team, "sweep", "--event", str(event.resolve())]).returncode == 0 else "sweep failed"
    repos = {r["name"]: r for r in config["repositories"]}
    primary = next(r["name"] for r in config["repositories"] if r.get("role") == "primary")
    pr, issue = payload.get("pull_request") or {}, payload.get("issue") or {}
    ticket = ticket_from_ref((pr.get("head") or {}).get("ref")) or issue.get("number") or pr.get("number")
    trees = {primary: (WORKTREES / str(ticket) / primary).resolve()}
    sh(run, ["git", "-C", state["checkouts"][primary], "fetch", "--quiet", "origin"])
    error = worktree(run, state["checkouts"][primary], trees[primary], f"origin/{repos[primary]['default_branch']}")
    if error:
        return f"checkout failed: {error[-300:]}"
    begin = sh(run, [*FACTORY, *team, "begin", entry["automation"], str(event.resolve())], cwd=trees[primary])
    if begin.returncode == 10:
        return "no-go"
    if begin.returncode:
        return f"begin failed: {(begin.stdout + begin.stderr).strip()[-300:]}"
    out, write = json.loads(begin.stdout), entry["output"] in WRITE_OUTPUTS
    branch = out["branch"]
    for c in out.get("checkouts") or []:
        trees.setdefault(c["repo"], (WORKTREES / str(ticket) / c["repo"]).resolve())
    for name, path in trees.items():
        checkout = state["checkouts"][name]
        sh(run, ["git", "-C", checkout, "fetch", "--quiet", "origin"])
        remote = sh(run, ["git", "-C", checkout, "ls-remote", "--heads", "origin", "--", branch]).stdout.strip()
        error = worktree(run, checkout, path, f"origin/{branch}" if remote else f"origin/{repos[name]['default_branch']}",
                         branch if write else None)
        if error:
            return f"checkout failed: {error[-300:]}"
    others = "".join(f"\n- `{name}`: `{path}`" for name, path in trees.items() if name != primary)
    prompt = deploy_mod.agent_prompt(entry["node"], node, config, entry["automation"], entry["focus"]) + (
        f"\n\nThe ticket's other repositories are checked out on its branch:{others}" if others else "")
    model = local_models(config)[entry["tier"]]
    harness = role_harness(config, node["role"], entry["focus"])
    done = sh(run, harness_argv(harness, model, write, prompt), cwd=trees[primary],
              env={**os.environ, "FACTORY_TEAM": state["factory"]})
    if done.returncode:
        return f"harness failed: {(done.stdout + done.stderr).strip()[-300:]}"
    if not write:
        return "ran"
    for name, path in trees.items():
        base = repos[name]["default_branch"]
        if sh(run, ["git", "-C", str(path), "rev-list", "--count", f"origin/{base}..HEAD"]).stdout.strip() in ("", "0"):
            continue  # nothing committed in this repository
        push = sh(run, ["git", "-C", str(path), "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}"])
        if push.returncode:
            return f"push failed: {push.stderr.strip()[-300:]}"
        full = "/".join(repos[name]["url"].rstrip("/").split("/")[-2:])
        if entry["output"] == "create-pr" and not lines(run, "pr", "list", "-R", full, "--head", branch, "--state", "open",
                                                         "--json", "number", "--jq", ".[]"):
            closes = f"Closes #{ticket}" if full == config["tracker"]["repository"] else f"Refs {config['tracker']['repository']}#{ticket}"
            create = sh(run, ["gh", "pr", "create", "-R", full, "--head", branch, "--base", base,
                              "--title", f"#{ticket} {out['context'].get('title', '')}".strip(), "--body", closes])
            if create.returncode:
                return f"pr create failed: {create.stderr.strip()[-300:]}"
    return "pushed"


# Failures before any harness started are tried on the next polls, up to TRIES runs in all; a run that started
# is not repeated, as on Air (reconcile adds the label again, which is a new event).
RETRIED, TRIES = ("begin failed", "checkout failed", "sweep failed", "error"), 3


def save(path, value):
    """Write a JSON file atomically, so a crash never leaves a broken one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value))
    os.replace(tmp, path)


def main(argv, run=subprocess.run, sleep=time.sleep):
    if "--local" not in argv or set(argv) - {"--local", "--once", "--dry-run"}:
        print(__doc__)
        return 2
    if not STATE.exists():
        print("no .factory/local.json: run `factory deploy --target local --apply` first")
        return 2
    state, config, graph = json.loads(STATE.read_text()), config_mod.load(), nodes_mod.load()
    plan = plan_local(graph, config)
    seen, first = (set(json.loads(SEEN.read_text())), False) if SEEN.exists() else (set(), True)
    tries = json.loads(TRIES_FILE.read_text()) if TRIES_FILE.exists() else {}
    while True:
        try:
            events = poll(plan, config["tracker"]["repository"], run)
        except (RuntimeError, ValueError) as e:  # GitHub unreachable or rate limited: try again next poll
            print(json.dumps({"poll failed": str(e)[-300:]}), flush=True)
            if "--once" in argv:
                return 1
            sleep(state.get("poll_seconds", 60))
            continue
        for event_id, entry, payload in events:
            key = f"{entry['automation']}:{event_id}"
            if key in seen:
                continue
            if first and not event_id.startswith("labeled:"):  # a merge, close or push before the first run is history
                seen.add(key)
                continue
            if "--dry-run" in argv:
                print(json.dumps({"would_run": entry["automation"], "event": event_id}))
                continue
            try:
                result = run_event(entry, payload, state, config, graph, run)
            except (RuntimeError, ValueError, KeyError, OSError) as e:
                result = f"error: {e}"[-300:]
            print(json.dumps({"ran": entry["automation"], "event": event_id, "result": result}), flush=True)
            tries[key] = tries.pop(key, 0) + 1
            if result.startswith(RETRIED) and tries[key] < TRIES:
                save(TRIES_FILE, tries)
                continue
            tries.pop(key)
            seen.add(key)
            save(TRIES_FILE, tries)
            save(SEEN, sorted(seen))
        if first and "--dry-run" not in argv:  # the baseline holds even when no label event ran
            save(SEEN, sorted(seen))
        first = False
        if "--once" in argv:
            return 0
        sleep(state.get("poll_seconds", 60))


if __name__ == "__main__":
    argv = config_mod.pop_team(sys.argv[1:])
    sys.exit(main(argv) if argv[:1] == ["--local"] else deploy(argv))

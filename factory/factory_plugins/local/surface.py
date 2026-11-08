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
A node with a `schedule <name>` wake runs when its cron (5 fields, UTC) is due in the loop's current minute; that
run is keyed `schedule:<name>:<YYYY-MM-DDTHH:MM>`. Before the harness starts, the worktree gets the role's skills
and `factory-step` in the harness's project skill directory, excluded from git.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]  # factory/: the core modules are imported by bare name
sys.path.insert(0, str(ENGINE))
import config as config_mod  # noqa: E402
import deploy as deploy_mod  # noqa: E402
import nodes as nodes_mod  # noqa: E402
import plugins  # noqa: E402
from gate_port import ticket_from_ref  # noqa: E402
from ports.system import Query  # noqa: E402


def role_harness(config, role, focus=None, tier=None):
    """The harness a role runs in on the local target: its mode's, else the role's, else the `harness` of its tier's
    model alias (`models.<alias>: {harness, model}`), else `targets.local.harness`, else claude-code.
    Air ignores it: there the platform runs the agent with `targets.air.models`."""
    r = (config.get("roles") or {}).get(role) or {}
    mode = ((r.get("modes") or {}).get(focus) or {}) if focus else {}
    local = (config.get("targets") or {}).get("local") or {}
    alias = local_models(config).get(tier) if tier else None
    return (mode.get("harness") or r.get("harness") or (alias.get("harness") if isinstance(alias, dict) else None)
            or local.get("harness") or "claude-code")


def local_models(config):
    """Tier -> model on the local target: `targets.local.models` (the older top-level `models` still works)."""
    return ((config.get("targets") or {}).get("local") or {}).get("models") or config.get("models") or {}


def local_model(config, tier):
    """The model of `tier` on the local target: a `provider/model` string, or the `model` of a `{harness, model}` alias."""
    m = local_models(config)[tier]
    return m["model"] if isinstance(m, dict) else m


def runner(harness, config=None):
    """The agent runner (ports/agent.py) of `harness`, from the runner plugin that provides it: running a role in a
    harness turns its plugin on (`claude-code` and `claude`: claude; `codex`, `junie`, `pi`). ValueError naming the
    known harnesses for any other name."""
    cls = plugins.registry(config or {}, use=(("agent", harness),)).impl("agent", harness)
    if cls is None:
        raise ValueError(plugins.impl_error(config or {}, "agent", harness, "harness"))
    return cls()


STATE, SEEN, WORKTREES = Path(".factory/local.json"), Path(".factory/local/seen.json"), Path(".factory/worktrees")
TRIES_FILE = Path(".factory/local/tries.json")  # failed runs per event that are tried again
FACTORY = ["uv", "run", "--quiet", str(ENGINE / "cli.py")]
WRITE_OUTPUTS = ("create-pr", "push-to-source")


def plan_local(nodes, config):
    """Each automation the local loop serves, named and triggered as on Air; a manual-only node is not. A scheduled node
    carries its `schedules` (names) beside its event `triggers`. A node woken only by webhooks is not served."""
    out = []
    for name, n in nodes["nodes"].items():
        if n.get("enabled") is False or n["role"] == "human" or n.get("runner"):
            continue
        events = [t for t in n["triggers"] if not str(t).startswith(("schedule ", "webhook ")) and str(t) != "manual"]
        scheds = [str(t)[9:] for t in n["triggers"] if str(t).startswith("schedule ")]
        for automation, tier_arg, tier, focus in deploy_mod.variants(name, n, config["roles"]):
            if events or scheds:
                out.append({"automation": automation, "node": name, "role": n["role"], "tier": tier, "focus": focus,
                            "schedules": scheds,
                            "triggers": [deploy_mod.trigger(t, tier_arg, None, focus) for t in events],
                            "output": n.get("output", "read-only")})
    return out


def field_matches(field, value, low):
    """True when `value` is in one cron field: `*`, numbers, ranges, lists and `*/n` (also `a-b/n`, `a/n`)."""
    for part in field.split(","):
        rng, _, step = part.partition("/")
        a, _, b = rng.partition("-")
        if rng == "*":
            start, end = low, None
        else:
            start, end = int(a), int(b) if b else (None if step else int(a))
        if value >= start and (end is None or value <= end) and (not step or (value - start) % int(step) == 0):
            return True
    return False


def cron_due(cron, when):
    """True when the 5-field cron (minute hour day month weekday, UTC) matches the minute of `when`.
    Day and weekday must both match; weekday 0 or 7 is Sunday."""
    minute, hour, day, month, weekday = cron.split()
    wd = (when.weekday() + 1) % 7
    return (field_matches(minute, when.minute, 0) and field_matches(hour, when.hour, 0)
            and field_matches(day, when.day, 1) and field_matches(month, when.month, 1)
            and (field_matches(weekday, wd, 0) or (wd == 0 and field_matches(weekday, 7, 0))))


def due_schedules(plan, crons, when):
    """(event id, plan entry, payload) for each scheduled automation whose cron is due in the minute of `when`."""
    stamp = when.strftime("%Y-%m-%dT%H:%M")
    return [(f"schedule:{name}:{stamp}", entry, {"eventType": "schedule", "schedule": name})
            for entry in plan for name in entry.get("schedules") or []
            if cron_due(crons[name]["cron"], when)]


def install_skills(run, role, harness, tree):
    """Copy the role's skills (`roles/<role>/skills.txt`) and `factory-step` into the harness's project skill
    directory of the worktree `tree`, and keep that directory out of git."""
    listed = config_mod.resolve(f"roles/{role}/skills.txt")
    skill_dir = runner(harness).skill_dir
    target = tree / skill_dir
    sources = [config_mod.resolve(f"skills/{x}") for x in (listed.read_text().split() if listed.exists() else [])]
    for src in [*sources, config_mod.engine_root() / "skills/factory-step"]:
        shutil.copytree(src, target / src.name, dirs_exist_ok=True)
    path = sh(run, ["git", "-C", str(tree), "rev-parse", "--git-path", "info/exclude"]).stdout.strip()
    if path:
        exclude = tree / path
        exclude.parent.mkdir(parents=True, exist_ok=True)
        text = exclude.read_text() if exclude.exists() else ""
        line = skill_dir.split("/")[0] + "/skills/"
        if line not in text.split():
            exclude.write_text(text + ("" if not text or text.endswith("\n") else "\n") + line + "\n")


def same_repository(url, origin):
    """True when a git `origin` (https or ssh) names the repository at `url`."""
    norm = lambda u: re.sub(r"(\.git)?/*$", "", re.sub(r"^git@([^:]+):", r"https://\1/", u.strip())).lower()  # noqa: E731
    return norm(url) == norm(origin)


def checks(config, run=subprocess.run, which=shutil.which):
    """(missing items, the state `--apply` records)."""
    local = (config.get("targets") or {}).get("local") or {}
    harnesses = sorted({role_harness(config, e["role"], e["focus"], e["tier"]) for e in plan_local(nodes_mod.load(), config)
                        if e["role"] != "script"} or {role_harness(config, None)})
    team = config_mod.team_root()
    workspace = Path(os.path.expanduser(local.get("workspace") or str(team.parent)))
    missing, login, checkouts = [], None, {}
    for harness in harnesses:
        try:
            probe = runner(harness, config).probe(which)
        except ValueError as e:
            missing.append(f"harness: {e}")
            continue
        if not probe.ok:
            missing.append(f"harness: {probe.detail}")
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
    for role, r in (config.get("roles") or {}).items():
        r = r if isinstance(r, dict) else {}
        names = list(r.get("mcp") or []) + [c for m in (r.get("modes") or {}).values() for c in (m or {}).get("mcp") or []]
        if names:
            print(f"warning: role {role}: MCP connectors not provided locally: {', '.join(dict.fromkeys(names))}")
    print(f"local: {len(plan)} automations, harnesses {', '.join(state['harnesses'])}, checkouts {', '.join(state['checkouts'])}")
    if "--apply" in argv:
        surface = LocalSurface()
        surface.apply({**surface.diff(plan, surface.read_state()), "record": state})
        print(f"wrote {STATE}")
    return 0


def sh(run, argv, **kw):
    return run(argv, capture_output=True, text=True, **kw)


def lines(run, *args):
    done = sh(run, ["gh", *args])
    if done.returncode:
        raise RuntimeError(f"gh {' '.join(args)}: {done.stderr.strip()}")
    return [json.loads(x) for x in done.stdout.splitlines() if x.strip()]


def gh_runner(run):
    """`run` (subprocess.run's shape) as a tracker's `gh` runner: the arguments after `gh`, stdout back, a
    RuntimeError on failure."""
    def call(args, timeout=None, token=None):
        done = run(["gh", *args], capture_output=True, text=True,
                   **({"env": {**os.environ, "GH_TOKEN": token}} if token else {}))
        if done.returncode:
            raise RuntimeError(f"gh {' '.join(args[:3])}: {done.stderr.strip()}")
        return done.stdout
    return call


def trackers(config, run):
    """The trackers the poll reads, their `gh` calls made through `run`: the team's tracker (`gate.tracker_of`), or
    with environments that have their own trackers (#1069) each environment's (`scopes.ScopedTracker`), one per
    repository. Built per poll, so no read is answered from an earlier poll's memory."""
    import gate
    import scopes
    gh = gh_runner(run)
    if scopes.project_envs(config):
        scoped = scopes.ScopedTracker(config, lambda c, env: gate.env_systems(c, env, run=gh), os.environ.get("FACTORY_ENV"))
        found = [scoped.systems(env)[0] for env in scoped.candidates()]
    else:
        found = [gate.tracker_of(config, run=gh)]
    return list({getattr(t, "repo", id(t)): t for t in found}.values())


def poll(plan, source, run=None):
    """(event id, plan entry, Air-like trigger payload) for each event the plan's triggers name, from the trackers'
    `find` (`labeled`, `prs`, `closed`; ports/system.py Query). `source` is the trackers, or a repository read
    through the GitHub tracker with `run`. A label event's id is the tracker's id of its latest `labeled` event, so a
    label added again is a new event. All watched labels are read by one search per tracker."""
    if isinstance(source, str):
        source = [plugins.registry({}).impl("tracker", "github-issues").for_repository(source, gh_runner(run))]
    events, cache, labeled, watch = [], {}, {}, {}

    def find(query):
        if query not in cache:
            cache[query] = [x for t in source for x in t.find(query)]
        return cache[query]
    for entry in plan:
        for t in entry["triggers"]:
            kind, label = t["eventType"], t["filters"].get("label")
            if kind in ("issues.labeled", "pull_request.labeled") and label:
                watch[("pr" if kind.startswith("pull_request") else "issue", label)] = True
    for item in find(Query("labeled", values=tuple(sorted(watch)))) if watch else []:
        n, label = item["number"], item["label"]
        key = ("pull_request.labeled" if item["pr"] else "issues.labeled", label)
        body = {"pull_request": {"number": n, "head": {"ref": item["branch"]}}} if item["pr"] else {"issue": {"number": n}}
        labeled.setdefault(key, []).append((f"labeled:{item['event']}", {"eventType": key[0], "action": "labeled", "label": {"name": label}, **body}))
    for entry in plan:
        for t in entry["triggers"]:
            kind, label = t["eventType"], t["filters"].get("label")
            if kind in ("issues.labeled", "pull_request.labeled") and label:
                events += [(eid, entry, payload) for eid, payload in labeled.get((kind, label), [])]
            elif kind in ("pull_request.merged", "pull_request.synchronize"):
                merged = kind.endswith("merged")
                for item in find(Query("prs", values=("merged" if merged else "open",))):
                    pr = {"number": item["number"], "head": {"ref": item["branch"], "sha": item["head"]}}
                    events.append((f"merged:{item['number']}" if merged else f"sync:{item['number']}:{item['head']}", entry,
                                   {"eventType": kind, "action": "closed" if merged else "synchronize",
                                    "pull_request": {**pr, **({"merged": True} if merged else {})}}))
            elif kind == "issues.closed":
                for item in find(Query("closed")):
                    events.append((f"closed:{item['number']}:{item['closed_at']}", entry,
                                   {"eventType": kind, "action": "closed", "issue": {"number": item["number"]}}))
    return events


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
    ticket = ticket_from_ref((pr.get("head") or {}).get("ref")) or issue.get("number") or pr.get("number") or "schedule"
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
    branch = out.get("branch")  # a scheduled routine has no ticket branch
    for c in out.get("checkouts") or []:
        trees.setdefault(c["repo"], (WORKTREES / str(ticket) / c["repo"]).resolve())
    for name, path in trees.items() if branch else ():
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
    model = local_model(config, entry["tier"])
    harness = role_harness(config, node["role"], entry["focus"], entry["tier"])
    install_skills(run, node["role"], harness, trees[primary])
    done = sh(run, runner(harness, config).command(prompt, model, write), cwd=trees[primary],
              env={**os.environ, "FACTORY_TEAM": state["factory"]})
    if done.returncode:
        return f"harness failed: {(done.stdout + done.stderr).strip()[-300:]}"
    if not write or not branch:
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


def main(argv, run=subprocess.run, sleep=time.sleep, now=lambda: datetime.now(timezone.utc)):
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
            events = poll(plan, trackers(config, run))
        except (RuntimeError, ValueError) as e:  # GitHub unreachable or rate limited: try again next poll
            print(json.dumps({"poll failed": str(e)[-300:]}), flush=True)
            if "--once" in argv:
                return 1
            sleep(state.get("poll_seconds", 60))
            continue
        events += due_schedules(plan, graph.get("schedules") or {}, now())
        for event_id, entry, payload in events:
            key = f"{entry['automation']}:{event_id}"
            if key in seen:
                continue
            if first and not event_id.startswith(("labeled:", "schedule:")):  # a merge, close or push before the first run is history
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


class LocalSurface:
    """The local surface (ports/surface.py): this laptop's loop, recorded in `.factory/local.json`."""
    name = "local"

    def plan(self, nodes, config):
        return plan_local(nodes, config)

    def read_state(self):
        return json.loads(STATE.read_text()) if STATE.exists() else None

    def diff(self, plan, state):
        """The automations to add and remove, and the full list `apply` records."""
        want, have = [p["automation"] for p in plan], set((state or {}).get("automations") or [])
        return {"add": [a for a in want if a not in have], "remove": sorted(have - set(want)), "automations": want}

    def apply(self, diff):
        """Record the laptop (`diff["record"]`, the deploy checks' state, else the recorded one) with the plan's
        automations."""
        record = diff.get("record") or self.read_state() or {}
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({**record, "automations": diff["automations"]}, indent=2) + "\n")

    def outputs(self):
        """A write output pushes the node's branch (and, for create-pr, opens the PR); every other is read-only."""
        return {"read-only", *WRITE_OUTPUTS}


if __name__ == "__main__":
    argv = config_mod.pop_team(sys.argv[1:])
    sys.exit(main(argv) if argv[:1] == ["--local"] else deploy(argv))

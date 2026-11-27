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
import itertools
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]  # factory/: the core modules are imported by bare name
sys.path.insert(0, str(ENGINE))
import config as config_mod  # noqa: E402
import definition  # noqa: E402
import deploy as deploy_mod  # noqa: E402
import nodes as nodes_mod  # noqa: E402
import plugins  # noqa: E402
import surfaces  # noqa: E402
import team_setup  # noqa: E402
from gate_port import ticket_from_ref  # noqa: E402
from ports.system import Query  # noqa: E402


SURFACE = "local"  # the surface instance this process serves (`--surface NAME`, ADR-0029)


def settings(config, surface=None):
    """The instance of the local surface `surface` (default: this process's): `surfaces.<name>`, else `targets.local`."""
    return surfaces.instances(config).get(surface or SURFACE) or {}


def role_harness(config, role, focus=None, tier=None, surface=None):
    """The harness a role runs in on the local target: its mode's, else the role's, else the `harness` of its tier's
    model alias (`models.<alias>: {harness, model}`), else `targets.local.harness`, else claude-code.
    Air ignores it: there the platform runs the agent with `targets.air.models`."""
    r = (config.get("roles") or {}).get(role) or {}
    mode = ((r.get("modes") or {}).get(focus) or {}) if focus else {}
    local = settings(config, surface)
    alias = local_models(config, surface).get(tier) if tier else None
    return (mode.get("harness") or r.get("harness") or (alias.get("harness") if isinstance(alias, dict) else None)
            or local.get("harness") or "claude-code")


def local_models(config, surface=None):
    """Tier -> model on the local target: the instance's `models` (the older top-level `models` still works)."""
    return settings(config, surface).get("models") or config.get("models") or {}


def local_model(config, tier, surface=None):
    """The model of `tier` on the local target: a `provider/model` string, or the `model` of a `{harness, model}` alias."""
    m = local_models(config, surface)[tier]
    return m["model"] if isinstance(m, dict) else m


def role_executor(config, role, focus=None, surface=None):
    """The executor a role runs in on the local target (ADR-0029): its mode's, else the role's, else
    `targets.local.executor`, else `host`."""
    r = (config.get("roles") or {}).get(role) or {}
    mode = ((r.get("modes") or {}).get(focus) or {}) if focus else {}
    local = settings(config, surface)
    return mode.get("executor") or r.get("executor") or local.get("executor") or "host"


def executor(config, name, run=subprocess.run):
    """The executor (ports/executor.py) of instance `name`: `executors.<name>` (its `type` picks the plugin, else the
    name does), built with the instance's other keys. ValueError for an unknown type."""
    options = dict(((config.get("executors") or {}).get(name)) or {})
    kind = options.pop("type", name)
    cls = plugins.registry(config, use=(("executor", kind),)).impl("executor", kind)
    if cls is None:
        raise ValueError(plugins.impl_error(config, "executor", kind, f"executors.{name}"))
    return cls(options, run=run)


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
SKILL_CACHE = Path(".factory/cache/skills")
SETUP_STATE = Path(".factory/local/setup-done.json")
PID_FILE, LOG_FILE = Path(".factory/local/local.pid"), Path(".factory/local/local.log")
FACTORY = ["uv", "run", "--quiet", str(ENGINE / "cli.py")]
WRITE_OUTPUTS = ("create-pr", "push-to-source")
_RUN_SEQ = itertools.count(1)
_GIT_LOCK = threading.Lock()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_HANDLES: dict[str, tuple] = {}  # rid -> (executor, handle) for emergency teardown on abort


def node_timeout(node):
    """Per-node timeout in seconds: `timeout` (seconds), else `timeout_minutes` * 60, else None."""
    if node.get("timeout"):
        return node["timeout"]
    return node["timeout_minutes"] * 60 if node.get("timeout_minutes") else None


def use_surface(name):
    """Serve the local surface instance `name`: its own state, seen-events, tries, pid and log files (`local` keeps the old ones)."""
    global SURFACE, STATE, SEEN, TRIES_FILE, PID_FILE, LOG_FILE
    SURFACE = name
    if name == "local":
        STATE, SEEN, TRIES_FILE = Path(".factory/local.json"), Path(".factory/local/seen.json"), Path(".factory/local/tries.json")
        PID_FILE, LOG_FILE = Path(".factory/local/local.pid"), Path(".factory/local/local.log")
    else:
        STATE, SEEN, TRIES_FILE = (Path(f".factory/local-{name}.json"), Path(f".factory/local/{name}-seen.json"),
                                   Path(f".factory/local/{name}-tries.json"))
        PID_FILE, LOG_FILE = Path(f".factory/local/{name}.pid"), Path(f".factory/local/{name}.log")


def pop_surface(argv):
    """`--surface NAME` out of argv: (argv without it, NAME or `local`). The surface is then in use."""
    if "--surface" in argv and argv.index("--surface") + 1 < len(argv):
        i = argv.index("--surface")
        name, argv = argv[i + 1], argv[:i] + argv[i + 2:]
        use_surface(name)
    return argv


def workers(config):
    """Runs at once: the instance's `max_parallel`, else 4 when its executor is isolated, else 1."""
    ex = settings(config).get("executor", "host")
    return settings(config).get("max_parallel") or (1 if surfaces.executor_type(config, ex) == "host" else 4)


def plan_local(nodes, config, surface=None):
    """Each automation the local loop serves, named and triggered as on Air; a manual-only node is not. A scheduled node
    carries its `schedules` (names) beside its event `triggers`. A node woken only by webhooks is not served."""
    out = []
    agents = deploy_mod.agents_of(nodes, config) if config.get("agents") else {}
    for name, n in nodes["nodes"].items():
        if n.get("enabled") is False or n["role"] == "human" or n.get("runner") or not surfaces.serves(settings(config, surface), name):
            continue
        events = [t for t in n["triggers"] if not str(t).startswith(("schedule ", "webhook ")) and str(t) != "manual"]
        scheds = [str(t)[9:] for t in n["triggers"] if str(t).startswith("schedule ")]
        for automation, tier_arg, tier, focus in deploy_mod.variants(name, n, config["roles"], agents.get(name)):
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


def role_skill_entries(role, fetcher=None, cache_dir=None, home=None):
    """`[(name, path)]` of `role`'s skills: ADR-0021 `factory.yaml` (resolved & sha256-verified via `team_setup`),
    any legacy `roles/<role>/skills.txt` entries, and always the engine's `ENGINE_SKILLS`."""
    entries, seen = [], set()

    def add(name, path):
        if path and path.is_dir() and name not in seen:
            seen.add(name)
            entries.append((name, path))

    root = config_mod.team_root()
    if definition.present(root):
        defn = definition.load(root)
        for name, path in team_setup.resolve_role_skills(defn, role, cache_dir=cache_dir or SKILL_CACHE,
                                                          home=home, fetcher=fetcher):
            add(name, path)
    listed = config_mod.resolve(f"roles/{role}/skills.txt")
    if listed.exists():
        for x in listed.read_text().split():
            add(x, config_mod.resolve(f"skills/{x}"))
    for name in team_setup.ENGINE_SKILLS:
        add(name, config_mod.engine_root() / "skills" / name)
    return entries


def role_skill_sources(role, fetcher=None, cache_dir=None, home=None):
    """Skill directories for `role`: ADR-0021 `factory.yaml` (`defaults.skills` + `roles.<role>.skills`), plus any
    legacy `roles/<role>/skills.txt` entries, and always the engine's `factory-step`."""
    return [path for _, path in role_skill_entries(role, fetcher=fetcher, cache_dir=cache_dir, home=home)]


def _exclude_in_worktree(run, tree, lines_to_add):
    """Add `lines_to_add` to `tree`'s git `info/exclude` without duplicates."""
    path = sh(run, ["git", "-C", str(tree), "rev-parse", "--git-path", "info/exclude"]).stdout.strip()
    if not path:
        return
    exclude = tree / path
    exclude.parent.mkdir(parents=True, exist_ok=True)
    text = exclude.read_text() if exclude.exists() else ""
    existing = set(text.split())
    new_lines = [line for line in lines_to_add if line and line not in existing]
    if new_lines:
        prefix = "" if not text or text.endswith("\n") else "\n"
        exclude.write_text(text + prefix + "\n".join(new_lines) + "\n")


def install_skills(run, role, harness, tree, sources=None, fetcher=None):
    """Copy the role's skills and engine skills into the harness's project skill directory of the worktree `tree`,
    and keep only the installed entries out of git (per-skill when `skill_dir` already has tracked files)."""
    skill_dir = runner(harness).skill_dir
    target = tree / skill_dir
    entries = sources if sources is not None else role_skill_entries(role, fetcher=fetcher)
    for name, src in entries:
        shutil.copytree(src, target / name, dirs_exist_ok=True)
    tracked = sh(run, ["git", "-C", str(tree), "ls-files", skill_dir]).stdout.strip()
    lines_to_add = [f"{skill_dir}/{name}/" for name, _ in entries] if tracked else [skill_dir.split("/")[0] + "/skills/"]
    _exclude_in_worktree(run, tree, lines_to_add)


def same_repository(url, origin):
    """True when a git `origin` (https or ssh) names the repository at `url`."""
    norm = lambda u: re.sub(r"(\.git)?/*$", "", re.sub(r"^git@([^:]+):", r"https://\1/", u.strip())).lower()  # noqa: E731
    return norm(url) == norm(origin)


def checks(config, run=subprocess.run, which=shutil.which):
    """(missing items, the state `--apply` records)."""
    local = settings(config)
    harnesses = sorted({role_harness(config, e["role"], e["focus"], e["tier"]) for e in plan_local(nodes_mod.load(), config)
                        if e["role"] != "script"} or {role_harness(config, None)})
    team = config_mod.team_root()
    workspace = Path(os.path.expanduser(local.get("workspace") or str(team.parent)))
    missing, login, checkouts = [], None, {}
    combos = sorted({(role_harness(config, e["role"], e["focus"], e["tier"]), role_executor(config, e["role"], e["focus"]))
                     for e in plan_local(nodes_mod.load(), config) if e["role"] != "script"}
                    or {(role_harness(config, None), role_executor(config, None))})
    executors = sorted({x for _, x in combos})
    failed_executors = set()
    for name in executors:
        try:
            ex = executor(config, name, run)
        except ValueError as e:
            missing.append(f"executor: {e}")
            failed_executors.add(name)
            continue
        bad = [f"executor: {p.detail}" for p in ex.probe(which) if not p.ok]
        if bad:
            missing += bad
            failed_executors.add(name)
            continue
        if ex.isolated:
            gh_var = ((ex.options or {}).get("secrets") or {}).get("GITHUB_TOKEN")
            if not gh_var or gh_var not in os.environ:
                if (ex.options or {}).get("github_token_from_gh"):
                    if not _gh_cli_token(run):
                        missing.append(f"executor: {name}: github_token_from_gh is enabled, but `gh auth token` failed (run `gh auth login`)")
                else:
                    missing.append(f"executor: {name}: GITHUB_TOKEN is not mapped/exported "
                                   f"(set `executors.{name}.secrets: {{GITHUB_TOKEN: <host var>}}` and export it)")
    isolated_harnesses = {}
    for harness, name in combos:
        if name in failed_executors:
            continue
        try:
            rn = runner(harness, config)
            ex = executor(config, name, run)
        except ValueError as e:
            missing.append(f"harness: {e}")
            continue
        if not ex.isolated:
            probe = rn.probe(which)
            if not probe.ok:
                missing.append(f"harness: {probe.detail}")
            continue
        isolated_harnesses.setdefault(name, (ex, []))[1].append((harness, rn))
    for name, (ex, items) in isolated_harnesses.items():
        try:  # probe every harness sharing one isolated executor inside a single sandbox start
            handle = ex.prepare(f"probe-{int(time.time())}-{os.getpid()}-{next(_RUN_SEQ)}")
            try:
                tools_check = ex.exec(
                    handle, ["sh", "-c", 'for b in git gh; do command -v "$b" >/dev/null || { echo "$b"; exit 1; }; done'],
                    timeout=60)
                if tools_check.returncode:
                    tool_name = (tools_check.stdout or "").strip().splitlines()[-1] if (tools_check.stdout or "").strip() else "git/gh"
                    missing.append(f"executor: {name}: missing required tool `{tool_name}` inside sandbox")
                for harness, rn in items:
                    if ex.exec(handle, [rn.cli, "--version"], timeout=120).returncode:
                        missing.append(f"harness: `{rn.cli}` ({harness}) does not run in executor {name}")
            finally:
                ex.teardown(handle)
        except (RuntimeError, OSError) as e:
            missing.append(f"executor: {name}: {e}"[:300])
    if run is subprocess.run and definition.present(team):
        for r in sorted({e["role"] for e in plan_local(nodes_mod.load(), config) if e["role"] != "script"}):
            try:
                role_skill_entries(r, fetcher=team_setup.fetch)
            except (RuntimeError, ValueError, OSError) as e:
                missing.append(f"skill: {e}")
    if any(surfaces.executor_type(config, name) == "host" for name in executors) and not which("factory"):
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
    return missing, {"harnesses": harnesses, "executors": executors, "login": login, "poll_seconds": local.get("poll_seconds", 60),
                     "factory": str(team), "workspace": str(workspace), "checkouts": checkouts}


def deploy(argv, run=subprocess.run, which=shutil.which):
    argv = pop_surface(argv)
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
    def _mcp_names(raw):
        if isinstance(raw, dict):
            return [k for k, v in raw.items() if v is not None]
        return list(raw or [])
    for role, r in (config.get("roles") or {}).items():
        r = r if isinstance(r, dict) else {}
        names = _mcp_names(r.get("mcp")) + [c for m in (r.get("modes") or {}).values() for c in _mcp_names((m or {}).get("mcp"))]
        if names:
            print(f"warning: role {role}: MCP connectors not provided locally: {', '.join(dict.fromkeys(names))}")
    for name in state["executors"]:
        opts = (config.get("executors") or {}).get(name) or {}
        if opts.get("auth_files"):
            print(f"warning: executor {name}: auth_files copies host logins into the sandbox; prefer API keys (ADR-0029)")
        gh_var = (opts.get("secrets") or {}).get("GITHUB_TOKEN")
        if opts.get("github_token_from_gh") and (not gh_var or gh_var not in os.environ):
            print(f"warning: executor {name}: github_token_from_gh falls back to host `gh auth token` "
                  f"(inherits your gh login scopes; prefer a scoped token in {gh_var or 'secrets.GITHUB_TOKEN'})")
        if surfaces.executor_type(config, name) == "docker-sandbox" and opts.get("secrets_mode") == "env":
            print(f"warning: executor {name}: secrets_mode=env passes secrets via `sbx exec -e` (readable inside the VM)")
    if definition.present(config_mod.team_root()):
        defn = definition.load(config_mod.team_root())
        nodes_graph = nodes_mod.load()
        for name in state["executors"]:
            if surfaces.executor_type(config, name) == "host":
                continue
            opts = (config.get("executors") or {}).get(name) or {}
            mapped = set(opts.get("secrets") or {}) | set(opts.get("env") or [])
            if "GITHUB_TOKEN" in mapped or opts.get("github_token_from_gh"):
                mapped |= {"GITHUB_TOKEN", "GH_TOKEN"}
            envs_used = sorted({nodes_graph["nodes"][e["node"]].get("env") for e in plan
                                if e["role"] != "script" and role_executor(config, e["role"], e["focus"]) == name
                                and nodes_graph["nodes"][e["node"]].get("env")})
            warned = set()
            for env_name in envs_used:
                for var, meta in definition.environment_variables(defn, env_name).items():
                    if meta.get("required") and var not in mapped and var not in warned:
                        warned.add(var)
                        print(f"warning: executor {name}: required env var {var} is not mapped in executors.{name}.secrets or executors.{name}.env")
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
    RuntimeError on failure. When `run` is the real `subprocess.run`, retries once after a GitHub rate-limit wait."""
    def call(args, timeout=None, token=None):
        env = {**os.environ, "GH_TOKEN": token} if token else None
        attempts = (1, 2) if run is subprocess.run else (1,)
        for attempt in attempts:
            done = run(["gh", *args], capture_output=True, text=True, **({"env": env} if env else {}))
            if not done.returncode:
                return done.stdout
            text = f"{done.stderr}\n{done.stdout}"
            if attempt == 1 and len(attempts) > 1 and re.search(r"HTTP (403|429)", text) and re.search(r"rate limit|x-ratelimit-remaining:\s*0", text, re.I):
                m = re.search(r"x-ratelimit-reset:\s*(\d+)", text, re.I)
                wait_s = min(max(int(m[1]) - time.time(), 0), 300) if m else 60
                time.sleep(wait_s)
                continue
            raise RuntimeError(f"gh {' '.join(args[:3])}: {done.stderr.strip()}")
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
    Reused worktrees are reset and cleaned so leftover state from an earlier run never contaminates the next one.
    Returns git's error, or "" when the worktree is ready."""
    reused = path.exists()
    if not reused:
        done = sh(run, ["git", "-C", str(checkout), "worktree", "add", "--detach", str(path), start])
        if done.returncode:
            return done.stderr.strip() or "git worktree add failed"
    else:
        sh(run, ["git", "-C", str(path), "reset", "--hard", "--quiet"])
        sh(run, ["git", "-C", str(path), "clean", "-fd", "--quiet"])
    done = sh(run, ["git", "-C", str(path), "checkout", "--quiet", *(["-B", branch] if branch else ["--detach"]), start])
    return done.stderr.strip() or "git checkout failed" if done.returncode else ""


def publish(run, config, entry, out, ticket, branch, name, repos, git_dir, ref):
    """Push `ref` of the repository at `git_dir` to the ticket's branch and, for `create-pr`, open the PR. Nothing
    committed past the default branch: nothing happens. The error text, or None."""
    base = repos[name]["default_branch"]
    if sh(run, ["git", "-C", git_dir, "rev-list", "--count", f"origin/{base}..{ref}"]).stdout.strip() in ("", "0"):
        return None  # nothing committed in this repository
    push = sh(run, ["git", "-C", git_dir, "push", "--quiet", "origin", f"{ref}:refs/heads/{branch}"])
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
    return None


BOX = {"work": "/work", "repos": "/repos", "engine": "/opt/factory/engine", "team": "/opt/factory/team",
       "home": "/home/factory"}  # where an isolated executor's sandbox keeps what the host copies in
STAGE_IGNORE = shutil.ignore_patterns(".git", ".factory", ".venv", "node_modules", "__pycache__", "build", "dist", "*.egg-info")


def _gh_cli_token(run=subprocess.run):
    """Read the host `gh` login token via `gh auth token` when explicitly opted in (`github_token_from_gh: true`)."""
    try:
        done = run(["gh", "auth", "token"], capture_output=True, text=True)
    except OSError:
        return None
    tok = (done.stdout or "").strip() if done.returncode == 0 else ""
    return tok or None


def box_env(ex, rn, team_path=BOX["team"], run=subprocess.run):
    """The environment of an isolated run: the instance's `secrets` ({NAME: HOST_VAR}), the harness's API keys and the
    instance's `env` names that the host has, and the team path. Names only in factory.yaml; values come from the host.
    When `GITHUB_TOKEN` is unset and the executor opts into `github_token_from_gh: true`, falls back to `gh auth token`."""
    opts = getattr(ex, "options", {}) or {}
    env = {n: os.environ[h] for n, h in (opts.get("secrets") or {}).items() if h in os.environ}
    if "GITHUB_TOKEN" not in env and opts.get("github_token_from_gh"):
        if tok := _gh_cli_token(run):
            env["GITHUB_TOKEN"] = tok
    env |= {n: os.environ[n] for n in rn.env if n in os.environ and n not in env}
    env |= {n: os.environ[n] for n in (opts.get("env") or []) if n in os.environ and n not in env}
    if "GITHUB_TOKEN" in env:
        env.setdefault("GH_TOKEN", env["GITHUB_TOKEN"])
    return {**env, "FACTORY_TEAM": team_path}


def _box_paths(ex, handle, env):
    """Discover `$HOME` and writable workspace roots inside the sandbox; falls back to `BOX` defaults."""
    script = ('w=/work; r=/repos; o=/opt/factory; '
              'if ! [ -w /work ] || ! [ -w /opt/factory ]; then '
              'b="${HOME:-/home/factory}/.factory-box"; w="$b/work"; r="$b/repos"; o="$b/opt"; fi; '
              'printf "%s\\n%s\\n%s\\n%s\\n" "${HOME:-/home/factory}" "$w" "$r" "$o"')
    lines_out = [x.strip() for x in ex.exec(handle, ["sh", "-c", script], env=env).stdout.splitlines() if x.strip()]
    if len(lines_out) >= 4:
        home, work_root, repos_root, opt_root = lines_out[:4]
        return {"home": home, "work": work_root, "repos": repos_root, "engine": f"{opt_root}/engine", "team": f"{opt_root}/team"}
    return dict(BOX)


def _wants_auth_files(opts, harness, rn):
    """Whether `executors.<name>.auth_files` enables copying host logins for `harness`/`rn`: `True`, `"auto"`,
    or a list naming `harness`, `rn.name`, `"all"`, or `"claude"` for `claude-code`."""
    raw = (opts or {}).get("auth_files")
    if raw is True or raw == "auto":
        return True
    if not isinstance(raw, list):
        return False
    aliases = {harness, rn.name, "all"} | ({"claude"} if rn.name == "claude-code" else set())
    return any(x in aliases for x in raw)


def _env_domains(config, env_name):
    """Network domains declared on `environments.<env_name>.domains`, for allowlisting executors."""
    return tuple(((config.get("environments") or {}).get(env_name) or {}).get("domains") or ())


def _should_run_setup(ex):
    """Whether runtime environment/role setup scripts should run for `ex` (`setup: auto|true|false`)."""
    opts = getattr(ex, "options", {}) or {}
    mode = opts.get("setup", "auto")
    if mode in (False, "off", "false", "baked", "none"):
        return False
    if mode == "auto" and getattr(ex, "name", None) == "docker" and opts.get("network") == "none":
        return False
    return True


def _local_workspace_note(team_dir, skill_dir, others, isolated):
    """Tell the agent where local skills, team files, setup tools, and additional checkouts live."""
    lines_note = [f"- Factory team files (`roles/<role>/instructions.md`, `lessons/`, `skills/`): `{team_dir}`"]
    if isolated:
        lines_note.append(f"- Role skills are installed in `~/.agents/skills` and `{skill_dir}` inside the checkout.")
    else:
        lines_note.append(f"- Role skills are installed in `{skill_dir}` inside the checkout (use `{skill_dir}/<skill>/SKILL.md` "
                          f"and scripts inside `{skill_dir}/<skill>/` when instructions mention `~/.agents/skills/<skill>` or `skills/<skill>`).")
    lines_note.append("- Tools installed by environment and role setup scripts are on `PATH` (`~/.local/bin`).")
    if others:
        lines_note.append(f"- Other ticket repository checkouts:{others}")
    return "\n\n## Local workspace\n\n" + "\n".join(lines_note)


def teardown_active():
    """Tear down every currently active isolated sandbox (called on abort / second signal)."""
    with _ACTIVE_LOCK:
        active = list(_ACTIVE_HANDLES.items())
        _ACTIVE_HANDLES.clear()
    for _, (ex, handle) in active:
        try:
            ex.teardown(handle)
        except Exception:
            pass


def run_isolated(entry, payload, state, config, node, event, run, ex):
    """The same run as `run_event`, with the workspace inside the executor's sandbox (ADR-0029): host-side read-only
    pre-check so `no-go` events skip sandbox boot, then clone from remotes, `git worktree add`, copy in the engine,
    team and skills, `begin`, run setup scripts, write `.mcp.json`, run the harness (streaming `harness.log` live),
    then the host exports the branch bundle, pushes and opens the PR. Run files are collected before teardown."""
    repos = {r["name"]: r for r in config["repositories"]}
    primary = next(r["name"] for r in config["repositories"] if r.get("role") == "primary")
    pr, issue = payload.get("pull_request") or {}, payload.get("issue") or {}
    ticket = ticket_from_ref((pr.get("head") or {}).get("ref")) or issue.get("number") or pr.get("number") or "schedule"
    harness = role_harness(config, node["role"], entry["focus"], entry["tier"])
    rn, write = runner(harness, config), entry["output"] in WRITE_OUTPUTS
    ex_opts = getattr(ex, "options", {}) or {}
    mapped_gh_var = (ex_opts.get("secrets") or {}).get("GITHUB_TOKEN")
    used_gh_fallback = bool(ex_opts.get("github_token_from_gh")) and (not mapped_gh_var or mapped_gh_var not in os.environ)
    env = box_env(ex, rn, run=run)
    if "GITHUB_TOKEN" not in env:
        return (f"config: executor {ex.name} has no GITHUB_TOKEN: map a scoped token, "
                f"`executors.<name>.secrets: {{GITHUB_TOKEN: <host variable>}}`")
    # Fast host-side read-only pre-check: avoid booting a container/microVM and cloning git repos on `no-go` (exit 10).
    pre = sh(run, [*FACTORY, "--team", state["factory"], "begin", "--check-only", entry["automation"], str(event.resolve())])
    if pre.returncode == 10:
        return "no-go"
    try:
        sources = role_skill_entries(node["role"], fetcher=team_setup.fetch if run is subprocess.run else None)
    except (RuntimeError, ValueError, OSError) as e:
        return f"setup failed: {e}"[-300:]
    rid = f"{ticket}-{int(time.time())}-{next(_RUN_SEQ)}"
    runs = Path(".factory/runs") / rid
    log_path = runs / "harness.log"
    env_name = node.get("env")
    extra_allow = tuple(dict.fromkeys([*getattr(rn, "api_hosts", ()), *_env_domains(config, env_name)]))
    try:
        handle = ex.prepare(rid, extra_allow=extra_allow)
    except (RuntimeError, OSError) as e:
        return f"checkout failed: {e}"[-300:]
    with _ACTIVE_LOCK:
        _ACTIVE_HANDLES[rid] = (ex, handle)
    runs.mkdir(parents=True, exist_ok=True)
    log_header = ["# harness log"]
    if used_gh_fallback:
        log_header.append("# warning: using host `gh auth token` fallback (github_token_from_gh: true)")
    box_paths = dict(BOX)
    try:
        if hasattr(ex, "inject_secrets"):
            env, secret_warnings = ex.inject_secrets(handle, env, host_vars=(getattr(ex, "options", {}) or {}).get("secrets"))
            log_header += [f"# warning: {w}" for w in secret_warnings]
        log_path.write_text("\n".join(log_header) + "\n")
        box_paths = _box_paths(ex, handle, env)
        env["FACTORY_TEAM"] = box_paths["team"]

        def box(argv, cwd=None, extra_env=None, **kw):
            return ex.exec(handle, argv, cwd=cwd, env={**env, **(extra_env or {})}, **kw)

        tree = lambda name: f"{box_paths['work']}/{ticket}/{name}"  # noqa: E731
        event_dst = f"{box_paths['work']}/event.json"
        box_path_prefix = f'PATH="{box_paths["home"]}/bin:{box_paths["home"]}/.local/bin:$PATH"'
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            shutil.copytree(config_mod.engine_root(), stage / "engine", ignore=STAGE_IGNORE)
            shutil.copytree(config_mod.team_root(), stage / "team", ignore=STAGE_IGNORE)
            skills_dir = stage / "skills"
            skills_dir.mkdir()
            for name, src in sources:
                shutil.copytree(src, skills_dir / name, dirs_exist_ok=True)
            for src, dst in ((stage / "engine", box_paths["engine"]), (stage / "team", box_paths["team"]),
                             (skills_dir, f"{box_paths['home']}/.agents/skills"), (event, event_dst)):
                ex.put(handle, src, dst)
            if hasattr(ex, "make_writable"):
                ex.make_writable(handle, f"{box_paths['home']}/.agents/skills")
            bootstrap = (
                f"mkdir -p {box_paths['home']}/bin {box_paths['home']}/.local/bin {box_paths['home']}/.agents/skills "
                f"{box_paths['repos']} {box_paths['work']}/{ticket} && "
                f"ln -sfn {shlex.quote(box_paths['team'])} {shlex.quote(box_paths['home'] + '/factory-team')} && "
                f"for d in {shlex.quote(box_paths['home'] + '/.agents/skills')} {shlex.quote(box_paths['team'])}; do "
                f"[ -d \"$d\" ] && find \"$d\" -type f \\( -name '*.sh' -o -name '*.py' \\) -exec chmod +x {{}} + 2>/dev/null || true; done && "
                f"cat <<'EOF' > {box_paths['home']}/bin/factory\n"
                f"#!/bin/sh\n"
                f"if python3 -c 'import yaml, jinja2' 2>/dev/null; then\n"
                f"  exec python3 {box_paths['engine']}/factory/cli.py \"$@\"\n"
                f"fi\n"
                f"exec uv run --quiet {box_paths['engine']}/factory/cli.py \"$@\"\n"
                f"EOF\n"
                f"chmod +x {box_paths['home']}/bin/factory && "
                f"git config --global credential.helper "
                f"'!f() {{ echo username=x-access-token; echo password=${{GITHUB_TOKEN:-$GH_TOKEN}}; }}; f'"
            )
            box(["sh", "-c", bootstrap])
            if _wants_auth_files(getattr(ex, "options", {}), harness, rn):
                for path in rn.auth_files:
                    src = Path(os.path.expanduser(path))
                    if src.exists():
                        dst = f"{box_paths['home']}/{os.path.relpath(src, Path.home())}"
                        ex.put(handle, src, dst)
                        if hasattr(ex, "make_writable"):
                            ex.make_writable(handle, dst)

            def clone(name):
                repo = f"{box_paths['repos']}/{name}"
                if box(["test", "-d", f"{repo}/.git"]).returncode:
                    got = box(["git", "clone", "--quiet", repos[name]["url"], repo])
                else:
                    got = box(["git", "-C", repo, "fetch", "--quiet", "origin"])
                return f"checkout failed: {(got.stderr or got.stdout).strip()[-300:]}" if got.returncode else ""

            def add_tree(name, start, branch=None):
                made = box(["git", "-C", f"{box_paths['repos']}/{name}", "worktree", "add", "--detach", tree(name), start])
                if made.returncode:
                    return f"checkout failed: {(made.stderr or made.stdout).strip()[-300:]}"
                return switch(name, start, branch)

            def switch(name, start, branch=None):
                done = box(["git", "-C", tree(name), "checkout", "--quiet", *(["-B", branch] if branch else ["--detach"]), start])
                return f"checkout failed: {done.stderr.strip()[-300:]}" if done.returncode else ""
            error = clone(primary) or add_tree(primary, f"origin/{repos[primary]['default_branch']}")
            if error:
                return error
            fx = [f"{box_paths['home']}/bin/factory", "--team", box_paths["team"]]
            begin = box(["sh", "-c", f'{box_path_prefix}; exec "$@"', "sh", *fx, "begin", entry["automation"], event_dst],
                        cwd=tree(primary))
            if begin.returncode == 10:
                return "no-go"
            if begin.returncode:
                return f"begin failed: {(begin.stdout + begin.stderr).strip()[-300:]}"
            out = json.loads(begin.stdout)
            branch, names = out.get("branch"), [primary]
            for c in out.get("checkouts") or []:
                if c["repo"] not in names:
                    names.append(c["repo"])
            if branch:  # the ticket's branch: the remote's when it exists, else cut from the default branch
                for name in names:
                    error = clone(name) if name != primary else ""
                    remote = box(["git", "-C", f"{box_paths['repos']}/{name}", "ls-remote", "--heads", "origin", "--", branch]).stdout.strip()
                    start = f"origin/{branch}" if remote else f"origin/{repos[name]['default_branch']}"
                    error = error or (switch(name, start, branch if write else None) if name == primary
                                      else add_tree(name, start, branch if write else None))
                    if error:
                        return error
            if definition.present(config_mod.team_root()):
                defn = definition.load(config_mod.team_root())
                scripts = team_setup.setup_scripts(defn, env_name, node["role"])
                if scripts:
                    if _should_run_setup(ex):
                        fp = team_setup.scripts_fingerprint(scripts, config_mod.team_root())
                        marker = f"{box_paths['home']}/.factory-setup/{fp}"
                        if box(["test", "-f", marker]).returncode != 0:
                            primary_repo = definition.full_name(repos[primary]["url"])
                            for script in scripts:
                                rel_script = script.relative_to(config_mod.team_root()).as_posix()
                                done_setup = box(["sh", "-c", f'{box_path_prefix}; exec bash "$1"', "sh", rel_script],
                                                 cwd=box_paths["team"], extra_env={"FACTORY_NODE_REPO": primary_repo},
                                                 log_file=log_path)
                                if done_setup.returncode:
                                    return f"setup failed: {(done_setup.stdout + done_setup.stderr).strip()[-300:]}"
                            box(["sh", "-c", f'mkdir -p {shlex.quote(box_paths["home"] + "/.factory-setup")} && : > {shlex.quote(marker)}'])
                    else:
                        opts = getattr(ex, "options", {}) or {}
                        with open(log_path, "a", encoding="utf-8") as fh:
                            fh.write(f"# note: skipped runtime setup scripts (setup={opts.get('setup', 'auto')}, "
                                     f"network={opts.get('network', 'default')})\n")
                mcp_stage = stage / "mcp"
                mcp_stage.mkdir()
                if team_setup.write_mcp_json(defn, mcp_stage, entry["node"]):
                    ex.put(handle, mcp_stage / ".mcp.json", f"{tree(primary)}/.mcp.json")
                    box(["sh", "-c", f'cd {shlex.quote(tree(primary))} && ex="$(git rev-parse --git-path info/exclude)" && '
                         f'mkdir -p "$(dirname "$ex")" && grep -qxF ".mcp.json" "$ex" 2>/dev/null || echo ".mcp.json" >> "$ex"'])
            others = "".join(f"\n- `{n}`: `{tree(n)}`" for n in names if n != primary)
            prompt = deploy_mod.agent_prompt(entry["node"], node, config, entry["automation"], entry["focus"]) + _local_workspace_note(
                box_paths["team"], rn.skill_dir, others, isolated=True)
            target = f"{tree(primary)}/{rn.skill_dir}"
            ex.put(handle, skills_dir, target)
            if rn.skill_dir == ".claude/skills":
                box(["sh", "-c",
                     f'mkdir -p {shlex.quote(box_paths["home"] + "/.claude/skills")} && '
                     f'for s in {shlex.quote(box_paths["home"] + "/.agents/skills")}/*; do '
                     f'[ -d "$s" ] && ln -sfn "$s" {shlex.quote(box_paths["home"] + "/.claude/skills")}/"$(basename "$s")"; done'])
            skill_excludes = " ".join(shlex.quote(f"{rn.skill_dir}/{name}/") for name, _ in sources)
            default_exclude = shlex.quote(f"{rn.skill_dir.split('/')[0]}/skills/")
            box(["sh", "-c",
                 f'cd {shlex.quote(tree(primary))} && ex="$(git rev-parse --git-path info/exclude)" && '
                 f'mkdir -p "$(dirname "$ex")" && '
                 f'if [ -n "$(git ls-files {shlex.quote(rn.skill_dir)})" ]; then '
                 f'for line in {skill_excludes}; do grep -qxF "$line" "$ex" 2>/dev/null || echo "$line" >> "$ex"; done; '
                 f'else grep -qxF {default_exclude} "$ex" 2>/dev/null || echo {default_exclude} >> "$ex"; fi'])
            argv = ["sh", "-c", f'{box_path_prefix}; exec "$@"', "sh",
                    *rn.command(prompt, local_model(config, entry["tier"]), write, isolated=True)]
            done = ex.exec(handle, argv, cwd=tree(primary), env=env, timeout=node_timeout(node), log_file=log_path)
            if done.returncode:
                return f"harness failed: {(done.stdout + done.stderr).strip()[-300:]}"
            if not write or not branch:
                return "ran"
            for name in names:
                base = f"origin/{repos[name]['default_branch']}"
                bundle = ex.export(handle, tree(name), branch, runs / f"{name}.bundle", base=base)
                if bundle is None:
                    continue  # nothing committed in this repository
                host = state["checkouts"][name]
                ref = f"refs/factory/{branch}"
                with _GIT_LOCK:
                    sh(run, ["git", "-C", host, "fetch", "--quiet", "origin"])
                    got = sh(run, ["git", "-C", host, "fetch", "--quiet", str(bundle), f"+{branch}:{ref}"])
                    if got.returncode:
                        return f"push failed: bundle: {got.stderr.strip()[-300:]}"
                    error = publish(run, config, entry, out, ticket, branch, name, repos, host, ref)
                    sh(run, ["git", "-C", host, "update-ref", "-d", ref])
                if error:
                    return error
            return "pushed"
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE_HANDLES.pop(rid, None)
        try:
            runs.mkdir(parents=True, exist_ok=True)
            ex.collect(handle, [f"{box_paths['work']}/{ticket}/{primary}/.factory"], runs)
        finally:
            ex.teardown(handle)


def run_event(entry, payload, state, config, graph, run):
    """Air's part of one node run on this laptop; the gate decides everything else."""
    event = Path(".factory/local/events") / f"{entry['automation']}-{os.getpid()}-{next(_RUN_SEQ)}.json"
    event.parent.mkdir(parents=True, exist_ok=True)
    event.write_text(json.dumps(payload))
    try:
        team = ["--team", state["factory"]]
        node = graph["nodes"][entry["node"]]
        if node["role"] == "script":
            return "swept" if sh(run, [*FACTORY, *team, "sweep", "--event", str(event.resolve())]).returncode == 0 else "sweep failed"
        ex = executor(config, role_executor(config, node["role"], entry["focus"]), run)
        if ex.isolated:
            return run_isolated(entry, payload, state, config, node, event, run, ex)
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
        try:
            sources = role_skill_entries(node["role"], fetcher=team_setup.fetch if run is subprocess.run else None)
        except (RuntimeError, ValueError, OSError) as e:
            return f"setup failed: {e}"[-300:]
        host_env = {**os.environ, "FACTORY_TEAM": state["factory"],
                    "PATH": f"{Path.home() / '.local/bin'}:{os.environ.get('PATH', '')}"}
        if definition.present(config_mod.team_root()):
            defn = definition.load(config_mod.team_root())
            scripts = team_setup.setup_scripts(defn, node.get("env"), node["role"])
            if scripts and _should_run_setup(ex):
                fp = team_setup.scripts_fingerprint(scripts, config_mod.team_root())
                setup_key = f"{node.get('env')}:{node['role']}"
                done_map = json.loads(SETUP_STATE.read_text()) if SETUP_STATE.exists() else {}
                if done_map.get(setup_key) != fp:
                    primary_repo = definition.full_name(repos[primary]["url"])
                    setup_env = {**host_env, "FACTORY_NODE_REPO": primary_repo}
                    for script in scripts:
                        res = sh(run, ["bash", str(script)], cwd=state["factory"], env=setup_env)
                        if res.returncode:
                            return f"setup failed: {(res.stdout + res.stderr).strip()[-300:]}"
                    done_map[setup_key] = fp
                    save(SETUP_STATE, done_map)
            if team_setup.write_mcp_json(defn, trees[primary], entry["node"]):
                _exclude_in_worktree(run, trees[primary], [".mcp.json"])
        others = "".join(f"\n- `{name}`: `{path}`" for name, path in trees.items() if name != primary)
        model = local_model(config, entry["tier"])
        harness = role_harness(config, node["role"], entry["focus"], entry["tier"])
        rn = runner(harness, config)
        prompt = deploy_mod.agent_prompt(entry["node"], node, config, entry["automation"], entry["focus"]) + _local_workspace_note(
            state["factory"], rn.skill_dir, others, isolated=False)
        install_skills(run, node["role"], harness, trees[primary], sources=sources)
        done = ex.exec(None, rn.command(prompt, model, write, isolated=False), cwd=trees[primary],
                       env=host_env, timeout=node_timeout(node))
        if done.returncode:
            return f"harness failed: {(done.stdout + done.stderr).strip()[-300:]}"
        if not write or not branch:
            return "ran"
        for name, path in trees.items():
            error = publish(run, config, entry, out, ticket, branch, name, repos, str(path), "HEAD")
            if error:
                return error
        return "pushed"
    finally:
        event.unlink(missing_ok=True)


# Failures before any harness started are tried on the next polls, up to TRIES runs in all; a run that started
# is not repeated, as on Air (reconcile adds the label again, which is a new event).
RETRIED, TRIES = ("begin failed", "checkout failed", "setup failed", "sweep failed", "error"), 3


def save(path, value):
    """Write a JSON file atomically, so a crash never leaves a broken one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value))
    os.replace(tmp, path)


def reconcile_executors(config, plan, run):
    """Run `reconcile()` on each unique isolated executor in `plan`; returns `{name: removed}` for non-zero cleanups."""
    names = sorted({role_executor(config, e["role"], e["focus"]) for e in plan if e["role"] != "script"})
    removed = {}
    for name in names:
        try:
            ex = executor(config, name, run)
        except ValueError:
            continue
        if ex.isolated:
            try:
                gone = ex.reconcile()
            except (RuntimeError, OSError):
                gone = 0
            if gone:
                removed[name] = gone
    return removed


def _pid_alive(pid: int) -> bool:
    """True when PID `pid` exists and is reachable."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_pid(path: Path) -> int | None:
    """Running PID stored in `path`, or None (cleaning up a stale/corrupt PID file)."""
    if not path.exists():
        return None
    try:
        pid = int(path.read_text().strip())
    except ValueError:
        path.unlink(missing_ok=True)
        return None
    if not _pid_alive(pid):
        path.unlink(missing_ok=True)
        return None
    return pid


def _write_pid(path: Path, pid: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f"{pid or os.getpid()}\n")
    os.replace(tmp, path)


def _loop_argv() -> list[str]:
    """Command to start the local polling loop in a background process, preserving `--team` and `--surface`."""
    base = [sys.executable, str(ENGINE / "cli.py")]
    try:
        team = ["--team", str(config_mod.team_root())]
    except SystemExit:
        team = []
    surf = ["--surface", SURFACE] if SURFACE != "local" else []
    return [*base, *team, "run", "--local", *surf]


def start(argv, popen=subprocess.Popen, sleep=time.sleep):
    """`factory start [--surface NAME] [--foreground] [--once] [--dry-run]`: start the local surface polling loop."""
    argv = pop_surface(list(argv))
    if set(argv) - {"--foreground", "--once", "--dry-run"}:
        print("usage: factory start [--surface NAME] [--foreground] [--once] [--dry-run]")
        return 2
    if not STATE.exists():
        hint = f" --surface {SURFACE}" if SURFACE != "local" else " --target local"
        print(f"no {STATE}: run `factory deploy{hint} --apply` first")
        return 2
    if "--foreground" in argv or "--once" in argv:
        run_args = ["--local", *(["--once"] if "--once" in argv else []), *(["--dry-run"] if "--dry-run" in argv else [])]
        return main(run_args)
    if existing := _read_pid(PID_FILE):
        print(f"local ({SURFACE}): already running (pid {existing})")
        return 0
    cmd = _loop_argv()
    if "--dry-run" in argv:
        print(f"local ({SURFACE}): would start `{' '.join(cmd)}` (pid {PID_FILE}, log {LOG_FILE})")
        return 0
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as log_fh:
        proc = popen(cmd, stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT, start_new_session=True)
    _write_pid(PID_FILE, proc.pid)
    sleep(0.2)
    if proc.poll() is not None:
        PID_FILE.unlink(missing_ok=True)
        print(f"local ({SURFACE}): exited immediately with code {proc.returncode} (see {LOG_FILE})", file=sys.stderr)
        return 1
    print(f"local ({SURFACE}): started (pid {proc.pid}, log {LOG_FILE})")
    return 0


def stop(argv, kill=os.kill, alive=_pid_alive, sleep=time.sleep):
    """`factory stop [--surface NAME] [--force] [--dry-run] [--timeout N]`: stop the local surface polling loop."""
    argv = pop_surface(list(argv))
    timeout = 10.0
    if "--timeout" in argv:
        i = argv.index("--timeout")
        if i + 1 >= len(argv):
            print("stop: --timeout needs seconds", file=sys.stderr)
            return 2
        try:
            timeout = float(argv[i + 1])
        except ValueError:
            print(f"stop: bad --timeout: {argv[i + 1]!r}", file=sys.stderr)
            return 2
        argv = argv[:i] + argv[i + 2:]
    if set(argv) - {"--force", "--dry-run"}:
        print("usage: factory stop [--surface NAME] [--force] [--dry-run] [--timeout N]")
        return 2
    if not PID_FILE.exists():
        print(f"local ({SURFACE}): not running")
        return 0
    try:
        pid = int(PID_FILE.read_text().strip())
    except ValueError:
        PID_FILE.unlink(missing_ok=True)
        print(f"local ({SURFACE}): not running")
        return 0
    if not alive(pid):
        PID_FILE.unlink(missing_ok=True)
        print(f"local ({SURFACE}): not running (cleaned stale pid {pid})")
        return 0
    if "--dry-run" in argv:
        print(f"local ({SURFACE}): would stop pid {pid}")
        return 0
    force = "--force" in argv
    try:
        kill(pid, signal.SIGTERM)
        if force:
            sleep(0.2)
            if alive(pid):
                kill(pid, signal.SIGTERM)  # second signal triggers immediate sandbox teardown
    except ProcessLookupError:
        PID_FILE.unlink(missing_ok=True)
        print(f"local ({SURFACE}): stopped (pid {pid})")
        return 0
    deadline = time.monotonic() + timeout
    while alive(pid) and time.monotonic() < deadline:
        sleep(0.1)
    if not alive(pid):
        PID_FILE.unlink(missing_ok=True)
        print(f"local ({SURFACE}): stopped (pid {pid})")
        return 0
    if force:
        try:
            kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        PID_FILE.unlink(missing_ok=True)
        print(f"local ({SURFACE}): force-killed (pid {pid})")
        return 0
    print(f"local ({SURFACE}): sent SIGTERM to pid {pid}; still draining in-flight runs (re-run with --force to abort)")
    return 0


def main(argv, run=subprocess.run, sleep=time.sleep, now=lambda: datetime.now(timezone.utc)):
    argv = pop_surface(argv)
    if "--local" not in argv or set(argv) - {"--local", "--once", "--dry-run"}:
        print(__doc__)
        return 2
    if not STATE.exists():
        hint = f" --surface {SURFACE}" if SURFACE != "local" else " --target local"
        print(f"no {STATE}: run `factory deploy{hint} --apply` first")
        return 2
    state, config, graph = json.loads(STATE.read_text()), config_mod.load(), nodes_mod.load()
    plan = plan_local(graph, config)
    seen, first = (set(json.loads(SEEN.read_text())), False) if SEEN.exists() else (set(), True)
    tries = json.loads(TRIES_FILE.read_text()) if TRIES_FILE.exists() else {}
    n, poll_seconds = workers(config), state.get("poll_seconds", 60)
    last_reconcile = 0.0
    stop_event, signals_seen = threading.Event(), [0]
    manage_pid = "--dry-run" not in argv and "--once" not in argv

    def _on_signal(signum, frame):
        signals_seen[0] += 1
        stop_event.set()
        if signals_seen[0] >= 2:
            teardown_active()
            if signum == signal.SIGINT:
                raise KeyboardInterrupt()

    prev_handlers = {}
    if manage_pid and threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            prev_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _on_signal)
    if manage_pid:
        _write_pid(PID_FILE)

    def _wait(seconds):
        if sleep is time.sleep:
            stop_event.wait(timeout=seconds)
        else:
            sleep(seconds)

    def one(item):
        try:
            return run_event(item[1], item[2], state, config, graph, run)
        except (RuntimeError, ValueError, KeyError, OSError) as e:
            return f"error: {e}"[-300:]

    def record(item, result):
        event_id, entry, _, key = item
        print(json.dumps({"ran": entry["automation"], "event": event_id, "result": result}), flush=True)
        tries[key] = tries.pop(key, 0) + 1
        if result.startswith(RETRIED) and tries[key] < TRIES:
            save(TRIES_FILE, tries)
            return
        tries.pop(key)
        seen.add(key)
        save(TRIES_FILE, tries)
        save(SEEN, sorted(seen))

    in_flight, active_keys = {}, set()

    def drain(futures):
        for fut in futures:
            item = in_flight.pop(fut)
            active_keys.discard(item[3])
            record(item, fut.result())

    pool = ThreadPoolExecutor(max_workers=n) if n > 1 else None
    try:
        while not stop_event.is_set():
            if "--dry-run" not in argv and time.monotonic() - last_reconcile >= 600:
                cleaned = reconcile_executors(config, plan, run)
                if cleaned:
                    print(json.dumps({"reconciled_sandboxes": cleaned}), flush=True)
                last_reconcile = time.monotonic()
            try:
                events = poll(plan, trackers(config, run))
            except (RuntimeError, ValueError) as e:  # GitHub unreachable or rate limited: try again next poll
                print(json.dumps({"poll failed": str(e)[-300:]}), flush=True)
                if "--once" in argv:
                    return 1
                _wait(poll_seconds)
                continue
            events += due_schedules(plan, graph.get("schedules") or {}, now())
            todo = []
            for event_id, entry, payload in events:
                key = f"{entry['automation']}:{event_id}"
                if key in seen or key in active_keys:
                    continue
                if first and not event_id.startswith(("labeled:", "schedule:")):  # a merge, close or push before the first run is history
                    seen.add(key)
                    continue
                if "--dry-run" in argv:
                    print(json.dumps({"would_run": entry["automation"], "event": event_id}))
                    continue
                todo.append((event_id, entry, payload, key))

            if pool is None:
                for item in todo:
                    if stop_event.is_set():
                        break
                    record(item, one(item))
            else:
                for item in todo:
                    if stop_event.is_set():
                        break
                    active_keys.add(item[3])
                    in_flight[pool.submit(one, item)] = item
                drain([f for f in list(in_flight) if f.done()])
            if first and "--dry-run" not in argv:  # the baseline holds even when no label event ran
                save(SEEN, sorted(seen))
            first = False
            if "--once" in argv or stop_event.is_set():
                if in_flight:
                    drain(list(in_flight))  # deterministic submission order on `--once` or graceful stop
                return 0
            if not in_flight:
                _wait(poll_seconds)
            else:  # non-batching: drain finished runs immediately and keep polling on schedule while slow runs continue
                deadline = time.monotonic() + poll_seconds
                while in_flight and not stop_event.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    done, _ = wait(in_flight, timeout=min(remaining, 0.5) if sleep is time.sleep else remaining,
                                   return_when=FIRST_COMPLETED)
                    if done:
                        drain(done)
                if not in_flight and not stop_event.is_set():
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        _wait(remaining)
        if in_flight:
            drain(list(in_flight))
        return 0
    finally:
        if pool is not None:
            pool.shutdown(wait=signals_seen[0] < 2, cancel_futures=signals_seen[0] >= 2)
        if manage_pid:
            try:
                if PID_FILE.exists() and PID_FILE.read_text().strip() == str(os.getpid()):
                    PID_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            for sig, prev in prev_handlers.items():
                signal.signal(sig, prev)


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

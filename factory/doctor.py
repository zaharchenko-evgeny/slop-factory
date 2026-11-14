#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""`factory doctor`: probe, in this Air environment, every write the gate makes, every read it needs and every tool.

Writes run on a probe issue, a child issue and a probe PR that the doctor creates and then deletes. Output: one JSON
line per probe, `{"probe", "result": ok|denied|missing|fail, "detail"}`; exit 1 on any failure. A failed probe opens or
updates the ticket `Doctor: <probe> fails`; if filing that ticket is denied, a `report` probe line records the failure.
Needs `FACTORY_ENV` (read, write, ops) for the tool check. Also one `env:<NAME>` probe per variable and secret that the
environment declares: `ok` when set and non-empty, `missing` otherwise. In the environments that write to the
`role: project` repositories, one `write:label:<owner/repo>` probe per such repository.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import effects  # noqa: E402
import nodes as nodes_mod  # noqa: E402
import plugins  # noqa: E402

BASE_TOOLS = ("gh", "uv")


def env_needs(env, nodes, resolve=config.resolve):
    """(tools, skills) the roles of the nodes in `env` need: `need_tools` of roles/<r>/setup.sh and
    roles/<r>/skills.txt."""
    roles = sorted({n["role"] for n in nodes["nodes"].values()
                    if n.get("env") == env and n["role"] not in ("script", "human") and n.get("enabled") is not False}
                   | set(((nodes.get("environments") or {}).get(env) or {}).get("roles") or []))  # #1377
    tools, skills = set(BASE_TOOLS), set()
    import definition
    if definition.present(config.team_root()):  # ADR-0021: skills from factory.yaml; setup.sh only installs tools
        defn = definition.load(config.team_root())
        known = defn["factory"].get("roles") or {}
        for r in roles:
            skills |= set(definition.role_skills(defn, r)) if r in known else set()
        from team_setup import ENGINE_SKILLS
        return sorted(tools), sorted(skills | set(ENGINE_SKILLS))
    for r in roles:
        setup, names = Path(resolve(f"roles/{r}/setup.sh")), Path(resolve(f"roles/{r}/skills.txt"))
        tools |= {t for line in setup.read_text().splitlines() if line.startswith("need_tools ") for t in line.split()[1:]}
        skills |= set(names.read_text().split()) if names.exists() else set()
    return sorted(tools), sorted(skills)


def skill_installed(name, home=None, definition_team=None):
    """ADR-0021: `factory setup` installs a definition team's skills under ~/.agents/skills/<name>; the old layout
    vendors them in the team repository's skills/."""
    import definition
    if definition_team is None:
        definition_team = definition.present(config.team_root())
    if definition_team:
        return (Path(home or Path.home()) / ".agents/skills" / name / "SKILL.md").is_file()
    return config.resolve(f"skills/{name}/SKILL.md").is_file()


def env_needs_tiers(nodes):
    return sorted({t for n in nodes["nodes"].values() for t in n.get("tiers") or []})


def probe(results, name, fn):
    try:
        detail = fn()
        results.append({"probe": name, "result": "ok", "detail": str(detail or "")[:200]})
    except Exception as e:  # any gh failure of this probe is a denied permission
        results.append({"probe": name, "result": "denied", "detail": str(e)[:200]})


def run_probes(repo, env, nodes, run, risk_label, which=shutil.which, has_skill=None, run_id="doctor", environ=os.environ):
    """Every probe's result: the selected systems' `doctor.probes` hooks (github: every write the gate makes and every
    read it needs, the wake files and the project write token), then the tools, skills and declared variables."""
    try:
        settings, unread = config.load(), []
    except Exception as e:  # the repository probes still run; the project files cannot be checked
        settings, unread = {}, [{"probe": "project-files", "result": "denied", "detail": str(e)[:200]}]
    results = [r for hook in plugins.registry(settings).hook("doctor.probes")
               for r in hook(settings, env, run, repo=repo, risk_label=risk_label, run_id=run_id, environ=environ,
                             nodes=nodes)]
    results += unread
    tools, skills = env_needs(env, nodes)
    for t in tools:
        results.append({"probe": f"tool:{t}", "result": "ok" if which(t) else "missing", "detail": env})
    for s in skills:
        found = has_skill(s) if has_skill else skill_installed(s)
        results.append({"probe": f"skill:{s}", "result": "ok" if found else "missing", "detail": env})
    declared = (nodes.get("environments") or {}).get(env) or {}
    names = [*(declared.get("variables") or {}), *(declared.get("secrets") or []),
             *(v["name"] for v in declared.get("declared") or [] if v.get("required", True))]
    for name in dict.fromkeys(names):
        results.append({"probe": f"env:{name}", "result": "ok" if environ.get(name) and environ.get(name) != "unset" else "missing", "detail": env,
                        "help": (declared.get("secret_help") or {}).get(name, "")})
    return results


def env_ticket_body(r, run_id):
    """The readable ticket for a variable that a person must set in Air (roles/_common.md layout)."""
    name, env = r["probe"].split(":", 1)[1], r["detail"]
    why = f"- The factory's `{env}` environment declares it" + (f": {r['help']}" if r.get("help") else ".")
    return effects.tagged(f"Set `{name}` in the Air environment of `{env}`. Runs there fail without it.\n\n"
                            f"Why:\n{why}\n- The doctor found it missing or empty in that environment.\n\n"
                            f"How:\n1. In Air, open Environments, then the factory environment for `{env}`.\n"
                            f"2. Add the variable `{name}` as a secret. Leave the other variables as they are.\n"
                            f"3. The next doctor run checks it again.\n\nFactory-Run: {run_id}")


def triage_label():
    """The stage label a new Doctor ticket starts with, so the router picks it up; None without a triage stage."""
    import vocab
    try:
        return vocab.stage("triage")
    except (KeyError, SystemExit):
        return None


def close_ticket(repo, probe, run, run_id="doctor"):
    """Close the open `Doctor: <probe> fails` ticket, with a comment, once the probe passes again."""
    title = f"Doctor: {probe} fails"
    try:
        found = json.loads(run(["issue", "list", "-R", repo, "--state", "open", "--search", f'"{title}" in:title',
                                "--json", "number,title"]))
        for n in [i["number"] for i in found if i["title"] == title]:
            run(["issue", "close", str(n), "-R", repo, "--comment",
                 effects.tagged(f"`{probe}` passes again.\n\nFactory-Run: {run_id}")])
    except Exception as e:
        print(json.dumps({"probe": "report", "result": "denied", "detail": f"{title}: {e}"[:200]}))


COMMENT_EVERY = timedelta(days=1)


def _report_text(body):
    """A report without its `Factory-Run:` trailer, so two runs of the same detail compare equal."""
    return re.sub(r"\n*Factory-Run:[^\n]*\s*$", "", body or "").strip()


def _repeat(repo, number, body, run, now):
    """Whether the factory already reported this detail on ticket `number`, or commented less than a day ago.
    Only the factory's own text counts (`targets.air.bot_login`); a person's comment never silences the doctor."""
    bot = ((config.load().get("targets") or {}).get("air") or {}).get("bot_login")
    issue = json.loads(run(["issue", "view", str(number), "-R", repo, "--json", "body,createdAt,author,comments"]))
    mine = [{"body": c.get("body"), "at": c.get("createdAt")} for c in issue.get("comments") or []
            if bot and (c.get("author") or {}).get("login") == bot]
    if bot and (issue.get("author") or {}).get("login") == bot:
        mine.insert(0, {"body": issue.get("body"), "at": issue.get("createdAt")})
    if not mine:
        return False
    last = mine[-1]
    if _report_text(last["body"]) == _report_text(body):
        return True
    try:
        return now - datetime.fromisoformat(last["at"].replace("Z", "+00:00")) < COMMENT_EVERY
    except (AttributeError, ValueError):
        return False


def file_tickets(repo, failed, run, run_id="doctor", now=None):
    """Open `Doctor: <probe> fails` in the triage stage, or reuse the open one. A reused ticket gets a comment only
    when the detail changed and the last report is over a day old."""
    now = now or datetime.now(timezone.utc)
    for r in failed:
        title = f"Doctor: {r['probe']} fails"
        body = env_ticket_body(r, run_id) if r["probe"].startswith("env:") and r["result"] == "missing" else \
            effects.tagged(f"`factory doctor` probe `{r['probe']}`: {r['result']}. {r['detail']}\n\nFactory-Run: {run_id}")
        try:
            # The list API reads at once; the search index lags and let a second ticket open for the same probe.
            found = json.loads(run(["issue", "list", "-R", repo, "--state", "open", "--limit", "200",
                                    "--json", "number,title"]))
            match = next((i["number"] for i in found if i["title"] == title), None)
            if match:
                if not _repeat(repo, match, body, run, now):
                    run(["issue", "comment", str(match), "-R", repo, "--body", body])
            else:
                label = triage_label()
                run(["issue", "create", "-R", repo, "--title", title, "--body", body, *(["--label", label] if label else [])])
        except Exception as e:
            print(json.dumps({"probe": "report", "result": "denied", "detail": f"{title}: {e}"[:200]}))


NS = "factory"  # probe names, not labels
STUCK, FACTS = f"{NS}:stuck", f"{NS}:facts"
STUCK_HOURS = 6
FACT_STAGES = ("ready", "in-dev", "in-review", "changes-requested", "approved", "blocked")
KNOWN_DRIFT = re.compile(r"update label .+ in \S+ \(description\)")


def stuck_tickets(tracker, tickets, wakes, nodes, now=None, hours=STUCK_HOURS):
    """[(ticket, why)] of the open tickets whose waking label wakes no node for more than `hours`: the reconcile rule
    (`reconcile.stuck_label`), `why` naming each failing guard. `wakes` is (node, label, timeout) per waking label."""
    import reconcile
    now = now or datetime.now(timezone.utc)
    ago = lambda at: now - datetime.fromisoformat(at.replace("Z", "+00:00"))  # noqa: E731
    limit = [(node, label, hours * 30) for node, label, _ in wakes]  # stuck_label waits 2 timeouts
    out = []
    for n in tickets:
        facts = tracker.stale_facts(n)
        if facts.get("issue_state", "open") != "open" or facts.get("blockers_open"):
            continue
        step = reconcile.stuck_label(limit, nodes, facts, facts.get("pr_labels") or [], ago)
        if step:
            out.append((n, step[4]))
    return out


def stuck_probe(tracker, defn, settings, now=None, hours=STUCK_HOURS):
    """`factory:stuck`: fails when a ticket has been stuck longer than `hours`."""
    import reconcile
    import vocab
    _, wakes = reconcile.definition_recovery(defn, settings["tracker"]["stages"], vocab.needs_human(),
                                             settings["limits"].get("run_timeout_minutes", 30))
    kinds = settings["tracker"].get("stage_kinds", {})
    stages = [s for s in settings["tracker"]["stages"] if kinds.get(s) not in ("waiting", "final")]
    found = stuck_tickets(tracker, tracker.sweep_tickets(stages, open_only=True), wakes,
                          defn["workflow"]["nodes"], now, hours) if wakes else []
    return {"probe": STUCK, "result": "fail" if found else "ok",
            "detail": "; ".join(f"#{n}: {why}" for n, why in found)}


def fact_mismatches(facts, in_frontier):
    """[text] per fact of one ticket that disagrees with GitHub: ticket, fact, both values. `in_frontier` is a callable."""
    import vocab
    n, out, labels = facts["ticket"], [], facts.get("pr_labels") or []
    for want in ("green", "red"):
        label = vocab.ci(f"ci-{want}")
        if label in labels and facts.get("ci") != want:
            out.append(f"#{n} ci: gate fact {facts.get('ci')}, GitHub label {label}")
    if facts.get("pr_state") == "merged" and facts["stage"] not in ("merged", "done"):
        out.append(f"#{n} stage: gate fact {facts['stage']}, GitHub PR #{facts.get('pr')} merged")
    if (facts["stage"] == "ready" and facts.get("issue_state", "open") == "open" and not facts.get("blockers_open")
            and not facts.get("assignees") and not in_frontier(n)):
        out.append(f"#{n} frontier: gate fact not in the frontier, GitHub ready with every blocker closed")
    return out


def facts_probe(tracker, stages, frontier):
    """`factory:facts`: the gate's facts of each open factory ticket against GitHub; fails on any mismatch.
    `frontier` returns the issue numbers of the frontier."""
    found = []
    for n in tracker.sweep_tickets([s for s in FACT_STAGES if s in stages], open_only=True):
        found += fact_mismatches({**tracker.facts(n), "ticket": n}, lambda x: x in frontier())
    return {"probe": FACTS, "result": "fail" if found else "ok", "detail": "; ".join(found)}


def deploy_run():
    """Output and exit code of `factory deploy --dry-run`; the doctor never applies a deploy."""
    p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / "cli.py"), "deploy", "--dry-run"],
                       capture_output=True, text=True)
    return p.stdout + p.stderr, p.returncode


def drift_probe(dry_run):
    """`deploy:drift`: fails when the dry run lists anything besides label-description updates, or fails itself."""
    text, code = dry_run()
    lines = [x.strip() for x in text.splitlines() if x.strip() and x.strip() != "unchanged"]
    drift = [x for x in lines if not KNOWN_DRIFT.fullmatch(x)]
    if code and not drift:
        drift = [f"factory deploy --dry-run exited {code}"]
    return {"probe": "deploy:drift", "result": "fail" if drift else "ok", "detail": "; ".join(drift)[:500]}


def report_engine(repo, results, run, run_id="doctor"):
    """A failed engine probe opens or updates its ticket; one that passes again comments on it and closes it."""
    file_tickets(repo, [r for r in results if r["result"] != "ok"], run, run_id)
    for r in results:
        if r["result"] == "ok":
            close_ticket(repo, r["probe"], run, run_id)


def ticket_repository(settings, env=None, defn=None):
    """The GitHub repository the doctor and reconcile file their tickets in: `tracker.repository`, else the code host.
    When the team's tracker is not GitHub Issues, environment `env` (FACTORY_ENV) with a GitHub Issues repository files
    there: the run's token reaches it, the code host can be in another organization (offline-eval-factory#90)."""
    env = os.environ.get("FACTORY_ENV") if env is None else env
    if env and settings["tracker"].get("adapter") not in (None, "github-issues"):
        import definition
        if defn is None and definition.present(config.team_root()):
            defn = definition.load(config.team_root())
        home = next((r["repo"] for r in (definition.repositories(defn, env) if defn else [])
                     if (r["tracker"] or {}).get("adapter") == "github-issues"), None)
        if home:
            return home
    return settings["tracker"].get("repository") or config.vcs(settings)["repository"]


def issue_run(settings):
    """The runner of the `gh`-style calls that file the doctor's tickets (hook `doctor.run`, from the tracker's plugin)."""
    return next(iter(plugins.registry(settings).hook("doctor.run")))


def engine_probes(settings, tracker=None, dry_run=deploy_run, now=None):
    """The `factory:stuck`, `factory:facts` and `deploy:drift` results; a probe that cannot run is a `fail`."""
    import definition
    out = []

    def attempt(name, fn):
        try:
            out.append(fn())
        except Exception as e:
            out.append({"probe": name, "result": "fail", "detail": f"{type(e).__name__}: {e}"[:300]})
    if tracker is None:
        import gate
        tracker = gate.tracker_of(config.load())
    if definition.present(config.team_root()):
        attempt(STUCK, lambda: stuck_probe(tracker, definition.load(config.team_root()), settings, now))
    frontier = next(iter(plugins.registry(settings).hook("doctor.frontier")), None)
    if frontier:
        attempt(FACTS, lambda: facts_probe(tracker, settings["tracker"]["stages"], frontier()))
    attempt("deploy:drift", lambda: drift_probe(dry_run))
    return out


def main(argv=None, run=None):
    env = os.environ.get("FACTORY_ENV")
    if env not in (nodes_mod.load().get("environments") or {}):
        print(json.dumps({"error": "FACTORY_ENV must name an environment of nodes.yaml"}))
        return 2
    settings = config.load()
    run = run or issue_run(settings)  # the doctor files its tickets where the tracker's plugin says
    repo = ticket_repository(settings, env)
    run_id = f"doctor:{env}"
    results = run_probes(repo, env, nodes_mod.load(), run, (settings["tracker"].get("risk_labels") or {"low": "risk:low"})["low"], run_id=run_id)
    engine = engine_probes(settings) if "--engine" in (argv or []) else []
    for r in [*results, *engine]:
        print(json.dumps(r))
    failed = [r for r in [*results, *engine] if r["result"] != "ok"]
    if any(r["result"] != "ok" for r in results):
        file_tickets(repo, [r for r in results if r["result"] != "ok"], run, run_id)
    if engine:
        report_engine(repo, engine, run, run_id)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

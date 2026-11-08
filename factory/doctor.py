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
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import github_tracker  # noqa: E402
import nodes as nodes_mod  # noqa: E402
import youtrack_tracker  # noqa: E402

BASE_TOOLS = ("gh", "uv")


def env_needs(env, nodes, resolve=config.resolve):
    """(tools, skills) the roles of the nodes in `env` need: `need_tools` of roles/<r>/setup.sh and
    roles/<r>/skills.txt."""
    roles = sorted({n["role"] for n in nodes["nodes"].values()
                    if n.get("env") == env and n["role"] not in ("script", "human") and n.get("enabled") is not False})
    tools, skills = set(BASE_TOOLS), set()
    import definition
    if definition.present(config.team_root()):  # ADR-0021: skills from factory.yaml; setup.sh only installs tools
        defn = definition.load(config.team_root())
        known = defn["factory"].get("roles") or {}
        for r in roles:
            skills |= set(definition.role_skills(defn, r)) if r in known else set()
        return sorted(tools), sorted(skills | {"factory-step"})
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


def run_log(api, run, repo):
    """Fetch the log of the latest successful workflow run (a run list alone does not need the logs permission)."""
    runs = json.loads(api(f"repos/{repo}/actions/runs?per_page=1&status=success"))["workflow_runs"]
    if not runs:
        raise RuntimeError("no finished workflow run to read a log from")
    return run(["run", "view", str(runs[0]["id"]), "-R", repo, "--log"])[:50]


def probe(results, name, fn):
    try:
        detail = fn()
        results.append({"probe": name, "result": "ok", "detail": str(detail or "")[:200]})
    except Exception as e:  # any gh failure of this probe is a denied permission
        results.append({"probe": name, "result": "denied", "detail": str(e)[:200]})


def run_probes(repo, env, nodes, run, risk_label, which=shutil.which, has_skill=None, run_id="doctor", environ=os.environ):
    """Every probe's result. Probe objects are removed at the end, also when a probe fails."""
    api = lambda *a: run(["api", *a])  # noqa: E731
    note = lambda text: f"[factory] {text}\n\nFactory-Run: {run_id}"  # noqa: E731
    results, made = [], {"issues": [], "pr": None, "branch": None}
    issue = child = None
    try:
        def create_issue(title):
            n = int(run(["issue", "create", "-R", repo, "--title", title, "--body", note("doctor probe, deleted at once.")]).strip().rsplit("/", 1)[-1])
            made["issues"].append(n)
            return n
        head = json.loads(api(f"repos/{repo}/commits/main"))
        old = head["parents"][0]["sha"] if head.get("parents") else head["sha"]  # a branch behind main: update-branch has work
        branch = f"factory/doctor-probe-{uuid.uuid4().hex[:12]}"
        api("-X", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={old}")
        made["branch"] = branch
        issue, child = create_issue("Doctor probe"), create_issue("Doctor probe child")
        child_id = json.loads(api(f"repos/{repo}/issues/{child}"))["id"]
        api("-X", "PUT", f"repos/{repo}/contents/.factory-doctor-{issue}", "-f", "message=doctor probe", "-f", "content=eA==", "-f", f"branch={branch}")
        made["pr"] = int(run(["pr", "create", "-R", repo, "--head", branch, "--base", "main", "--title", "Doctor probe", "--body", note("doctor probe, closed at once.")]).strip().rsplit("/", 1)[-1])
        pr = made["pr"]
        sha = head["sha"]
        probe(results, "write:label", lambda: run(["issue", "edit", str(issue), "-R", repo, "--add-label", risk_label]))
        probe(results, "write:comment", lambda: run(["issue", "comment", str(issue), "-R", repo, "--body", note("doctor probe")]))
        probe(results, "write:pr-review", lambda: run(["pr", "review", str(pr), "-R", repo, "--comment", "--body", note("doctor probe")]))
        probe(results, "write:sub-issue", lambda: api("-X", "POST", f"repos/{repo}/issues/{issue}/sub_issues", "-F", f"sub_issue_id={child_id}"))
        probe(results, "write:blocked-by", lambda: api("-X", "POST", f"repos/{repo}/issues/{issue}/dependencies/blocked_by", "-F", f"issue_id={child_id}"))
        probe(results, "write:update-branch", lambda: api("-X", "PUT", f"repos/{repo}/pulls/{pr}/update-branch"))
        probe(results, "read:actions-run-logs", lambda: run_log(api, run, repo))
        probe(results, "read:check-runs", lambda: api(f"repos/{repo}/commits/{sha}/check-runs"))
        probe(results, "read:commit-statuses", lambda: api(f"repos/{repo}/commits/{sha}/status"))
        probe(results, "read:timeline", lambda: api(f"repos/{repo}/issues/{issue}/timeline"))
    except Exception as e:
        results.append({"probe": "setup", "result": "denied", "detail": str(e)[:200]})
    finally:
        results += cleanup(repo, made, run)
    try:
        results += comment_wake_probes(config.load(), run)
    except Exception as e:
        results.append({"probe": "project-files", "result": "denied", "detail": str(e)[:200]})
    tools, skills = env_needs(env, nodes)
    for t in tools:
        results.append({"probe": f"tool:{t}", "result": "ok" if which(t) else "missing", "detail": env})
    for s in skills:
        found = has_skill(s) if has_skill else skill_installed(s)
        results.append({"probe": f"skill:{s}", "result": "ok" if found else "missing", "detail": env})
    declared = (nodes.get("environments") or {}).get(env) or {}
    for name in [*(declared.get("variables") or {}), *(declared.get("secrets") or [])]:
        results.append({"probe": f"env:{name}", "result": "ok" if environ.get(name) else "missing", "detail": env,
                        "help": (declared.get("secret_help") or {}).get(name, "")})
    return results


def project_write_probes(settings, env, run, environ=os.environ):
    """In an environment that writes with a project token (github_tracker.token_env, or `issues_token_env` in a multi-repository team), one `write:label:<owner/repo>` probe per `role: project` repository: create
    a probe label, then delete it, with the issues token (`issues_token_env`) or $FACTORY_WRITE_TOKEN when it is set
    (github_tracker.ProjectWriteRun). With `issues_token_env` or `write_mode: token`, a missing token is a `fail` that
    names the secret, and nothing is written."""
    repos = github_tracker.project_repositories(settings)
    issues = settings.get("issues_token_env")
    # deploy adds the issues token to every environment of a multi-repository team (deploy.py), declared or not
    if not (github_tracker.token_env(env, settings) or (issues and len(settings.get("repositories") or []) > 1)):
        return []
    run = github_tracker.ProjectWriteRun(run, repos, environ, issues)
    results = []
    for repo in repos:
        name = f"write:label:{repo}"
        if issues and not environ.get(issues) and not environ.get(github_tracker.WRITE_TOKEN):
            results.append({"probe": name, "result": "fail", "detail": f"{issues} is not set (issues_token_env)"})
            continue
        if not issues and settings.get("write_mode") == "token" and not environ.get(github_tracker.WRITE_TOKEN):
            results.append({"probe": name, "result": "fail", "detail": f"{github_tracker.WRITE_TOKEN} is not set (write_mode: token)"})
            continue
        label = f"factory-doctor-probe-{uuid.uuid4().hex[:12]}"
        probe(results, name, lambda: run(["label", "create", label, "-R", repo, "--description", "doctor probe, deleted at once"]))
        if results[-1]["result"] == "ok":
            try:
                run(["label", "delete", label, "-R", repo, "--yes"])
            except Exception as e:
                results.append({"probe": "cleanup", "result": "denied", "detail": f"label delete {repo}: {e}"[:200]})
    return results


def comment_wake_probes(settings, run):
    """Check that each GitHub project repository has the current comment wake workflow and script."""
    import deploy
    expected = {path: (deploy.ROOT / path).read_text() for path in deploy.COMMENT_WAKE_FILES}
    results = []
    repos = ["/".join(r["url"].rstrip("/").split("/")[-2:]) for r in settings.get("repositories", [])
             if r.get("role") in ("primary", "project") and "github.com" in r.get("url", "")]
    for repo in repos:
        for path, text in expected.items():
            name = f"project-file:{repo}/{path}"
            def check(repo=repo, path=path, text=text):
                data = json.loads(run(["api", f"repos/{repo}/contents/{path}"]))
                if base64.b64decode(data.get("content", "")).decode() != text:
                    raise RuntimeError("file content differs from factory source")
            probe(results, name, check)
    return results


def youtrack_wanted(settings, tiers):
    """(tags, {field: values}) the descriptor's state and command stores name, `<tier>` expanded."""
    tags, fields = set(), {}
    stores = [x["store"] for x in [*(settings.get("states") or {}).values(), *(settings.get("commands") or {}).values()]]
    for store in stores:
        if isinstance(store.get("tag"), str):
            tags |= {store["tag"].replace("<tier>", t) for t in tiers} if "<tier>" in store["tag"] else {store["tag"]}
        if isinstance(store.get("field"), dict):
            fields.setdefault(store["field"]["name"], set()).add(store["field"]["value"])
    return sorted(tags), {k: sorted(v) for k, v in fields.items()}


def youtrack_probes(settings, tiers, environ=os.environ, yt_run=None):
    """Read-only probes of a YouTrack tracker: the token variable, the project, and every mapped tag and field value.
    A missing one is reported, never created."""
    t = settings["tracker"]
    token_env = t.get("token_env", "YOUTRACK_TOKEN")
    results = [{"probe": f"youtrack:token:{token_env}", "result": "ok" if environ.get(token_env) else "missing", "detail": ""}]
    if not environ.get(token_env) and yt_run is None:
        return results
    yt_run = yt_run or youtrack_tracker.urllib_runner(t["url"], environ[token_env])
    q = urllib.parse.quote
    try:
        projects = yt_run("GET", f"/api/admin/projects?fields=id,shortName&query={q(t['project'])}&{youtrack_tracker.TOP}")
        project = next((p for p in projects if p["shortName"] == t["project"]), None)
        results.append({"probe": f"youtrack:project:{t['project']}", "result": "ok" if project else "missing", "detail": ""})
        tags, fields = youtrack_wanted(settings, tiers)
        have = {x["name"] for x in yt_run("GET", f"/api/tags?fields=name&{youtrack_tracker.TOP}")}
        results += [{"probe": f"youtrack:tag:{x}", "result": "ok" if x in have else "missing", "detail": ""} for x in tags]
        values = {}
        if project and fields:
            for c in yt_run("GET", f"/api/admin/projects/{project['id']}/customFields?fields=field(name),bundle(values(name))&{youtrack_tracker.TOP}"):
                values[c["field"]["name"]] = {v["name"] for v in (c.get("bundle") or {}).get("values") or []}
        for name, wanted in fields.items():
            for v in wanted:
                results.append({"probe": f"youtrack:field:{name}={v}", "result": "ok" if v in values.get(name, ()) else "missing",
                                "detail": "" if name in values else "field not in project"})
    except Exception as e:  # an unreadable YouTrack is a denied probe
        results.append({"probe": "youtrack:read", "result": "denied", "detail": str(e)[:200]})
    return results


def cleanup(repo, made, run):
    """Delete the probe objects (an issue the token cannot delete is closed); report a `cleanup` failure."""
    out = []
    steps = ([["pr", "close", str(made["pr"]), "-R", repo, "--delete-branch"]] if made["pr"] else
             [["api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{made['branch']}"]] if made["branch"] else [])
    for n in made["issues"]:
        steps.append(["issue", "delete", str(n), "-R", repo, "--yes"])
    for args in steps:
        try:
            run(args)
        except Exception as e:
            fallback = ["issue", "close", args[2], "-R", repo] if args[:2] == ["issue", "delete"] else None
            try:
                if not fallback:
                    raise e
                run(fallback)
            except Exception:
                out.append({"probe": "cleanup", "result": "denied", "detail": f"{' '.join(args[:3])}: {e}"[:200]})
    return out


def env_ticket_body(r, run_id):
    """The readable ticket for a variable that a person must set in Air (roles/_common.md layout)."""
    name, env = r["probe"].split(":", 1)[1], r["detail"]
    why = f"- The factory's `{env}` environment declares it" + (f": {r['help']}" if r.get("help") else ".")
    return (f"[factory] Set `{name}` in the Air environment of `{env}`. Runs there fail without it.\n\n"
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
                 f"[factory] `{probe}` passes again.\n\nFactory-Run: {run_id}"])
    except Exception as e:
        print(json.dumps({"probe": "report", "result": "denied", "detail": f"{title}: {e}"[:200]}))


def file_tickets(repo, failed, run, run_id="doctor"):
    """Open `Doctor: <probe> fails` in the triage stage, or comment on the open one."""
    for r in failed:
        title = f"Doctor: {r['probe']} fails"
        body = env_ticket_body(r, run_id) if r["probe"].startswith("env:") and r["result"] == "missing" else \
            f"[factory] `factory doctor` probe `{r['probe']}`: {r['result']}. {r['detail']}\n\nFactory-Run: {run_id}"
        try:
            found = json.loads(run(["issue", "list", "-R", repo, "--state", "open", "--search",
                                    f'"{title}" in:title', "--json", "number,title"]))
            match = next((i["number"] for i in found if i["title"] == title), None)
            if match:
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
        import gate_github
        tracker = gate_github.GitHubGateTracker.from_config()
    if definition.present(config.team_root()):
        attempt(STUCK, lambda: stuck_probe(tracker, definition.load(config.team_root()), settings, now))
    attempt(FACTS, lambda: facts_probe(tracker, settings["tracker"]["stages"],
                                                 github_tracker.GitHubTracker.from_config()._frontier))
    attempt("deploy:drift", lambda: drift_probe(dry_run))
    return out


def main(argv=None, run=github_tracker.gh):
    env = os.environ.get("FACTORY_ENV")
    if env not in (nodes_mod.load().get("environments") or {}):
        print(json.dumps({"error": "FACTORY_ENV must name an environment of nodes.yaml"}))
        return 2
    settings = config.load()
    repo = settings["tracker"].get("repository") or settings["code_host"]["repository"]
    run_id = f"doctor:{env}"
    results = run_probes(repo, env, nodes_mod.load(), run, (settings["tracker"].get("risk_labels") or {"low": "risk:low"})["low"], run_id=run_id)
    results += project_write_probes(settings, env, run)
    if settings["tracker"].get("adapter") == "youtrack":
        results += youtrack_probes(settings, env_needs_tiers(nodes_mod.load()))
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

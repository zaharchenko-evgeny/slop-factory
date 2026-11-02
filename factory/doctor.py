#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""`factory doctor`: probe, in this Air environment, every write the gate makes, every read it needs and every tool.

Writes run on a probe issue, a child issue and a probe PR that the doctor creates and then deletes. Output: one JSON
line per probe, `{"probe", "result": ok|denied|missing, "detail"}`; exit 1 on any failure. A failed probe opens or
updates the ticket `Doctor: <probe> fails`. Needs `FACTORY_ENV` (read, write, ops) for the tool check.
"""
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import github_tracker  # noqa: E402
import nodes as nodes_mod  # noqa: E402

BASE_TOOLS = ("gh", "uv")


def env_needs(env, nodes, resolve=config.resolve):
    """(tools, skills) the roles of the nodes in `env` need: `need_tools` of roles/<r>/setup.sh and roles/<r>/skills.txt."""
    roles = sorted({n["role"] for n in nodes["nodes"].values()
                    if n.get("env") == env and n["role"] not in ("script", "human") and n.get("enabled") is not False})
    tools, skills = set(BASE_TOOLS), set()
    for r in roles:
        setup, names = Path(resolve(f"roles/{r}/setup.sh")), Path(resolve(f"roles/{r}/skills.txt"))
        tools |= {t for line in setup.read_text().splitlines() if line.startswith("need_tools ") for t in line.split()[1:]}
        skills |= set(names.read_text().split()) if names.exists() else set()
    return sorted(tools), sorted(skills)


def run_log(api, run, repo):
    """Fetch the log of the latest finished workflow run (a run list alone does not need the logs permission)."""
    runs = json.loads(api(f"repos/{repo}/actions/runs?per_page=1&status=completed"))["workflow_runs"]
    if not runs:
        raise RuntimeError("no finished workflow run to read a log from")
    return run(["run", "view", str(runs[0]["id"]), "-R", repo, "--log"])[:50]


def probe(results, name, fn):
    try:
        detail = fn()
        results.append({"probe": name, "result": "ok", "detail": str(detail or "")[:200]})
    except Exception as e:  # any gh failure of this probe is a denied permission
        results.append({"probe": name, "result": "denied", "detail": str(e)[:200]})


def run_probes(repo, env, nodes, run, risk_label, which=shutil.which, has_skill=None, run_id="doctor"):
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
        issue, child = create_issue("Doctor probe"), create_issue("Doctor probe child")
        child_id = json.loads(api(f"repos/{repo}/issues/{child}"))["id"]
        head = json.loads(api(f"repos/{repo}/commits/main"))
        old = head["parents"][0]["sha"] if head.get("parents") else head["sha"]  # a branch behind main: update-branch has work
        branch = f"factory/doctor-probe-{issue}"
        api("-X", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={old}")
        made["branch"] = branch
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
    tools, skills = env_needs(env, nodes)
    for t in tools:
        results.append({"probe": f"tool:{t}", "result": "ok" if which(t) else "missing", "detail": env})
    for s in skills:
        found = has_skill(s) if has_skill else any(config.engine_root().glob(f"skills/**/{s}/SKILL.md"))
        results.append({"probe": f"skill:{s}", "result": "ok" if found else "missing", "detail": env})
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


def file_tickets(repo, failed, run, run_id="doctor"):
    """Open `Doctor: <probe> fails`, or comment on the open one."""
    for r in failed:
        title = f"Doctor: {r['probe']} fails"
        body = f"[factory] `factory doctor` probe `{r['probe']}`: {r['result']}. {r['detail']}\n\nFactory-Run: {run_id}"
        found = json.loads(run(["issue", "list", "-R", repo, "--state", "open", "--search", f'"{title}" in:title', "--json", "number,title"]))
        match = next((i["number"] for i in found if i["title"] == title), None)
        if match:
            run(["issue", "comment", str(match), "-R", repo, "--body", body])
        else:
            run(["issue", "create", "-R", repo, "--title", title, "--body", body])


def main(argv=None, run=github_tracker.gh):
    env = os.environ.get("FACTORY_ENV")
    if env not in (nodes_mod.load().get("environments") or {}):
        print(json.dumps({"error": "FACTORY_ENV must name an environment of nodes.yaml"}))
        return 2
    settings = config.load()
    repo = settings["tracker"]["repository"]
    run_id = os.environ.get("FACTORY_RUN_ID") or f"doctor:{env}"
    results = run_probes(repo, env, nodes_mod.load(), run, (settings["tracker"].get("risk_labels") or {"low": "risk:low"})["low"], run_id=run_id)
    for r in results:
        print(json.dumps(r))
    failed = [r for r in results if r["result"] != "ok"]
    if failed:
        file_tickets(repo, failed, run, run_id)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

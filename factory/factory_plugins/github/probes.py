"""The GitHub checks of `factory doctor` (hook `doctor.probes`): every write the gate makes and every read it needs, on
a probe issue, a child issue and a probe PR that are deleted at the end; the comment wake files; and the project
write token."""
import base64
import json
import os
import uuid

import plugins
from doctor import probe
from factory_plugins.github import tracker as github_tracker
from factory_plugins.github import wake


def probes(settings, env, run, *, repo, risk_label, run_id="doctor", environ=os.environ, **_):
    """Hook `doctor.probes`: the repository probes, the comment wake files and the project write token."""
    results = repo_probes(repo, run, risk_label, run_id)
    try:
        results += comment_wake_probes(settings, run)
    except Exception as e:
        results.append({"probe": "project-files", "result": "denied", "detail": str(e)[:200]})
    return results + project_write_probes(settings, env, run, environ)


def run_log(api, run, repo):
    """Fetch the log of the latest successful workflow run (a run list alone does not need the logs permission)."""
    runs = json.loads(api(f"repos/{repo}/actions/runs?per_page=1&status=success"))["workflow_runs"]
    if not runs:
        raise RuntimeError("no finished workflow run to read a log from")
    return run(["run", "view", str(runs[0]["id"]), "-R", repo, "--log"])[:50]


def repo_probes(repo, run, risk_label, run_id="doctor"):
    """Every write and read probe on `repo`. Probe objects are removed at the end, also when a probe fails."""
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


def project_write_probes(settings, env, run, environ=os.environ):
    """One `write:label:<owner/repo>` probe per `role: project` repository the environment writes with a token: one
    with a `token` in `env` (ADR-0027), or any when `env` declares $FACTORY_WRITE_TOKEN (github_tracker.token_env).
    Each probe creates a label, then deletes it, with that token (github_tracker.ProjectWriteRun). A `token` secret
    that is not set is a `fail` that names it, and nothing is written. The environment's own repository
    ($FACTORY_ENV_REPOSITORY) is not probed: Air's token writes it."""
    tokens = github_tracker.repository_tokens(settings, {**environ, "FACTORY_ENV": env})
    declared = github_tracker.project_repositories(settings)
    declared += [r for r in tokens if r not in {x.lower() for x in declared}]  # a token-declared repository of any role
    repos = github_tracker.foreign_projects(declared, environ)
    if not github_tracker.token_env(env, settings):
        repos = [r for r in repos if r.lower() in tokens]
    if not repos:
        return []
    run = github_tracker.ProjectWriteRun(run, repos, environ, tokens)
    results = []
    for repo in repos:
        name = f"write:label:{repo}"
        secret = tokens.get(repo.lower())
        if secret and not environ.get(secret) and not environ.get(github_tracker.WRITE_TOKEN):
            results.append({"probe": name, "result": "fail", "detail": f"{secret} is not set (token of {repo})"})
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
    """Check that each GitHub project repository has the current wake files `wake.wake_rules` installs (the comment
    wake workflow and script while the comments plugin is on)."""
    paths = wake.wake_rules(plugins.selected(settings))["install"]
    expected = {path: (wake.ROOT / path).read_text() for path in paths}
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

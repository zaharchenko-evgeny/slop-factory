"""The GitHub comment that records when reconcile last ran."""
import json
import os
from datetime import datetime, timedelta, timezone

from factory_plugins.github.tracker import ProjectWriteRun, checked_out, project_repositories, repository_tokens
import vocab

MARKER = "<!-- factory:heartbeat -->"
PREFIX = "[factory] reconcile heartbeat"


def map_issue(config, run):
    tracker = config["tracker"]
    repo, label = tracker["repository"], tracker.get("map_label") or vocab.map_label()
    issues = json.loads(run(["issue", "list", "-R", repo, "--state", "all", "--label", label,
                            "--json", "number", "--limit", "100"]))
    if len(issues) != 1:
        raise RuntimeError(f"expected one map issue with {label} in {repo}, found {len(issues)}")
    return repo, issues[0]["number"]


def heartbeat_comment(comments, bot_login):
    return [c for c in comments if (c.get("user") or {}).get("login") == bot_login
            and c.get("body", "").startswith(PREFIX) and MARKER in c.get("body", "")]


def comment_body(at):
    return f"{PREFIX}\n\nLast reconcile pass: {at}\n\n{MARKER}\n\nFactory-Run: reconcile"


def comments_for(run, repo, issue):
    out = run(["api", "--paginate", f"repos/{repo}/issues/{issue}/comments", "--jq", ".[]"])
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def reconcile_pass(config, dry_run, progress, *, run=None, environ=os.environ, now=None):
    """Create the map heartbeat once, then update that comment on later passes."""
    from factory_plugins.github import tracker as github_tracker

    run = run or github_tracker.gh
    repo, issue = map_issue(config, run)
    bot = config["targets"]["air"]["bot_login"]
    matches = heartbeat_comment(comments_for(run, repo, issue), bot)
    if len(matches) > 1:
        raise RuntimeError(f"found {len(matches)} reconcile heartbeat comments in {repo}#{issue}")
    action = "edit" if matches else "create"
    result = {"repo": repo, "issue": issue, "action": action, "dry_run": dry_run}
    if not dry_run:
        at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds").replace("+00:00", "Z")
        body = comment_body(at)
        if matches:
            args = ["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{matches[0]['id']}", "-f", f"body={body}"]
        else:
            args = ["api", f"repos/{repo}/issues/{issue}/comments", "-f", f"body={body}"]
        writer = ProjectWriteRun(run, [*project_repositories(config), repo], environ, repository_tokens(config, environ),
                                 checked_out(config, environ), config.get("issues_token_env"))
        writer(args)
    if progress:
        progress(result)
    return [result], False


def doctor_probe(config, run, now=None):
    """Return whether the configured factory identity updated its heartbeat in the last hour."""
    try:
        repo, issue = map_issue(config, run)
        matches = heartbeat_comment(comments_for(run, repo, issue), config["targets"]["air"]["bot_login"])
        if len(matches) != 1:
            raise RuntimeError(f"expected one reconcile heartbeat comment in {repo}#{issue}, found {len(matches)}")
        updated = datetime.fromisoformat(matches[0]["updated_at"].replace("Z", "+00:00"))
        age = (now or datetime.now(timezone.utc)) - updated
        if age > timedelta(minutes=60):
            raise RuntimeError(f"heartbeat is {int(age.total_seconds() // 60)} minutes old")
        return {"probe": "reconcile:heartbeat", "result": "ok", "detail": f"{int(age.total_seconds() // 60)} minutes old"}
    except Exception as e:
        return {"probe": "reconcile:heartbeat", "result": "fail", "detail": str(e)[:200]}

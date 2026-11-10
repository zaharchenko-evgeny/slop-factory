"""`factory ci-sync [--dry-run]`: `factory:ci-green` / `factory:ci-red` on the open factory PRs of every
`role: project` and ticket repository, from their head check runs and commit statuses. `factory reconcile` runs it on its
schedule through the `reconcile.passes` hook."""
import argparse
import json
import sys

import config as config_mod
import plugins
import vocab
from gate_port import tracker_repositories
from factory_plugins.github.gate import GitHubCodeHost, head_ci
from factory_plugins.github.tracker import project_repositories

GO, BAD, FAILED = 0, 2, 1  # the gate's exit codes


def sync_pr(code_host, repository, pr, dry_run=False):
    """Set `factory:ci-green` or `factory:ci-red` on one factory PR {number, head, labels} from its head checks; a
    head with no result yet (pending, or no checks) keeps its labels. A label added before the head's latest result
    belongs to an earlier head: it is removed and added again, because re-adding a present label fires no event.
    The scheduled `ci_sync` calls this per PR; the event path (a finished check, after the #365 probe) calls it for one PR."""
    ci, at = head_ci(code_host.head_checks(repository, pr["head"]))
    r = {"repo": repository, "pr": pr["number"], "head": pr["head"], "ci": ci, "added": [], "removed": []}
    if ci not in ("green", "red"):
        return r
    want, other = vocab.ci(f"ci-{ci}"), vocab.ci("ci-red" if ci == "green" else "ci-green")
    stale = want in pr["labels"] and bool(at) and (code_host.label_added_at(repository, pr["number"], want) or "") < at
    if want in pr["labels"] and not stale:
        r["removed"] = [other] if other in pr["labels"] else []
    else:
        r["removed"], r["added"] = [x for x in (other, want) if x in pr["labels"]], [want]
    if not dry_run and (r["added"] or r["removed"]):
        code_host.edit_pr_labels(repository, pr["number"], add=r["added"], remove=r["removed"])
    return r


def ci_sync(code_host, repositories, dry_run=False, progress=None):
    """The CI label of every open factory PR in `repositories` (the project and ticket repositories, the engine's
    too: no project's CI labels its own PRs, #1207). Returns (results, failed); one PR failing does not stop the others."""
    results, failed = [], False
    for repository in repositories:
        try:
            prs = code_host.factory_prs(repository)
        except Exception as e:
            failed = True
            results.append({"repo": repository, "error": f"{type(e).__name__}: {e}"})
            continue
        for pr in prs:
            try:
                r = sync_pr(code_host, repository, pr, dry_run)
            except Exception as e:
                failed, r = True, {"repo": repository, "pr": pr["number"], "error": f"{type(e).__name__}: {e}"}
            results.append(r)
            if progress:
                progress(r)
    return results, failed


def run_ci_sync(dry_run=False, progress=None):
    """`ci-sync` (manual) and each `factory reconcile` pass (scheduled): the descriptor's project repositories on
    GitHub. It is GitHub's alone: a team whose vcs is another plugin's (space, #1070) has no ci-sync, and nothing is
    read."""
    config = config_mod.load()
    if plugins.registry(config).impl("vcs", config_mod.vcs(config).get("adapter") or "github") is not GitHubCodeHost:
        return [], False
    repositories = [*project_repositories(config), *tracker_repositories(config["tracker"])]  # and every ticket repository
    return ci_sync(GitHubCodeHost.from_config(), list(dict.fromkeys(repositories)), dry_run, progress)


def reconcile_pass(config, dry_run, progress):
    """The `reconcile.passes` hook: {"ci_sync": (results, failed)}; an error is a result, so the reconcile results
    are still reported."""
    try:
        return {"ci_sync": run_ci_sync(dry_run, progress)}
    except Exception as e:
        return {"ci_sync": ([{"error": f"{type(e).__name__}: {e}"}], True)}


def command(argv):
    """`factory ci-sync [--dry-run]`."""
    p = argparse.ArgumentParser(prog="factory ci-sync")
    p.add_argument("--dry-run", action="store_true")
    try:
        a = p.parse_args(config_mod.pop_team(argv))
    except SystemExit:
        return BAD
    results, failed = run_ci_sync(a.dry_run, lambda r: print(json.dumps(r), file=sys.stderr, flush=True))
    print(json.dumps({"dry_run": a.dry_run, "ci_sync": results}))
    return FAILED if failed else GO

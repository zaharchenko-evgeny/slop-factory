"""What wakes a node on GitHub (ADR-0026 `wake_rules`): the Actions workflows of `runner: actions` nodes, rendered into
the factory repository, and the comment wake files (ADR-0023) installed in every GitHub project repository while the
comments plugin is on, and removed once it is off."""
import base64
import json
import sys
from pathlib import Path

import plugins

WAKE_FILES = Path(__file__).resolve().parent / "wake_files"  # package data: copies of the repository files, so the wheel has them
COMMENT_WAKE_FILES = (".github/workflows/comment-wake-label.yml", "scripts/comment-wake-label.sh")


def wake_file_text(path: str) -> str:
    """The text of the comment wake file installed at `path` in a project repository."""
    return (WAKE_FILES / Path(path).name).read_text()


def wake_rules(features) -> dict:
    """{"install": paths, "remove": paths} of the engine files the project repositories carry for the turned-on
    `features` (plugin names): the comment wake files while `comments` is on, else none of them."""
    on = "comments" in set(features)
    return {"install": list(COMMENT_WAKE_FILES) if on else [], "remove": [] if on else list(COMMENT_WAKE_FILES)}


def engine_repository(config: dict) -> str | None:
    """`owner/repo` (lowercase) of the repository the engine comes from: the team's `engine:` source
    (`git+https://github.com/o/r@main`), else the git origin of this engine checkout; None when neither names one."""
    import re
    import subprocess
    source = str(config.get("engine") or "")
    if not source:
        try:
            source = subprocess.run(["git", "-C", str(Path(__file__).resolve().parents[3]), "remote", "get-url", "origin"], capture_output=True,
                                    text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            source = ""
    m = re.search(r"github\.com[/:]([^/@\s]+/[^/@\s]+?)(?:\.git)?(?:@[^/\s]*)?/?$", source)
    return m[1].lower() if m else None


def sync_wake_files(config: dict, run, apply: bool) -> int:
    """Install the files `wake_rules` names in every configured GitHub project repository, and delete the ones it
    removes where an earlier deploy installed them."""
    import deploy
    rules = wake_rules(plugins.selected(config))
    files = {path: wake_file_text(path) for path in dict.fromkeys(rules["install"])}
    engine = engine_repository(config)
    for repo in [r for r in config.get("repositories", []) if "github.com" in r["url"]]:
        name = deploy.repository(repo)["fullName"]
        if name.lower() == engine:  # the engine ships these files from its own tree: never install or delete them there
            continue
        for path in [*files, *rules["remove"]]:
            endpoint = f"repos/{name}/contents/{path}"
            try:
                current = json.loads(run(["api", endpoint]))
            except Exception as error:
                if "HTTP 404" not in str(error):
                    print(f"error: cannot read {name}/{path}: {error}", file=sys.stderr)
                    return 1
                current = None
            if path not in files:
                if current is None:
                    continue
                action, args = "remove", ["api", "-X", "DELETE", endpoint, "-f",
                                          "message=Remove factory comment wake workflow", "-f", f"sha={current.get('sha', '')}"]
            elif current is not None and base64.b64decode(current.get("content", "")).decode() == files[path]:
                continue
            else:
                action = "update" if current is not None else "create"
                args = ["api", "-X", "PUT", endpoint, "-f", "message=Install factory comment wake workflow",
                        "-f", f"content={base64.b64encode(files[path].encode()).decode()}"]
                if (current or {}).get("sha"):
                    args += ["-f", f"sha={current['sha']}"]
            print(f"{action} project file {name}/{path}")
            if apply:
                try:
                    run(args)
                except Exception as error:
                    print(f"error: cannot write {name}/{path}: {error}", file=sys.stderr)
                    return 1
    return 0


def actions_files(nodes: dict, config: dict) -> dict:
    """{path: text} of the GitHub Actions workflows the graph's `runner: actions` nodes render, in the factory repository only."""
    import deploy
    scheds, files = nodes.get("schedules") or {}, {}
    for name, n in nodes["nodes"].items():
        if n.get("runner") == "actions" and n.get("enabled") is not False:
            cron = next(scheds[str(t)[9:]]["cron"] for t in n["triggers"] if str(t).startswith("schedule "))
            wake = "".join(f"  # jactionlint ignore=dangerous-triggers\n  workflow_run:\n    workflows: [{str(t)[13:]}]\n    types: [completed]\n"
                           for t in n["triggers"] if str(t).startswith("workflow_run "))
            events = {}
            for t in n["triggers"]:
                if str(t).startswith(("issues.", "pull_request_review.")):
                    event, _, kind = str(t).partition(".")
                    events.setdefault(event, []).append(kind)
            wake = "".join(f"  {e}:\n    types: [{', '.join(k)}]\n" for e, k in events.items()) + wake
            files[f".github/workflows/factory-{name}.yml"] = ACTIONS_WORKFLOW.format(
                name=name, project=deploy.project_name(config), cron=cron, wake=wake, run=n["run"].replace("uv run factory/cli.py", "uv run --quiet factory/cli.py"))
    return files


# The token is the built-in github.token while the factory repository is also the target (#339); labels it sets wake Air.
ACTIONS_WORKFLOW = """# Generated by `factory deploy` from engine/nodes.yaml (node `{name}`). Do not edit.
name: factory-{name}

on:
  schedule:
    - cron: "{cron}"
{wake}  workflow_dispatch:

permissions: {{}}

concurrency:
  group: factory-{name}
  cancel-in-progress: false

jobs:
  {name}:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: read
      issues: write
      pull-requests: write
    env:
      GH_TOKEN: ${{{{ github.token }}}}
      GH_REPO: ${{{{ github.repository }}}}
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4
        with: {{ persist-credentials: false }}
      - run: pipx install uv
      - run: {run}
"""

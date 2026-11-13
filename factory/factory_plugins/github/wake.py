"""What wakes a node on GitHub (ADR-0026 `wake_rules`): the comment wake files (ADR-0023) installed in every GitHub
project repository while the comments plugin is on, and removed once it is off."""
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

import plugins

ROOT = Path(__file__).resolve().parents[3]
COMMENT_WAKE_FILES = (".github/workflows/comment-wake-label.yml", "scripts/comment-wake-label.sh")


def wake_rules(features) -> dict:
    """{"install": paths, "remove": paths} of the engine files the project repositories carry for the turned-on
    `features` (plugin names): the comment wake files while `comments` is on, else none of them."""
    on = "comments" in set(features)
    return {"install": list(COMMENT_WAKE_FILES) if on else [], "remove": [] if on else list(COMMENT_WAKE_FILES)}


def engine_repository(config: dict) -> str | None:
    """`owner/repo` (lowercase) of the repository the engine comes from: the team's `engine:` source
    (`git+https://github.com/o/r@main`), else the git origin of this engine checkout; None when neither names one."""
    source = config.get("engine")
    source = source if isinstance(source, str) and "github.com" in source else ""  # the compiled config's `engine: 1` is a schema version
    if not source:
        try:
            source = subprocess.run(["git", "-C", str(ROOT), "remote", "get-url", "origin"], capture_output=True,
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
    files = {path: (ROOT / path).read_text() for path in dict.fromkeys(rules["install"])}
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

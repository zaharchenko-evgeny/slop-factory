"""What wakes a node on YouTrack (ADR-0026 `wake_rules`, #892): the comment wake workflow in `comment-wake/`, a
YouTrack workflow rule that adds the `factory:comment` tag when a comment without the factory marker is added. While
the comments plugin is on, `factory deploy` uploads it with the supported `@jetbrains/youtrack-scripting` CLI (YouTrack
has no supported REST endpoint for workflows); a person attaches it to the project once (Project settings → Workflows).
"""
import os
import shlex
import subprocess
import sys
from pathlib import Path

import plugins

WORKFLOW = Path(__file__).resolve().parent / "comment-wake"


def wake_rules(features) -> dict:
    """{"upload": workflow directories} the YouTrack project carries for the turned-on `features` (plugin names): the
    comment wake workflow while `comments` is on, else none (the CLI cannot detach one; a person does)."""
    return {"upload": [WORKFLOW] if "comments" in set(features) else []}


def upload_command(tracker: dict, workflow: Path) -> list[str]:
    """The upload command of one workflow directory, without its `--token` argument."""
    return ["npx", "--yes", "@jetbrains/youtrack-scripting", "upload", str(workflow), "--host", tracker["url"]]


def sync_wake_files(config: dict, run, apply: bool, workflow_run=subprocess.run, environ=os.environ) -> int:
    """Hook `deploy.files`: list the uploads `wake_rules` names (with `--token $<token_env>`) and, on apply, run them
    with the token the tracker's `token_env` names. The token itself is never printed."""
    tracker = config.get("tracker") or {}
    token_env = tracker.get("token_env", "YOUTRACK_TOKEN")
    for workflow in wake_rules(plugins.selected(config))["upload"]:
        command = upload_command(tracker, workflow)
        print("step YouTrack workflow upload: " + shlex.join(command) + f" --token ${token_env}")
        if not apply:
            continue
        token = environ.get(token_env)
        if not token:
            print(f"error: set {token_env} to upload the YouTrack workflow", file=sys.stderr)
            return 1
        try:
            workflow_run([*command, "--token", token], check=True)
        except subprocess.CalledProcessError as e:
            print(f"error: YouTrack workflow upload failed with exit code {e.returncode}", file=sys.stderr)
            return 1
        except OSError as e:
            print(f"error: YouTrack workflow upload failed: {e}", file=sys.stderr)
            return 1
    return 0

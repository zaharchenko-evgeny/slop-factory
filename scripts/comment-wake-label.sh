#!/usr/bin/env bash
set -euo pipefail

event_file=${GITHUB_EVENT_PATH:?GITHUB_EVENT_PATH is required}
repo=${GH_REPO:?GH_REPO is required}
python3 - "$event_file" "$repo" <<'PY'
import json
import subprocess
import sys

with open(sys.argv[1], encoding="utf-8") as event_file:
    event = json.load(event_file)
number = (event.get("issue") or event.get("pull_request") or {}).get("number")
body = (event.get("comment") or {}).get("body")
if not number or body is None:
    raise SystemExit("comment event has no issue number or comment body")
if "<!-- factory:from " in body and "<!-- factory:ask " not in body:
    raise SystemExit(0)
# The label exists once the comment node is deployed (#893); before that there is nothing to wake.
label = subprocess.run(["gh", "api", f"repos/{sys.argv[2]}/labels/factory%3Acomment"], capture_output=True, text=True)
if label.returncode and "HTTP 404" in label.stderr:
    print("no factory:comment label in this repository: nothing to wake")
    raise SystemExit(0)
label.check_returncode()
subprocess.run(["gh", "issue", "edit", str(number), "-R", sys.argv[2], "--add-label", "factory:comment"],
               check=True)
PY

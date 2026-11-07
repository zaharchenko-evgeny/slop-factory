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
if type(number) is not int or number < 1 or not isinstance(body, str):
    raise SystemExit("comment event has no issue number or comment body")
if "<!-- factory:from " in body and "<!-- factory:ask " not in body:
    raise SystemExit(0)
subprocess.run(["gh", "issue", "edit", str(number), "-R", sys.argv[2], "--add-label", "factory:comment"],
               check=True)
PY

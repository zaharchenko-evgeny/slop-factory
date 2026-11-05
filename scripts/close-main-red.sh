#!/usr/bin/env bash
# Close every open `Main is red: <check>` / `Main is red in Air: <check>` issue after a green push to main.
# Env: GH_REPO, SHA (the green commit), RUN_URL. Other issues, even ones titled `Main is red` alone, are left open.
set -euo pipefail
numbers=$(gh issue list --state open --search '"Main is red" in:title' --limit 100 --json number,title |
  python3 -c 'import json, re, sys; [print(i["number"]) for i in json.load(sys.stdin) if re.match(r"Main is red( in Air)?: ", i["title"])]')
for n in $numbers; do
  gh issue comment "$n" --body "[factory] Main is green again at ${SHA}: ${RUN_URL}. Closing this ticket; tickets it blocks resume.

Factory-Run: ${RUN_URL##*/}"
  gh issue close "$n"
done

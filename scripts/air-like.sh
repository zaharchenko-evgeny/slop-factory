#!/bin/bash
# Runs inside the air-like CI container as the non-root user `workspace` (HOME=/home/workspace), the way Air starts
# a cloud environment: .air/cloud/startup.sh per FACTORY_ENV, then the checks of that environment.
set -euo pipefail
cd "$REPO"
git config --global --add safe.directory "$REPO"

echo "== write =="
env FACTORY_ENV=write sh .air/cloud/startup.sh
export PATH="$HOME/.local/bin:$PATH"
uv run --quiet --with pyyaml python -c '
import subprocess, sys
sys.path.insert(0, "factory")
import config
check = next(c for c in config.load()["checks"] if c["name"] == "state-machine")
sys.exit(subprocess.run(["bash", "-c", check["run"]]).returncode)'

echo "== read =="
env FACTORY_ENV=read sh .air/cloud/startup.sh
factory --help >/dev/null
for r in reviewer router; do
  while read -r skill; do
    [ -f "$HOME/.claude/skills/$skill/SKILL.md" ] || { echo "air-like: skill $skill of $r not installed" >&2; exit 1; }
  done < "roles/$r/skills.txt"
done

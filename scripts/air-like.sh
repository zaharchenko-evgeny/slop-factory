#!/bin/bash
# Runs inside the air-like CI container as the non-root user `workspace` (HOME=/home/workspace), the way Air starts
# a cloud environment: the settings repository's .air/cloud/startup.sh per FACTORY_ENV, then the engine checks.
set -euo pipefail
cd "$REPO"
# gh sends sampled telemetry to cafe.github.com, a host outside every environment plan: off, as in Air (#602).
export GH_TELEMETRY=false
# Every download in the startup (curl) retries transient failures.
printf '%s\n' retry=3 retry-all-errors retry-delay=5 > "$HOME/.curlrc"
git config --global --add safe.directory "$REPO"
TEAM="$FACTORY_TEAM"
git config --global --add safe.directory "$TEAM"
STARTUP="$TEAM/.air/cloud/startup.sh"
[ -f "$STARTUP" ] || { echo "air-like: missing settings startup: $STARTUP" >&2; exit 1; }

echo "== write =="
(cd "$TEAM" && env FACTORY_ENV=write sh "$STARTUP")
export PATH="$HOME/.local/bin:$PATH"
uv run --quiet --with pyyaml python -c '
import subprocess, sys
sys.path.insert(0, "factory")
import config
check = next(c for c in config.load()["checks"] if c["name"] == "state-machine")
sys.exit(subprocess.run(["bash", "-c", check["run"]]).returncode)'

echo "== read =="
(cd "$TEAM" && env FACTORY_ENV=read sh "$STARTUP")
# main's `factory` (installed from origin/main, not this branch) prints its usage for --help but exits 2: check the entry point runs and prints it.
help=$(factory --help 2>&1 || true)
# shellcheck disable=SC2016
grep -q '^The `factory` command' <<<"$help"
for r in reviewer router; do
  while read -r skill; do
    [ -f "$HOME/.claude/skills/$skill/SKILL.md" ] || { echo "air-like: skill $skill of $r not installed" >&2; exit 1; }
  done < "$TEAM/roles/$r/skills.txt"
done

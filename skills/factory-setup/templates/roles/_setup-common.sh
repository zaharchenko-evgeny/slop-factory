#!/usr/bin/env bash
# Shared checks for roles/<role>/setup.sh: required tools and skills.
# Skills come from skills/vendor/ (pinned in skills.lock, ADR-0008); installed
# folders are a fallback for skills that are not vendored.
set -euo pipefail

need_tools() {
  local missing=0 t
  for t in "$@"; do
    if ! command -v "$t" >/dev/null 2>&1; then
      echo "missing tool: $t" >&2
      missing=1
    fi
  done
  return "$missing"
}

# Skill directories searched, in pi's discovery order.
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
skill_dirs=(".agents/skills" "$HOME/.agents/skills" "$HOME/.pi/agent/skills" "$HOME/.pi/agent/npm/node_modules")

need_skills() {
  local file=$1 missing=0 s d found
  while IFS= read -r s; do
    [ -z "$s" ] && continue
    found=no
    [ -f "$repo_root/skills/vendor/$s/SKILL.md" ] && continue
    for d in "${skill_dirs[@]}"; do
      if [ -d "$d" ] && find "$d" -path "*/$s/SKILL.md" -print -quit 2>/dev/null | grep -q .; then
        found=yes
        break
      fi
    done
    if [ "$found" = no ]; then
      echo "missing skill: $s" >&2
      missing=1
    fi
  done < "$file"
  return "$missing"
}

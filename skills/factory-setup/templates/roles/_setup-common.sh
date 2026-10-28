#!/usr/bin/env bash
# Shared checks for roles/<role>/setup.sh: required tools and skills.
# shortcut: only verifies installed skills; vendoring and pinning through
# skills.lock come with the setup skill templates (#6).
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
skill_dirs=(".agents/skills" "$HOME/.agents/skills" "$HOME/.pi/agent/skills" "$HOME/.pi/agent/npm/node_modules")

need_skills() {
  local file=$1 missing=0 s d found
  while IFS= read -r s; do
    [ -z "$s" ] && continue
    found=no
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

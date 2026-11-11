#!/bin/sh
# Air startup script (ADR-0021). Air runs it from the checkout that holds it, on every launch and resume.
# It installs the engine that factory.yaml pins, gives the engine this definition as ~/factory-team, and
# runs `factory setup` for the environment deploy named in FACTORY_ENV (skills, then setup scripts).
# It also runs from a project checkout (the cwd): it finds the team's checkout under /workspaces/ by its factory.yaml.
# FACTORY_SKIP_IF_INSTALLED=1 (the first prompt step of a project's automation sets it) exits when the pinned
# engine is installed and ~/factory-team exists.
set -eu
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
export PATH="$BIN:$PATH"
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
TEAM=$(pwd)
if [ ! -f "$TEAM/factory.yaml" ]; then  # the main repository is a project: find the team's checkout beside it
  for d in "${FACTORY_WORKSPACES:-/workspaces}"/*/; do
    if [ -f "${d}factory.yaml" ] && [ -f "${d}workflow.yaml" ]; then TEAM=${d%/}; break; fi
  done
fi
[ -f "$TEAM/factory.yaml" ] || { echo "startup: no checkout with factory.yaml and workflow.yaml" >&2; exit 1; }
ENGINE=$(sed -n 's/^engine: *"\{0,1\}\([^" #]*\).*/\1/p' "$TEAM/factory.yaml" | head -n 1)
MARK="$BIN/.factory-engine-spec"
if [ -n "${FACTORY_SKIP_IF_INSTALLED:-}" ] && command -v factory >/dev/null 2>&1 && [ -e "$HOME/factory-team" ] \
   && [ "$(cat "$MARK" 2>/dev/null)" = "$ENGINE" ]; then
  echo "startup: factory is installed at $ENGINE; skipped" >&2
  exit 0
fi
rm -f "$MARK"
uv tool install --quiet --force "$ENGINE"
ln -sfn "$TEAM" "$HOME/factory-team"
factory setup --environment "${FACTORY_ENV:?deploy sets FACTORY_ENV}"
printf '%s\n' "$ENGINE" > "$MARK"

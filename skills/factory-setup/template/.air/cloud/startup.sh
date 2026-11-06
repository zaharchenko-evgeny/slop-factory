#!/bin/sh
# Air startup script (ADR-0021). Air runs it from the checkout that holds it, on every launch and resume.
# It installs the engine that factory.yaml pins, gives the engine this definition as ~/factory-team, and
# runs `factory setup` for the environment deploy named in FACTORY_ENV (skills, then setup scripts).
set -eu
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
export PATH="$BIN:$PATH"
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
TEAM=$(pwd)
if [ ! -f "$TEAM/factory.yaml" ]; then  # an environment whose main repository is a project: the team checkout
  TEAM="/workspaces/$(basename "${FACTORY_TEAM_REPOSITORY:?deploy sets FACTORY_TEAM_REPOSITORY}")"
fi
ENGINE=$(sed -n 's/^engine: *"\{0,1\}\([^" #]*\).*/\1/p' "$TEAM/factory.yaml" | head -n 1)
uv tool install --quiet --force "$ENGINE"
ln -sfn "$TEAM" "$HOME/factory-team"
factory setup --environment "${FACTORY_ENV:?deploy sets FACTORY_ENV}"

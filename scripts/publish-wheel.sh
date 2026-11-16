#!/bin/sh
# Build the slop-factory wheel (engine + progress UI) into dist/.
# Usage: scripts/publish-wheel.sh [--skip-ui]
#   --skip-ui  use the committed bundle in factory/ui/assets instead of rebuilding it.
# Writes dist/slop_factory-<version>-py3-none-any.whl, dist/LATEST (the wheel's
# file name) and dist/SHA256SUMS. A team checkout's start-ui.sh installs dist/LATEST.
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

if [ "${1:-}" != "--skip-ui" ]; then
  command -v npm >/dev/null 2>&1 || { echo "npm not found; install Node 22+ or pass --skip-ui" >&2; exit 1; }
  [ -d ui/node_modules ] || npm --prefix ui ci
  npm --prefix ui run build
fi

rm -rf build dist
uv build --wheel --out-dir dist
WHEEL=$(basename "$(find dist -maxdepth 1 -name 'slop_factory-*.whl' | head -n 1)")
printf '%s\n' "$WHEEL" > dist/LATEST
COMMIT=$(git rev-parse HEAD 2>/dev/null || echo unknown)
DIRTY=""
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then DIRTY=" (dirty)"; fi
{
  printf 'commit %s%s\n' "$COMMIT" "$DIRTY"
  (cd dist && shasum -a 256 "$WHEEL")
} > dist/SHA256SUMS
echo "published $ROOT/dist/$WHEEL"

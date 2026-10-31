#!/bin/sh
# Air environment setup (docs/targets.md): runs in the repository root on every launch and resume.
# Idempotent: a launch from a snapshot already has the tools.
set -e
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
export PATH="$BIN:$PATH"
# shellcheck disable=SC2016
grep -q '.local/bin' "$HOME/.bashrc" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
# gate.py needs uv (it resolves the script's own dependencies) and gh (tracker writes).
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$BIN" UV_NO_MODIFY_PATH=1 sh
fi
if ! command -v gh >/dev/null 2>&1; then
  tmp=$(mktemp -d)
  curl -sSL https://github.com/cli/cli/releases/download/v2.62.0/gh_2.62.0_linux_amd64.tar.gz | tar -xz -C "$tmp"
  cp "$tmp"/gh_*/bin/gh "$BIN/gh"
fi
# FACTORY_ENV (the environment's variable) picks the branch: `light` has no approver tools.
case "${FACTORY_ENV:-default}" in
  light|probe) echo "startup ran $(date -u +%FT%TZ) FACTORY_ENV=$FACTORY_ENV"; exit 0 ;;
esac
if ! command -v conftest >/dev/null 2>&1 && [ ! -x "$BIN/conftest" ]; then
  curl -sSL https://github.com/open-policy-agent/conftest/releases/download/v0.56.0/conftest_0.56.0_Linux_x86_64.tar.gz | tar -xz -C "$BIN" conftest
fi
# decisions-kit comes from the additional repository JetBrains/jbeval; skipped when it is absent.
if ! command -v decide >/dev/null 2>&1 && [ ! -x "$BIN/decide" ] && [ -d ../jbeval/.git ]; then
  git -C ../jbeval fetch --depth 1 origin tag decisions-kit/v0.3.0
  rm -rf /tmp/decisions-kit
  git -C ../jbeval worktree add --force /tmp/decisions-kit decisions-kit/v0.3.0
  uv tool install /tmp/decisions-kit/apps/decisions
fi

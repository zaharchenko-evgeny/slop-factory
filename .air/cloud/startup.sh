#!/bin/sh
# Air environment setup (docs/targets.md): runs in the repository root on every launch and resume.
# Idempotent: a launch from a snapshot already has the tools.
set -e
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
export PATH="$BIN:$PATH"
# shellcheck disable=SC2016
grep -q '.local/bin' "$HOME/.bashrc" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
# Every download is pinned to a version and a SHA-256; a mismatch fails the script (set -e), nothing is installed.
UV_URL=https://github.com/astral-sh/uv/releases/download/0.5.11/uv-x86_64-unknown-linux-gnu.tar.gz
UV_SHA=14411de26cdea5f5139fafaf2b675b1c633e744dd49c6d6a9fc8817ec065158b
GH_URL=https://github.com/cli/cli/releases/download/v2.62.0/gh_2.62.0_linux_amd64.tar.gz
GH_SHA=41c8b0698ad3003cb5c44bde672a1ffd5f818595abd80162fbf8cc999418446a
CONFTEST_URL=https://github.com/open-policy-agent/conftest/releases/download/v0.56.0/conftest_0.56.0_Linux_x86_64.tar.gz
CONFTEST_SHA=620f41640d63bbde1646a108ce2816ba54c980466ceebb7e754dda2d508e8cd5
# fetch <url> <sha256>: download to $TMP/pkg.tgz and verify it.
fetch() {
  TMP=$(mktemp -d)
  curl -fsSL "$1" -o "$TMP/pkg.tgz"
  if command -v sha256sum >/dev/null 2>&1; then SUM="sha256sum -c -"; else SUM="shasum -a 256 -c -"; fi
  echo "$2  $TMP/pkg.tgz" | $SUM >/dev/null 2>&1 || { echo "startup: checksum mismatch for $1" >&2; exit 1; }
}
# gate.py needs uv (it resolves the script's own dependencies) and gh (tracker writes).
if ! command -v uv >/dev/null 2>&1; then
  fetch "$UV_URL" "$UV_SHA"
  tar -xzf "$TMP/pkg.tgz" -C "$TMP"
  cp "$TMP"/uv-*/uv "$TMP"/uv-*/uvx "$BIN/"
fi
if ! command -v gh >/dev/null 2>&1; then
  fetch "$GH_URL" "$GH_SHA"
  tar -xzf "$TMP/pkg.tgz" -C "$TMP"
  cp "$TMP"/gh_*/bin/gh "$BIN/gh"
fi
# FACTORY_ENV (the environment's variable) picks the branch: `light` has no approver tools.
case "${FACTORY_ENV:-default}" in
  light|probe) echo "startup ran $(date -u +%FT%TZ) FACTORY_ENV=$FACTORY_ENV"; exit 0 ;;
esac
if ! command -v conftest >/dev/null 2>&1 && [ ! -x "$BIN/conftest" ]; then
  fetch "$CONFTEST_URL" "$CONFTEST_SHA"
  tar -xzf "$TMP/pkg.tgz" -C "$BIN" conftest
fi
# decisions-kit comes from the additional repository JetBrains/jbeval; skipped when it is absent.
if ! command -v decide >/dev/null 2>&1 && [ ! -x "$BIN/decide" ] && [ -d ../jbeval/.git ]; then
  git -C ../jbeval fetch --depth 1 origin tag decisions-kit/v0.3.0
  rm -rf /tmp/decisions-kit
  git -C ../jbeval worktree add --force /tmp/decisions-kit decisions-kit/v0.3.0
  uv tool install /tmp/decisions-kit/apps/decisions
fi

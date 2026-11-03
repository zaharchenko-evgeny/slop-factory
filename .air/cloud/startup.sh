#!/bin/sh
# Air environment setup (docs/targets.md): runs in the repository root on every launch and resume.
# Idempotent: a launch from a snapshot already has the tools.
set -e
# FACTORY_ENV picks the environment (ADR-0015): `read` (intake, review, join, finish), `write` (implement, fix),
# `ops` (routines). Each installs the skills and tools of its roles; test_air_startup checks the roles against nodes.yaml.
ROLES=
TOOLS=
case "${FACTORY_ENV:-default}" in
  read) ROLES="router reviewer" ;;
  write) ROLES="developer"; TOOLS="shellcheck-py==0.10.0.1 jactionlint==2.3.0" ;;  # tools of the pre-review and CI checks
  ops) ROLES="distill retro" ;;
esac
# resolve <path>: the team's file replaces the engine's file at the same path. The team root is the settings
# repository's checkout when `factory deploy` rendered FACTORY_TEAM_REPOSITORY, else the repository root.
ENGINE="$HOME/factory-main"
[ -n "${FACTORY_DRY:-}" ] && ENGINE=.
TEAM=.
[ -n "${FACTORY_TEAM_REPOSITORY:-}" ] && [ -z "${FACTORY_DRY:-}" ] && TEAM="$HOME/factory-team"
resolve() { if [ -e "$TEAM/$1" ]; then echo "$TEAM/$1"; else echo "$ENGINE/$1"; fi; }
# skill_names / role_tools: the skills (skills.txt) and the `need_tools` of every role of the environment.
skill_names() { for r in $ROLES; do cat "$(resolve "roles/$r/skills.txt")"; done | sort -u | tr '\n' ' '; }
role_tools() { for r in $ROLES; do sed -n 's/^need_tools //p' "$(resolve "roles/$r/setup.sh")"; done | tr ' ' '\n' | sort -u | tr '\n' ' '; }
if [ -n "${FACTORY_DRY:-}" ]; then
  echo "startup: env=${FACTORY_ENV:-default} roles=$ROLES"
  echo "startup: would install skills: $(skill_names)"
  echo "startup: would install tools: $(role_tools)$TOOLS"
  exit 0
fi
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
# The settings repository is an additional repository of the automation (checked out beside this one); when the
# launch has none, it is public and read-only: cloned without a token, replaced on every launch.
if [ "$TEAM" != . ]; then
  SIBLING="../$(basename "${FACTORY_TEAM_REPOSITORY%/}" .git)"
  rm -rf "$TEAM"
  if [ -d "$SIBLING/.git" ]; then
    ln -s "$(cd "$SIBLING" && pwd)" "$TEAM"
  else
    git clone --depth 1 "$FACTORY_TEAM_REPOSITORY" "$TEAM" || { echo "startup: team clone failed" >&2; exit 1; }
  fi
  sed -i '/^export FACTORY_TEAM=/d' "$HOME/.bashrc" 2>/dev/null || true
  echo "export FACTORY_TEAM=\"$TEAM\"" >> "$HOME/.bashrc"
fi
FACTORY_TEAM=$(cd "$TEAM" && pwd)
export FACTORY_TEAM
# The engine version is the settings descriptor's `engine` (factory.project.yaml, else factory.descriptor.yaml):
# a tag or `main`; a number or none means main.
REF=$(sed -n 's/^engine: *"\{0,1\}\([^" #]*\).*/\1/p' "$TEAM/factory.project.yaml" "$TEAM/factory.descriptor.yaml" 2>/dev/null | head -n 1)
case "$REF" in
  main|"") REF=main ;;
  *[!0-9]*)
    if git check-ref-format "refs/tags/$REF"; then REF="refs/tags/$REF"; else REF=main; fi
    ;;
  *) REF=main ;;
esac
if ! { git worktree remove --force "$HOME/factory-main" 2>/dev/null || rm -rf "$HOME/factory-main"; } ||
  ! git fetch --depth 1 origin "$REF" ||
  ! git worktree add --detach "$HOME/factory-main" FETCH_HEAD ||
  ! uv tool install --force "$HOME/factory-main"; then
  echo "startup: factory install failed" >&2
  exit 1
fi
# Skills go to the user-level directories the harnesses scan at session start (Codex, Claude). HOME is not kept
# across a resume, so this runs on every launch. The team's skills/<name> replaces the engine's skills/vendor/<name>.
for s in $(skill_names); do
  case "$s" in
    ""|.|..|*/*) echo "startup: invalid skill name: $s" >&2; exit 1 ;;
  esac
  src="$TEAM/skills/$s"
  [ -d "$src" ] || src="$ENGINE/skills/vendor/$s"
  for d in "$HOME/.agents/skills" "$HOME/.claude/skills"; do
    mkdir -p "$d"
    rm -rf "${d:?}/$s"
    cp -R "$src" "$d/$s"
  done
done
echo "startup: installed skills: $(skill_names)"
for t in $TOOLS; do
  uv tool install --quiet "$t" || { echo "startup: tool install failed: $t" >&2; exit 1; }
done
for setup in "$FACTORY_TEAM/environments/setup.sh" "$FACTORY_TEAM/environments/${FACTORY_ENV:-default}/setup.sh"; do
  if [ -f "$setup" ]; then
    bash "$setup" || { echo "startup: environment setup failed: $setup" >&2; exit 1; }
  fi
done
for r in $ROLES; do
  bash "$(resolve "roles/$r/setup.sh")" >&2 || echo "startup: tools of role $r are missing" >&2
done
# `light` and `probe` have no approver tools.
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

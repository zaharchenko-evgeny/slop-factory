#!/bin/sh
# Air environment setup: runs from the team repository checkout on every launch and resume.
# Idempotent: a launch from a snapshot already has the tools. It installs nothing specific to one team:
# put team tools in environments/setup.sh (every environment) or environments/<env>/setup.sh.
# FACTORY_DRY=1 prints the skills it would install and exits; `factory` must then be on PATH, and
# FACTORY_ENGINE_DIR (default $HOME/factory-main) names an engine checkout for roles and skills the team does not own.
set -e
TEAM=$(pwd -P)
FACTORY_TEAM=$TEAM
export FACTORY_TEAM
FACTORY_ENV=${FACTORY_ENV:-default}
ENGINE=${FACTORY_ENGINE_DIR:-$HOME/factory-main}
BIN="$HOME/.local/bin"
export PATH="$BIN:$PATH"
# The first value of a top-level key of factory.project.yaml.
descriptor_value() {
  sed -n "s/^$1: *\"\{0,1\}\([^\" #]*\).*/\1/p" "$TEAM/factory.project.yaml" 2>/dev/null | head -n 1
}
# A role's file: the team's own, else the engine's template.
role_file() {
  if [ -f "$TEAM/roles/$1/$2" ]; then
    printf '%s\n' "$TEAM/roles/$1/$2"
  elif [ -f "$ENGINE/template/roles/$1/$2" ]; then
    printf '%s\n' "$ENGINE/template/roles/$1/$2"
  else
    echo "startup: missing file roles/$1/$2" >&2
    return 1
  fi
}
env_roles() {
  factory env roles "$FACTORY_ENV"
}
skill_names() {
  for r in $ROLES; do
    cat "$(role_file "$r" skills.txt)"
  done | sort -u | tr '\n' ' '
}
if [ -n "${FACTORY_DRY:-}" ]; then
  ROLES=$(env_roles)
  echo "startup: env=$FACTORY_ENV roles=$(echo "$ROLES" | tr '\n' ' ')"
  echo "startup: would install skills: $(skill_names)factory-step"
  exit 0
fi
mkdir -p "$BIN"
# shellcheck disable=SC2016
grep -q '.local/bin' "$HOME/.bashrc" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
# Every download is pinned to a version and a SHA-256; a mismatch fails the script (set -e), nothing is installed.
UV_URL=https://github.com/astral-sh/uv/releases/download/0.5.11/uv-x86_64-unknown-linux-gnu.tar.gz
UV_SHA=14411de26cdea5f5139fafaf2b675b1c633e744dd49c6d6a9fc8817ec065158b
GH_URL=https://github.com/cli/cli/releases/download/v2.62.0/gh_2.62.0_linux_amd64.tar.gz
GH_SHA=41c8b0698ad3003cb5c44bde672a1ffd5f818595abd80162fbf8cc999418446a
fetch() {
  TMP=$(mktemp -d)
  curl -fsSL "$1" -o "$TMP/pkg.tgz"
  if command -v sha256sum >/dev/null 2>&1; then SUM="sha256sum -c -"; else SUM="shasum -a 256 -c -"; fi
  echo "$2  $TMP/pkg.tgz" | $SUM >/dev/null 2>&1 || { echo "startup: checksum mismatch for $1" >&2; exit 1; }
}
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
# The engine, at the project's `engine` ref, from its `engine_repository`.
REF=$(descriptor_value engine)
case "$REF" in
  main|"") REF=main ;;
  *[!0-9]*) if git check-ref-format "refs/tags/$REF"; then :; else REF=main; fi ;;
  *) REF=main ;;
esac
ENGINE_REPOSITORY=$(descriptor_value engine_repository)
ENGINE_REPOSITORY=${ENGINE_REPOSITORY:-https://github.com/zaharchenko-evgeny/slop-factory.git}
install_engine() {
  if [ -d "$ENGINE/.git" ]; then
    git -C "$ENGINE" fetch --depth 1 "$ENGINE_REPOSITORY" "$REF" || return 1
    git -C "$ENGINE" checkout --detach FETCH_HEAD || return 1
  else
    git clone --depth 1 --branch "$REF" "$ENGINE_REPOSITORY" "$ENGINE" || return 1
  fi
  uv tool install --force "$ENGINE"
}
if ! install_engine; then
  echo "startup: factory install failed" >&2
  exit 1
fi
# The agent's shell does not inherit this script's exports: the factory command carries the team itself.
if [ -e "$BIN/factory" ] && ! grep -q 'factory-team-wrapper' "$BIN/factory" 2>/dev/null; then
  mv -f "$BIN/factory" "$BIN/.factory-engine"
fi
cat > "$BIN/factory" <<WRAPPER
#!/bin/sh
# factory-team-wrapper: written by .air/cloud/startup.sh
FACTORY_TEAM=\${FACTORY_TEAM:-$TEAM}
export FACTORY_TEAM
exec "$BIN/.factory-engine" "\$@"
WRAPPER
chmod +x "$BIN/factory"
ROLES=$(env_roles)
install_skill() {
  for d in "$HOME/.agents/skills" "$HOME/.claude/skills"; do
    mkdir -p "$d"
    rm -rf "${d:?}/$2"
    cp -R "$1" "$d/$2"
  done
}
# A role's skills come from the team's skills/, else the engine's template.
for s in $(skill_names); do
  case "$s" in
    ""|.|..|*/*) echo "startup: invalid skill name: $s" >&2; exit 1 ;;
  esac
  if [ -f "$TEAM/skills/$s/SKILL.md" ]; then
    install_skill "$TEAM/skills/$s" "$s"
  elif [ -f "$ENGINE/template/skills/$s/SKILL.md" ]; then
    install_skill "$ENGINE/template/skills/$s" "$s"
  else
    echo "startup: missing skill: $s" >&2
    exit 1
  fi
done
# The engine's own skill is the same for every team: factory-step tells each role how to report its outcome.
if [ -f "$ENGINE/skills/factory-step/SKILL.md" ]; then
  install_skill "$ENGINE/skills/factory-step" factory-step
else
  echo "startup: engine ref $REF has no skill factory-step" >&2
fi
echo "startup: installed skills: $(skill_names)factory-step"
for r in $ROLES; do
  bash "$(role_file "$r" setup.sh)" >&2 || echo "startup: tools of role $r are missing" >&2
done
for setup in "$TEAM/environments/setup.sh" "$TEAM/environments/$FACTORY_ENV/setup.sh"; do
  if [ -f "$setup" ]; then
    bash "$setup" || { echo "startup: environment setup failed: $setup" >&2; exit 1; }
  fi
done

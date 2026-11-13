#!/usr/bin/env bash
# Tools of the environments that name this script (`setup:` in factory.yaml). `factory setup` runs it from the team
# root after the skills install. Add the environment's other tools here.
# Node 22.18+ and Java 11+ run `factory check-workflow` (the statechart skill and TLC) and its tests.
# Pinned versions; idempotent (a resumed run has them); stops with a clear message when an install fails.
set -euo pipefail

fail() { echo "environments setup: $*" >&2; exit 1; }

bin="$HOME/.local/bin"
mkdir -p "$bin"

# Node from PyPI: Air's domains allow pypi.org, not nodejs.org.
# A Node the statechart skill accepts (22.18 to 22.x, or 23.6+) is kept; otherwise `--force` replaces the one in the
# bin directory.
node_ok() {
  command -v node >/dev/null 2>&1 || return 1
  v=$(node --version 2>/dev/null | sed -n 's/^v\([0-9]*\)\.\([0-9]*\).*/\1 \2/p' | { read -r a b; echo $(( ${a:-0} * 1000 + ${b:-0} )); })
  [ "$v" -ge 23006 ] || { [ "$v" -ge 22018 ] && [ "$v" -lt 23000 ]; }
}
node_ok || uv tool install --quiet --force nodejs-wheel==22.20.0 || fail "tool install failed: nodejs-wheel 22.20.0"
node_ok || fail "node on PATH is $(command -v node) $(node --version 2>/dev/null); put $(uv tool dir --bin) first"

# Java: a Java 11+ on PATH is kept; otherwise the Temurin JRE from GitHub releases (linux only).
java_major() {
  command -v java >/dev/null 2>&1 || { echo 0; return; }
  v=$(java -version 2>&1 | sed -n '1s/.*version "\([0-9]*\).*/\1/p')
  echo "${v:-0}"
}
if [ "$(java_major)" -lt 11 ]; then
  [ "$(uname -s)" = Linux ] || fail "Java 11+ not found; install it (the Temurin download is linux only)"
  case "$(uname -m)" in
    x86_64) arch=x64 sha=a535a1df9c6284d9618872984e026ae0352d1f7e9e0605425c0e6114f47351dd ;;
    aarch64) arch=aarch64 sha=b1734db7ade49c3f6acc6ae06233b8c2dd90e3e2f00f67000ae1c972aaa64d8c ;;
    *) fail "no pinned Temurin JRE for $(uname -m)" ;;
  esac
  home="$HOME/.local/share/temurin-jre-21.0.13"
  if [ ! -x "$home/bin/java" ]; then
    jtmp="$(mktemp -d)"
    url="https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.13%2B9/OpenJDK21U-jre_${arch}_linux_hotspot_21.0.13_9.tar.gz"
    curl -fsSL --retry 3 -o "$jtmp/jre.tgz" "$url" || fail "download failed: Temurin JRE 21.0.13"
    echo "$sha  $jtmp/jre.tgz" | sha256sum -c --quiet - || fail "sha256 mismatch: Temurin JRE 21.0.13"
    mkdir -p "$jtmp/x"
    tar -xzf "$jtmp/jre.tgz" -C "$jtmp/x" --strip-components=1 || fail "unpack failed: Temurin JRE"
    rm -rf "$home"
    mkdir -p "$(dirname "$home")"
    mv "$jtmp/x" "$home"
    rm -rf "$jtmp"
  fi
  ln -sfn "$home/bin/java" "$bin/java"
fi
[ "$(java_major)" -ge 11 ] || fail "java on PATH is $(command -v java), not Java 11+; put $bin first"

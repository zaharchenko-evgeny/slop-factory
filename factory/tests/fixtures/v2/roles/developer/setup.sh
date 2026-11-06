#!/usr/bin/env bash
# Tools the developer runs before review. Pinned versions; idempotent (a resumed run has them).
set -euo pipefail
for t in shellcheck-py==0.10.0.1 jactionlint==2.3.0; do
  uv tool install --quiet "$t" || { echo "developer setup: tool install failed: $t" >&2; exit 1; }
done

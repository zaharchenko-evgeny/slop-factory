#!/usr/bin/env bash
# Checks that the distill role can run locally. Run from the repository root.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=template/roles/_setup-common.sh
. "$here/../_setup-common.sh"
need_tools git
need_skills "$here/skills.txt"
echo "distill: ready"

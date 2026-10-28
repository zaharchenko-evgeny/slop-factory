#!/usr/bin/env bash
# Checks that the router role can run locally. Run from the repository root.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=roles/_setup-common.sh
. "$here/../_setup-common.sh"
need_tools git gh pi
need_skills "$here/skills.txt"
echo "router: ready"

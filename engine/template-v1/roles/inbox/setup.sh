#!/usr/bin/env bash
# Checks that the inbox role can run locally. Run from the repository root.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=engine/template-v1/roles/_setup-common.sh
. "$here/../_setup-common.sh"
need_tools git gh
need_skills "$here/skills.txt"
echo "inbox: ready"

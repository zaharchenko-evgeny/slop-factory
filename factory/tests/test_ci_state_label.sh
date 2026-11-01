#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/gh" <<'MOCK'
#!/usr/bin/env bash
set -euo pipefail
state=${GH_MOCK_STATE:?}
case "$*" in
  'pr list '*) printf '7\n' ;;
  'pr view '*) printf '%s\n' "$(cat "$state/label" 2>/dev/null || true)" ;;
  *'commits/'*'/status --jq'*)
    sha=${2#*commits/}; sha=${sha%%/*}
    cat "$state/$sha" 2>/dev/null || true
    ;;
  *'statuses/'*)
    sha=${2#*statuses/}
    last=${*: -1}
    printf '%s\n' "${last#description=}" > "$state/$sha"
    ;;
  *'--add-label'*)
    label=${*: -1}
    printf '%s\n' "$label" > "$state/label"
    echo add >> "$state/events"
    ;;
  *'--remove-label'*) rm -f "$state/label" ;;
  *) echo "unexpected gh command: $*" >&2; exit 1 ;;
esac
MOCK
chmod +x "$tmp/gh"
export PATH="$tmp:$PATH" GH_MOCK_STATE="$tmp/state" GH_REPO=owner/repo BRANCH=ez/1-thing CONCLUSION=success
mkdir "$tmp/state"
export SHA=first
"$root/scripts/ci-state-label.sh" >/dev/null
"$root/scripts/ci-state-label.sh" >/dev/null
[ "$(wc -l < "$tmp/state/events")" -eq 1 ]
export SHA=second
"$root/scripts/ci-state-label.sh" >/dev/null
[ "$(wc -l < "$tmp/state/events")" -eq 2 ]
echo 'same-head rerun adds once; new head adds once'

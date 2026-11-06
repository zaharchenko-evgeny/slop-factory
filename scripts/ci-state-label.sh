#!/usr/bin/env bash
set -euo pipefail

# GitHub returns transient errors now and then: retry each gh call, 3 attempts, waits 5 s and 15 s.
gh() {
  local waits n=0
  read -ra waits <<< "${CI_LABEL_WAITS:-5 15}"
  until command gh "$@"; do
    if [ "$n" -ge "${#waits[@]}" ]; then
      echo "::error title=ci-state-label::gh $1 $2 failed after $((n + 1)) attempts (label step error, not a failed check)" >&2
      return 1
    fi
    echo "gh $1 $2 failed, retrying in ${waits[$n]} s" >&2
    sleep "${waits[$n]}"
    n=$((n + 1))
  done
}

pr=$(gh pr list --head "$BRANCH" --state open --json number,headRefOid --jq ".[] | select(.headRefOid == \"$SHA\") | .number")
if [ -z "$pr" ]; then echo "no open PR at $SHA (head moved or PR closed): nothing to label"; exit 0; fi
case "$CONCLUSION" in
  success|neutral) want=factory:ci-green ;;
  cancelled|skipped) echo "conclusion $CONCLUSION: no result"; exit 0 ;;
  *) want=factory:ci-red ;;
esac
labels=$(gh pr view "$pr" --json labels --jq '[.labels[].name] | join(",")')
has_want=false
has_other=false
case ",$labels," in *",$want,"*) has_want=true ;; esac
other=factory:ci-green
[ "$want" = factory:ci-green ] && other=factory:ci-red
case ",$labels," in *",$other,"*) has_other=true ;; esac
published=$(gh api "repos/$GH_REPO/commits/$SHA/status" --jq '[.statuses[] | select(.context == "factory-ci-label")][0].description // ""')
if [ "$has_want" = true ] && [ "$published" = "$want" ]; then
  if [ "$has_other" = true ]; then gh pr edit "$pr" --remove-label "$other" >/dev/null; fi
  echo "PR #$pr at $SHA already has $want: nothing to change"
  exit 0
fi
# Re-adding the same label fires no event; clear the old result before recording this head or result.
if [ "$has_other" = true ] || [ "$has_want" = true ]; then
  gh pr edit "$pr" --remove-label "$other" --remove-label "$want" >/dev/null 2>&1 || true
fi
gh pr edit "$pr" --add-label "$want"
gh api "repos/$GH_REPO/statuses/$SHA" -f state=success -f context=factory-ci-label -f description="$want" >/dev/null
echo "PR #$pr at $SHA: $want"

#!/usr/bin/env bash
set -euo pipefail

pr=$(gh api "repos/$GH_REPO/pulls?state=open&head=${GH_REPO%/*}:$BRANCH" --jq ".[] | select(.head.sha == \"$SHA\") | .number")
if [ -z "$pr" ]; then echo "no open PR at $SHA (head moved or PR closed): nothing to label"; exit 0; fi
case "$CONCLUSION" in
  success|neutral) want=factory:ci-green ;;
  cancelled|skipped) echo "conclusion $CONCLUSION: no result"; exit 0 ;;
  *) want=factory:ci-red ;;
esac
labels=$(gh api "repos/$GH_REPO/issues/$pr/labels" --jq '[.[].name] | join(",")')
has_want=false
has_other=false
case ",$labels," in *",$want,"*) has_want=true ;; esac
other=factory:ci-green
[ "$want" = factory:ci-green ] && other=factory:ci-red
case ",$labels," in *",$other,"*) has_other=true ;; esac
published=$(gh api "repos/$GH_REPO/commits/$SHA/status" --jq '[.statuses[] | select(.context == "factory-ci-label")][0].description // ""')
if [ "$has_want" = true ] && [ "$published" = "$want" ]; then
  if [ "$has_other" = true ]; then gh api -X DELETE "repos/$GH_REPO/issues/$pr/labels/$other" >/dev/null; fi
  echo "PR #$pr at $SHA already has $want: nothing to change"
  exit 0
fi
# Re-adding the same label fires no event; clear the old result before recording this head or result.
if [ "$has_other" = true ] || [ "$has_want" = true ]; then
  for label in "$other" "$want"; do
    case ",$labels," in *",$label,"*) gh api -X DELETE "repos/$GH_REPO/issues/$pr/labels/$label" >/dev/null ;; esac
  done
fi
gh api -X POST "repos/$GH_REPO/issues/$pr/labels" -f "labels[]=$want" >/dev/null
gh api "repos/$GH_REPO/statuses/$SHA" -f state=success -f context=factory-ci-label -f description="$want" >/dev/null
echo "PR #$pr at $SHA: $want"
if [ "$want" = factory:ci-green ]; then
  ticket=${BRANCH#ez/}
  ticket=${ticket%%-*}
  uv run --quiet factory/gate.py wake-reviewers --issue "$ticket"
fi

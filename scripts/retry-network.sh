#!/bin/bash
# Usage: retry-network.sh <command...>. Runs the command up to 3 times, 20 s apart, only when its output shows a
# transient network failure (curl 6/7/22 with 5xx/28/35/56, npm ETIMEDOUT/ECONNRESET/E5xx); any other failure is final.
set -uo pipefail
network='curl: \((6|7|28|35|56)\)|curl: \(22\).*(error: )?5[0-9][0-9]|ETIMEDOUT|ECONNRESET|\bE5[0-9][0-9]\b'
log=$(mktemp)
trap 'rm -f "$log"' EXIT
for attempt in 1 2 3; do
  "$@" 2>&1 | tee "$log"
  status=${PIPESTATUS[0]}
  [ "$status" -eq 0 ] && exit 0
  grep -qE "$network" "$log" || exit "$status"
  [ "$attempt" -lt 3 ] && { echo "retry-network: network failure, retry $attempt/2 in 20 s" >&2; sleep 20; }
done
exit "$status"

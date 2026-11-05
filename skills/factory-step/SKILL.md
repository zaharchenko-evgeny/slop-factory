---
name: factory-step
description: How any factory role starts its step with `factory begin` and reports its outcome, with the outcome's data, through `factory end`. Use whenever a role file tells you to report an outcome.
---

# Factory step

You never change labels, stages, reviews or comments yourself. You report one
outcome; the gate checks it and moves the work on.

1. `factory begin` (the prompt gives its arguments) prints the facts, the
   branch and `outcomes`, the outcomes this step may report. Exit 10: do not
   act; report its `reason` and stop.
2. As the last thing you do, run `factory end <outcome>` once, with the data
   the role file asks for. Quote each value; repeat a list option per item:

| Data | Option |
| --- | --- |
| reason, summary, size estimate | `--reason "…"`, `--summary "…"`, `--size-estimate "~120"` |
| failing check on `origin/main` | `--failing-check "<check>"` with `env-broken` |
| acceptance check result | `--pass "<check>"`, `--fail "<check>"`, `--not-run "<check> \| <why>"` |
| finding | `--blocking "<id> <file:line> <finding> \| case: <repro>"`, `--minor "<id> <file:line> <finding>"` |
| finding fixed or disputed | `--fixed "<id> <sha>"`, `--disputed "<id> <reason>"`, `--disputed-focuses a,b` |
| new ticket (slice) | `--slice <file.md>`: first line the title, the rest the body |
| proposed acceptance check | `--propose-check "<check>"` |
| note for a later agent | `--note-for <role\|any>` with `--note-hint "…"`, `--note-repro "<cmd>"`, `--note-file <path>` (≤2,000 chars, no secrets) |

Example: `factory end <outcome> --summary "…" --blocking "B1 src/x.py:10 crashes on '' | case: x('') raises"`

If `factory end` refuses (non-zero exit, error printed), fix the data and run
it again. Never change labels instead.

When a required check fails the same way on `origin/main`, report
`factory end env-broken --failing-check <check>`.

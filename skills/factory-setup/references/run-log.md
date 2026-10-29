# Run log and report

`<run_dir>/<ticket>/events.log` is JSON lines. Every line has `ts` (ISO 8601
UTC), `run` (the run id) and `event`:

| event | fields |
|---|---|
| `usage` | step, role, model, input, output, cacheRead, cacheWrite, cost (one per model call; `null` = unknown) |
| `step.start` | step, role, model, attempt |
| `step.end` | step, role, model, attempt, duration_s, outcome (`ok`, `empty`, `bad-handoff`) |
| `fallback` | step, role, from, to |
| `retry` | action, count (the engine reran an action) |
| `transition` | from, on (the fired event), to, note |
| `wait` | what (`ci`, `merge`), duration_s |
| `human` | what (`merge`), decision, approver (the approver's verdict) |

`loop.log` lines start with `<ISO timestamp> cycle=<cycle id> `.

`drive.py report [--since DATE] [--ticket N] [--json]` prints per ticket the
final state, wall time (first to last event), active agent time (sum of
`step.end`), wait time, cost by role and model, fix rounds, retries, fallbacks
and blocked/failed reasons, then a total line. Logs from before this schema
hold `usage` lines only: their cost counts, their durations print `?`
(`null` in JSON), never 0.

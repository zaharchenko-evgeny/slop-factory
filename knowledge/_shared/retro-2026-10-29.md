# Retro: 28–29 October 2026

Scope: the first two days of the factory building itself, from the design to
unattended scheduled runs. Numbers come from `.factory/runs/*/events.log`, run
files and pi session logs; wall times are from file timestamps.

## Numbers

Unattended runs since lean prompts (#51–#76, 9 tickets): $12.71.

| Ticket | Cost | Fix rounds | Note |
| --- | --- | --- | --- |
| #68 scheduler | $0.14 | 1 | Sonnet developer |
| #72 keychain | $0.52 | 1 | |
| #74 central | $0.95 | 0 | CI job stuck 32 min |
| #51 doc-writer | $0.98 | 0 | |
| #76 harnesses | $1.00 | 1 | Opus stream broke, pi hung 25 min |
| #59 merged state | $1.16 | 0 | |
| #53 distill | $1.69 | 1 | |
| #61 loop | $1.76 | 0 | |
| #52 retro | $4.51 | 2 + restart | blocked on a design conflict |

- By role: developer $11.93 (94%), reviewers $0.66, router, doc-writer and retro $0.12.
- By model: Opus $10.12 (80%), Sonnet $2.45, Haiku $0.15, Qwen $0.
- Extra attempts: about 30 (behaviour review 10, structure-security 5, implement 5, diagnose 5).
- Fallbacks: Qwen → Haiku in 10 of about 13 behaviour reviews; Sonnet 3; Opus 3.
- Whole effort: coordinating session about $248, Opus orchestrator subagents up to $118, developer sessions before lean prompts about $21, unattended runs about $13. About 90% went into coordination.

## What went well

- Tracker stages as states, a deterministic engine, claims, risk-based reviewers and the human merge gate held up.
- The factory built itself: 30 tickets through ticket → PR → CI → merge, the last ten unattended; since #59 the machine also sets `done`.
- The Sonnet structure-security reviewer found real defects five times (forged vars, `base:` command injection, lost verdicts, missing labels, git write access).
- Per-role cost records and lean prompts (first-turn cache 178k → 11k tokens).
- Gates stopped a real design conflict (#52) instead of guessing.

## What went badly

- Long interactive coordination and Opus orchestrators cost far more than the work.
- Handoff format failures from every model were the main source of retries; Qwen rarely produced a valid tool-using review.
- `risk:high` sent most tickets to Opus.
- Defects found only in live runs: ignored splits, no role timeout, pending CI treated as approved, missing stage label, non-atomic `set_state`, loop not checking claims (#56).
- Two PRs merged in parallel broke `main` once (#48).
- Human-gate latency and approver accuracy were not measured.

## Improvements

| Rank | Change | Status |
| --- | --- | --- |
| 1 | Short coordinating sessions; the scheduler drives; status from a report command | practice |
| 2 | Only Haiku 5.5 and Sonnet 5.5 | done in #79 |
| 3 | Behaviour reviewer on Haiku instead of Qwen | done in #79 |
| 4 | Structured handoffs written to a file | ticket |
| 5 | Driver gaps | #56 |
| 6 | Timestamped run log and `drive.py report` | ticket |
| 7 | Record human merge decisions next to the approver verdict to calibrate it | after the run log |

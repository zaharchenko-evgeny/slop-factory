# Lessons: offline-eval-factory

## Automated checks

- Pin reviewer agreement with a test using two reviewers over two rounds, and test that a retro without a handoff records failed, not done with lessons unknown (#52).
- Add a test that drives `loop` through the distill wiring, not only `distill_due`, and name tests for what they cover (#53, `test_loop_runs_distill_once_per_interval`).
- Assert every `roles/*` entry has a `harness` key in both `factory.yaml` files; implement missed it for distill (#53).
- Run the merge-with-base check before review, not only at `approved`; a conflict with main surfaced only in CI and cost a fix round (#53).
- Add central-mode tests: the "model access: ..." failure in test_drive.py and the one-line FAIL output of the preflight.py main path (#74).
- Block `approved` while a ticket acceptance check is not done, or list it as an explicit human gate at intake; #74 reached approved with its live-cycle check unmet (#74).
- Publish the ci handoff only after all CI checks finish; #74's handoff said passed while the ticket comment showed pending after 1800s (#74).
- Retro entries from #68 and #72 recorded "retro gave no handoff"; the retro role's handoff failure is unresolved (#68, #72, #52).

## Coding standards

- Add a line-length check to the pre-review checks; two reviewers flagged over-long lines in factory/drive.py that whitespace and shellcheck miss (#52).
- Fence inbox entries with a fence longer than any run inside the entry. Use `git status --porcelain -z` or `--no-renames` in the outside-dir guard (#53).
- Record the distill attempt only after a successful run, so a failed run retries next loop (#53, `distill_due`).
- Serialise `jbcentral proxy start` with a lock, since parallel reviewer threads can both start it. Reject `model_access` values other than central|env, because a typo like "Central" falls back to env silently (#74).

## Information access

- Have intake check ticket acceptance wording against the workflow's state rules (actions run only in agent states) before delivery; #52 hit a scope conflict only a human could resolve.
- Give router intake the changed-line count of similar work, or count tests in the estimate; #74 estimated ~150 lines, the diff was 254 and tripped the approver's >200 gate (#74).

## No-ops and routing

- Route an out-of-scope blocking finding straight to a human instead of spending a fix round; #52 fix round 2 ran with no change and ended blocked.

## AGENTS.md

- List docs/design.md and docs/adr/ in a ticket's affected files when a change alters a documented flow; in #52 design.md and ADR-0007 stayed stale and the doc-writer ran with files none.

## Open questions

- #53: the approver verdict comment was posted twice with identical text; unconfirmed whether a manual rerun caused it.

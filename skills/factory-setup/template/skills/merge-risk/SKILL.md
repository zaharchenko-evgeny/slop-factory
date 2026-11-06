---
name: merge-risk
description: "Use when assessing the risk of merging a factory PR: score alignment, impact, verification, rollback cost and error detection, and say what to fix or what a person must check."
metadata:
  credits:
    skill: merge-risk
    author: Warp
    url: "https://github.com/warpdotdev/common-skills/blob/main/.agents/skills/merge-risk/SKILL.md"
---

Score the diff against the base branch, not the PR text. Read the ticket for alignment.

Output format (put it first in your summary or PR body):

```
**Merge risk: <low|medium|high>**

**Alignment:** <verdict>
- <gap that raises risk, with file:line>
**Impact:** <verdict>
**Verification:** <verdict>
**Rollback cost:** <verdict>
**Error detection:** <verdict>
```

List gaps only under a category whose verdict is not ✅. Keep each gap to one line.

## Categories

A ✅ verdict means low risk for that category.

- **Alignment (poor ⚠️ / decent 👀 / good ✅):** the diff does what the ticket asks and nothing else.
  Drive-by changes and behaviour assumptions the ticket does not state are gaps.
- **Impact (large ⚠️ / medium 👀 / small ✅):** the blast radius. Sensitive areas carry outsized risk at any
  size: the gate and its effects, outcome and guard vocabulary, deploy and Air automations, schemas,
  `.github/**`, secrets and tokens, tracker writes (labels, stages, comments), merge logic, and anything a
  later factory run reads. Flag every one touched, and call out anything that widens access, weakens a
  check, or logs a secret.
- **Verification (poor ⚠️ / decent 👀 / good ✅):** the behaviour is shown to work. A behaviour change needs
  before-and-after evidence: the test that failed and now passes, or command output that changed
  (`factory decide --dry-run`, a deploy plan, a fixture comparison). Untested edge cases are gaps unless the
  PR names them. Unit tests alone for a gate or deploy change are at best 👀.
- **Rollback cost (high ⚠️ / medium 👀 / low ✅):** one-way or two-way door. A plain revert undoes a two-way
  door. One-way doors: deleting or renaming Air automations, environments or labels; rewriting stage history;
  data or format migrations; a change that needs the team repository merged before the engine; anything
  already published outside the repository.
- **Error detection (poor ⚠️ / decent 👀 / good ✅ / N/A ✅):** if this breaks, does someone find out before a
  ticket silently stalls? Good: the gate refuses with a reason, `doctor` or CI fails, `notify` reaches the
  owner. Poor: swallowed errors, a skipped poll that reports nothing, a fallback that hides breakage.

## Score

Verdict values: ⚠️ = 3, 👀 = 2, ✅ = 1. Weights: impact 2, verification 2, rollback cost 1.5, error
detection 1, alignment 1. Score = Σ(value × weight) / 7.5.

- low: < 1.5
- medium: 1.5 to < 2.25
- high: ≥ 2.25

Any ⚠️ in rollback cost makes the result at least medium. Keep the number and its arithmetic out of the
output.

## Lowering the risk (when you wrote the change)

Before handing off, fix what you can: drop changes the ticket did not ask for, make new failure paths
report (a gate refusal, an error, a comment) instead of passing silently, add the missing before/after
evidence, and avoid a sensitive path when another path reaches the same result. When impact stays large,
propose a split instead.

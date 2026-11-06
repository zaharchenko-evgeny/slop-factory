---
name: pr
description: "Use when writing a PR body (on Air, your final message becomes the PR body)."
metadata:
  credits:
    skill: pr
    author: Matt Pocock
    url: "https://github.com/mattpocock/skills/blob/main/skills/engineering/pr/SKILL.md"
    based_on: "https://github.com/humanlayer/skills/blob/main/plugins/show-me/skills/show-me/SKILL.md"
    review_guide_from: "https://github.com/warpdotdev/common-skills/blob/main/.agents/skills/write-pr-description/SKILL.md"
---

Write the PR body with this template. Skip preambles; keep prose brief; use the repository's own terms.

```markdown
Closes #<ticket>   (or `Part of #<ticket>` when the PR does not finish it)

## Summary

<one or two sentences, then the smallest sketch that shows the change>

## Review guide   (only when warranted, see below)

## Evidence

- **Before:** <failing test, old output, or the wrong behaviour>
  **After:** <passing test, new output>
- <each acceptance check: the command and its result>

## Merge danger

**Door:** <one-way or two-way>, <why in one line>
**Blast radius:** <one word>, <what could break>
```

## Summary

Pick the smallest view that makes the point; often one is enough:

- logic as pseudocode;
- runtime control flow as a call tree (`gate apply → effects → tracker`);
- a workflow change as a node or outcome sketch;
- a broad refactor as a shallow file tree with one comment per entry;
- a `diff` block of the shape when the surrounding structure already exists:

```diff
 review-high
   outcomes:
-    approve: automerge
+    approve-low: automerge
+    approve-risky: needs-human
```

Keep only the calls, files, nodes and states needed to see the change.

## Evidence

Show that the change works, before and after. Execution is the evidence: the exact test that failed and now
passes, or the command output that changed. Name a check you could not run and why. Do not paste long logs;
quote the lines that matter.

## Review guide

Add it when the reading order is not obvious, when you want attention somewhere specific, or when the change
can break things well beyond the files it touches; skip it when a competent reviewer knows where to look.
It holds some of: a reading order (files or commits) for a PR large enough that order matters; focus areas,
meaning what you are unsure of, security-relevant surfaces, decisions with a real alternative, and genuine
one-way doors. Point at the code and the open question; do not explain the code.

## Merge danger

A two-way door is undone by a plain revert. A one-way door is not: deleting or renaming automations or
labels, stage-history or format migrations, changes published outside the repository, or an order the
repositories must merge in. The blast radius is who or what breaks if the change is wrong (every ticket in
flight, one node, deploy only, docs only).

## Self-check

Before you finish: every claim in the body is one you checked by looking (a command, the code), not one you
inferred or copied from a commit message; nothing repeats what the diff already shows; no section is padding.
Cut a sentence that does neither.

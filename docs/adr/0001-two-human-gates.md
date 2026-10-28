# ADR-0001: Two human gates

Status: accepted (28 Oct 2026)

Context: the handoff requires approval of every text item (commit, PR, comment). An unattended factory cannot work under that rule.

Decision: for factory-run work, humans approve only (1) the ticket after design and (2) the PR. Agents write commit messages, PR text and review replies. Auto-approval (ADR-0005) can replace gate 2 for low-risk PRs.

Consequence: this is an explicit change to the handoff policy, limited to factory-run work. Manual work still follows the handoff rule.

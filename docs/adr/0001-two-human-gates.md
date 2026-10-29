# ADR-0001: Two human gates

Status: accepted (28 Oct 2026)

Context: the handoff requires approval of every text item (commit, PR, comment). An unattended factory cannot work under that rule.

Decision: for factory-run work, humans approve only (1) the ticket after design and (2) the PR. Agents write commit messages, PR text and review replies. Auto-approval (ADR-0005) can replace gate 2 for low-risk PRs.

Consequence: this is an explicit change to the handoff policy, limited to factory-run work. Manual work still follows the handoff rule.

Amendment (#91): gate 2 is a human merge unless the auto-merge policy passes. The factory then merges the PR itself when `roles.approver.auto_merge.enabled` is true, the approval is `auto-approve`, the risk is within `max_risk`, CI passed, the ticket has no `factory:hold` label, and the branch merged with the current base still passes the blocking checks. It is off by default; any other case still ends at the human gate.

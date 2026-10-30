# ADR-0011: Enforce write limits in Air, not in prompts

Status: accepted (28 Oct 2026). The enforcement part is superseded by ADR-0013: `read-only` does not limit labels, so the step script is the gate.

Context: all automations run under the same Air account and tokens. A role boundary written in a prompt does not stop a role from writing.

Decision: keep the shared account and tokens. Limit writes through Air `outputBehaviour`: reviewer, approver, auditor and ops use `read-only`. Event triggers always filter narrowly (branch prefix, label or tag).

Consequence: a misbehaving read-only role cannot push code. External writes through tokens (comments, tags) are still limited only by prompts and are audited through run reports.

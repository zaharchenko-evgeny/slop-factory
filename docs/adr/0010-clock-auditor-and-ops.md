# ADR-0010: A clock — nightly auditor and daily ops digest

Status: accepted (28 Oct 2026)

Context: the "front door, specialists, board, clock, gate" model (distort, "Grok Bot: How to Build Your First AI Team") showed that our design had no clock. Work moved only when an event arrived, and nobody reported the overall state.

Decision: add two scheduled, read-only roles. The auditor runs advisory checks nightly and drafts tickets. ops posts a daily digest, pings on `blocked`, flags stuck workflows and proposes allow-rule promotions. A scheduled routine never waits at a human checkpoint; it ends at a draft and sets `blocked`.

Consequence: adds the `blocked` tracker stage and two schedules. Roles are enabled in phases.

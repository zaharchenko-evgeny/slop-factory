# ADR-0005: Auto-approval through hard rules and a decisions-kit judgment

Status: accepted (28 Oct 2026)

Decision: a separate approver role, which never sees the developer's reasoning. A deterministic script checks the hard rules first. decisions-kit `decide` answers the soft questions; any `host` result means a human decides. Dry-run mode until the judgment is calibrated on about ten real decisions.

Consequence: depends on decisions-kit (pinned version) and on per-repo branch protection.

# ADR-0012: Cost and quality levers

Status: accepted (28 Oct 2026)

Context: the existing harbor-infra review automation ran three times in about an hour on one PR, and a run lost evidence to missing Grafana and TeamCity auth. Fix rounds, re-reviews and mid-run environment failures are the main cost drivers; unclear tickets are the main quality risk.

Decision: a definition of ready before `ready`; two delivery paths by risk; deterministic checks and a preflight before any model runs; narrow review triggers with incremental re-review; only blocking findings start the fixer; model tiers with escalation on failure; structured handoffs instead of transcripts; per-run cost and quality metrics. Quality-gate tools come from dev-quality-gates; a check becomes blocking only after it is shown to fail. Auto-approval hard rules are Rego evaluated by Conftest.

Consequence: the factory needs a PR summary JSON, Conftest, Betterleaks and the selected gate tools in the environment image, and a usage source for cost metrics. Evaluating factory changes on Harbor tasks is a follow-up.

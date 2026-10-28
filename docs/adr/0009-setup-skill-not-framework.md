# ADR-0009: A setup skill, not a framework

Status: accepted (28 Oct 2026)

Options: a core package with a CLI; a framework and instance split; one setup skill.

Decision: one skill, `factory-setup`. It interviews a team, generates its factory files in the team's repository, and applies targets through the Air MCPs or local links after a plan the user approves. Small scripts are kept only where determinism matters (auto-approval hard rules, manifest diff).

Consequence: no package to version or install. Each team owns and customises its generated files.

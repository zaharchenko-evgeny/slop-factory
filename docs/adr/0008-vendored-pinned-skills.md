# ADR-0008: Vendor and pin external skills

Status: accepted (28 Oct 2026)

Decision: pstack and mattpocock skills are vendored at pinned commits in `skills.lock`. Each role installs only the skills in its `skills.txt`, through its own `setup.sh`, which also installs the role's tools.

Consequence: upstream updates are deliberate PRs. Duplicate names are resolved in the lock.

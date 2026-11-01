# Hard rules of auto-approval (design §8), checked by Conftest over the JSON
# summary of a PR that factory/approver.py writes:
#   {"paths": [...], "lines_changed": N, "ci": "passed", "risk": "low",
#    "reviews": [{"reviewer": "...", "verdict": "approved"}], "dependency_changes": [...]}
# Every `deny` message is a reason the PR needs a human.
# shortcut: no public-API rule yet; add one per repository when an allow rule needs it.
package main

import rego.v1

max_lines := 200

# CI, deploy, secrets and the approval gate itself.
blocked_prefixes := [".github/", "deploy/", "secrets/", "policies/", "judgments/", "factory.descriptor.yaml", ".env"]

deny contains "no reviewer report" if {
	count(input.reviews) == 0
}

deny contains msg if {
	some r in input.reviews
	r.verdict != "approved"
	msg := sprintf("reviewer %s did not approve", [r.reviewer])
}

deny contains msg if {
	input.ci != "passed"
	msg := sprintf("CI is %s, not passed", [input.ci])
}

deny contains msg if {
	input.risk != "low"
	msg := sprintf("risk is %s", [input.risk])
}

deny contains msg if {
	input.lines_changed > max_lines
	msg := sprintf("%d changed lines, more than %d", [input.lines_changed, max_lines])
}

deny contains msg if {
	some p in input.paths
	some prefix in blocked_prefixes
	startswith(p, prefix)
	msg := sprintf("touches %s", [p])
}

deny contains msg if {
	some d in input.dependency_changes
	msg := sprintf("changes dependencies in %s", [d])
}

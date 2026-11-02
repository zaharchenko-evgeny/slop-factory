# Recipe: auto-approve and auto-merge

Let the factory merge a pull request without a person when every gate passes.
This page explains the gates, how to add your own criteria, and how to turn
auto-merge on safely.

Status:

- **Exists now:** the hard rules, the LLM judge, the approver, the `auto_merge` setting, the `merge` action, the `factory:hold` label, and the `labels` and `complexity` facts (#91). `dry_run: false` is accepted with `require: hard-rules`; a level that needs `judgment` stays `needs-human` until the judgment is calibrated. `auto_merge.enabled` is `false` by default.
- `complexity` is read from the intake handoff when it reports one (#88), else it is empty.

## How the approver decides

The approver (`roles/approver/role.md`) runs after every reviewer has approved
and before the PR is published. It has three layers:

| Layer | File | What it is | Deterministic |
| --- | --- | --- | --- |
| 1. Hard rules | `policies/auto-approve.rego` | Rego `deny` rules, checked by Conftest over a JSON summary of the PR | yes |
| 2. Judge | `judgments/auto-approve.yaml` | decisions-kit questions answered by an LLM, with a confidence threshold and an abstain option | no |
| 3. Combine rule | `factory.descriptor.yaml` `roles.approver` | which layers must pass before a merge, per risk level (#91) | yes |

Any `deny` message, any judge answer other than `yes`, a missing tool, or an
unavailable engine is a reason. The verdict is `auto-approve` only when there
are no reasons. Otherwise it is `needs-human`.

## Layer 1: hard rules

The rules evaluate this summary of the branch against its base:

```json
{"paths": ["..."], "lines_changed": 42, "ci": "passed", "risk": "low",
 "reviews": [{"reviewer": "full", "verdict": "approved"}],
 "dependency_changes": [], "labels": ["..."], "complexity": "low"}
```

Today's rules deny a PR when:

- there is no reviewer report, or a reviewer did not approve
- CI did not pass
- the risk is not `low`
- more than 200 lines changed
- dependencies changed
- it touches `.github/`, `deploy/`, `secrets/`, `policies/`, `judgments/`, `factory.descriptor.yaml` or `.env`

To add a criterion, add a `deny` rule. Rego supports AND, OR, NOT, counts and
path patterns:

```rego
# Documentation-only changes may be larger.
max_lines := 600 if {
	every p in input.paths { startswith(p, "docs/") }
} else := 200

# Source changes need a test change.
deny contains "source changed without a test" if {
	some p in input.paths
	endswith(p, ".py")
	not startswith(p, "tests/")
	not some t in input.paths; startswith(t, "tests/")
}

# A person asked to look at it.
deny contains "ticket is labelled needs-human" if {
	"needs-human" in input.labels
}
```

A rule can only use facts that are in the summary. To add a new fact, such as
coverage, test count, author or the ticket's repository, add it to the summary
builder that supplies the policy input. That is a small code change, made once
per fact.

Check a rule locally with `conftest test --policy policies summary.json`.

## Layer 2: the LLM judge

`judgments/auto-approve.yaml` holds decisions-kit questions. Today there are
three: `risk-low`, `intent-clear` and `scope-limited`. The judge gets the
ticket, the summary and the diff, and every question must resolve to `decided`
with the answer `yes`.

To add a criterion, add a question:

```yaml
  - id: tests-cover-change
    type: choice
    instructions: >-
      The input holds a ticket, a summary of the pull request that implements it, and its diff.
      Decide whether the tests in the diff exercise the behaviour the ticket changes. Use only the input.
    options:
      - { value: "yes", description: The tests exercise the changed behaviour. }
      - { value: "no", description: The changed behaviour has no test. }
      - { value: uncertain, description: The input does not show enough to tell., abstain: true }
    thresholds:
      confidence: 0.9
```

Keep an `uncertain` option with `abstain: true`. A doubtful judge then sends
the PR to a person rather than approving it.

The model and engine are set in `factory.descriptor.yaml` `roles.approver`: `tier`
(`cheap` is Haiku; `mid` or `strong` is Sonnet) and `engine`. A stronger judge
is a one-line change.

### Calibrate the judge

1. Leave `dry_run: true`. Every verdict and its reasons go into the PR body and onto the ticket.
2. Label about ten of these PRs "should merge" or "should not merge", based on what you actually did.
3. Run decisions-kit calibration on `judgments/auto-approve.yaml` with those labels, and record the result in the file's header comment.
4. When the judge agrees with your labels, the judgment counts as calibrated and `require: judgment` may be used.

Compare each ticket's approval verdict (in its approver handoff) with its
outcome.

## Layer 3: the combine rule

```yaml
roles:
  approver:
    tier: cheap
    engine: agent:claude
    dry_run: false
    auto_merge:
      enabled: true
      max_risk: low                # tickets above this risk always go to a person
      require: hard-rules          # or: judgment
      # or one value per level:
      # require: { low: hard-rules, medium: judgment }
```

| `require` | Merges when | Usable |
| --- | --- | --- |
| `hard-rules` | the Rego rules pass, CI passed, every reviewer approved | now |
| `judgment` | all of the above, and every judge question is `decided` `yes` | after calibration |

## What happens on a merge

In the delivery workflow, a new transition comes before the human gate:

```text
in-review --ci.passed [approval = auto-approve, risk <= max_risk, enabled, no factory:hold]
          --> merge --> pr.merged --> merged (retro) --> done
```

The `merge` action:

1. Runs the blocking checks again on the branch merged with the current base, so parallel merges cannot break `main`.
2. Merges with `gh pr merge --merge --match-head-commit <reviewed_sha>`.
3. If the head moved, the base changed, or a check fails, the ticket goes to `approved` and waits for a person.

`ci.pending` never auto-merges; it goes to `approved` as before.

## Rollout

1. Run in dry-run, with today's defaults, and collect verdicts.
2. Turn on `auto_merge` with `require: hard-rules` and `max_risk: low`.
3. Calibrate the judge, then use `require: judgment` for `medium` if you want.
4. Raise `max_risk` only when the approval verdicts show auto-merged tickets did not cause fix-up tickets.

## Safety

- **The gate cannot loosen itself.** `policies/`, `judgments/` and `factory.descriptor.yaml` are on the blocked list, so any change to the criteria, including one the factory proposes in its weekly improvement (#64), needs a person to merge it (ADR-0007).
- **Kill switches.** The `factory:hold` label on a ticket, or `enabled: false`, sends every PR to a person.
- **Fail closed.** A missing `conftest` or `decide`, an unavailable engine, or unreadable output is a reason, never an approval.
- **Two human gates** (ADR-0001) are amended (#91): gate 2 is a human merge unless this policy passes.

## Development workflow

This repository is built by its own factory. Follow this process for every
change, whether you are a person, a pi session or a factory role.

1. **Ticket first.** Every change starts as a GitHub issue in this
   repository, added as a sub-issue of the map
   ({{MAP_ISSUE_LINK}}).
   The body has the scope, acceptance checks, affected repositories and a size
   estimate that counts tests and fixtures. Dependencies use GitHub's native
   blocked-by links, not text in the body. Tracker operations are in
   [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md).
2. **Stage labels.** A ticket carries exactly one `factory:*` stage label:
   `draft → ready → in-dev → in-review → approved → done`, with
   `changes-requested` for a fix round and `blocked` or `failed` when a run
   stops. The next ticket to work on comes from the frontier: open, `ready`,
   every blocker closed, unclaimed (no assignee), in map order.
3. **Split large tickets.** Above about 400 lines, or 3 repositories, split the
   ticket into linked sub-issues before starting. Deliver the first slice.
4. **Deliver through the factory.** A ticket in `ready` runs through
   [workflows/delivery.sm.yaml](workflows/delivery.sm.yaml), driven by
   `factory/sm.py` and the `factory-delivery` skill: router intake →
   developer in `.factory/worktrees/<issue>` → pre-review checks from
   [factory.yaml](factory.yaml) → reviewers chosen by risk (one for
   `risk:low`, behaviour plus structure-security for `risk:high`, each in its
   own directory) → fix rounds → doc-writer when a reviewer reports docs impact →
   approver (dry-run) → PR → router summary on the
   ticket. Each role runs as its own `pi -p` process with the model from
   `factory.yaml`. Whoever orchestrates runs scripts, git and `gh`, and never
   writes or reviews the code.
5. **Merge gate.** A human merges the PR after CI passes and the reviewers
   approve, then moves the ticket to `factory:done`. A PR that passed alone
   can still break the base together with another one: the factory re-runs
   the blocking checks on the branch merged with the current base before
   `approved`, and a failure (or failed CI) is a fix round. Merge PRs one at a
   time, and update a branch that is behind the base first.
6. **Record process gaps.** When a run needs a manual step or hits a factory
   defect, add one comment to the ticket that will fix it (or open a new
   ticket). Do not fix the factory inside an unrelated ticket.

{{MODEL_POLICY}}

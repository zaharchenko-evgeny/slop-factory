## Development workflow

This repository is built by its own factory. Follow this process for every
change, whether you are a person, a pi session or a factory role.

1. **Ticket first.** Every change starts as a GitHub issue in this
   repository, added as a sub-issue of the map
   ({{MAP_ISSUE_LINK}}).
   The body has the scope, acceptance checks, affected repositories and a size
   estimate that counts tests and fixtures. Each acceptance check that searches
   files names the paths it covers or excludes, and uses a command or test name
   instead of an unbounded judgement word such as "history", "clean" or
   "appropriate". Dependencies use GitHub's native blocked-by links, not text
   in the body. Tracker operations are in
   [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md).
2. **Stage labels.** A ticket carries exactly one `factory:*` stage label:
   `draft → ready → in-dev → in-review → approved → merged → done`, with
   `changes-requested` for a fix round and `blocked` or `failed` when a run
   stops. The next ticket to work on comes from the frontier: open, `ready`,
   every blocker closed, unclaimed (no assignee), in map order.
3. **Split large tickets.** Estimate size as lines a reviewer must read: added
   lines plus modified lines, counting tests and fixtures. A whole file deleted
   counts as one line because it is checked by listing it; lines removed from a
   file that remains count as modified. A ticket may report both changed lines
   and deletion volume; use changed lines for the size estimate. Above about
   400 lines, or 3 repositories, split the ticket into linked sub-issues before
   starting. Deliver the first slice.
4. **Deliver through the factory.** The factory is the node list in
   [workflows/nodes.yaml](workflows/nodes.yaml) ([ADR-0014](docs/adr/0014-node-model.md)),
   deployed as `[factory:<node>]` Air automations by the `factory-deploy` skill.
   Each automation's agent runs `uv run factory/gate.py check`, does its
   role's work, writes the handoff to the `handoff_path` that `check`
   printed, and runs `gate.py apply`. The gate alone changes labels, posts
   reviews and comments, and moves the stage. An agent never edits labels,
   never approves, and never merges. On Air the platform pushes the branch
   (`create-pr` for implement, `push-to-source` for fix); work on the
   `branch` that `check` prints. Every automation starts on a label or a PR
   event, never on a schedule: CI labels the PR `factory:ci-green` or
   `factory:ci-red` when it finishes (`.github/workflows/ci-state.yml`).
   Scripted steps (join, finish) run only `gate.py sweep`. Agent reviews are `COMMENT` reviews pinned to the head,
   with the verdict in a `<!-- factory:review ... -->` marker.
5. **Merge gate.** The `merge` node merges an approved PR when every gate
   holds, read fresh: every required focus approved the current head, CI is
   green at it, the PR is not conflicting or behind `main`, and the risk is at
   most `gates.auto_merge.max_risk`. It merges with `--match-head-commit`,
   never a squash or rebase, one PR at a time; a branch behind `main` is
   updated first. A person merges only when `gates.auto_merge.enabled` is
   `false` or the risk is above its limit, a PR whose `factory-checks` ran
   after the latest push to `main` (GitHub cannot require status checks here:
   private repository, no Pro). The `finish` node, on the merge event, then
   moves the ticket to `merged` and `done`, and closes it.
6. **Write notes where the role was triggered.** A role woken by a pull request
   writes every note to that PR, as a PR comment starting with `[factory]` and
   ending with a `Factory-Run:` trailer. This includes process gaps and
   blocked-gate reports. A role woken by a ticket writes notes to that ticket.
   Stage changes and the retro stay on the ticket and are written by the gate.
   Identify the ticket that will fix a process gap (or open a new ticket); do
   not fix the factory inside an unrelated ticket.

{{MODEL_POLICY}}

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
   `draft → ready → in-dev → in-review → approved → merged → done`, with
   `changes-requested` for a fix round and `blocked` or `failed` when a run
   stops. The next ticket to work on comes from the frontier: open, `ready`,
   every blocker closed, unclaimed (no assignee), in map order.
3. **Split large tickets.** Above about 400 lines, or 3 repositories, split the
   ticket into linked sub-issues before starting. Deliver the first slice.
4. **Deliver through the factory.** The factory is the node list in
   [workflows/nodes.yaml](workflows/nodes.yaml) ([ADR-0014](docs/adr/0014-node-model.md)),
   deployed as `[factory:<node>]` Air automations by the `factory-deploy` skill.
   Each automation's agent runs `uv run factory/gate.py check`, does its
   role's work, writes the handoff to the `handoff_path` that `check`
   printed, and runs `gate.py apply`. The gate alone changes labels, posts
   reviews and comments, and moves the stage. An agent never edits labels,
   never approves, and never merges. On Air the platform pushes the branch
   (`create-pr` for implement, `push-to-source` for fix); work on the
   `branch` that `check` prints. Scripted steps (join, sweep) run only
   `gate.py sweep`. Agent reviews are `COMMENT` reviews pinned to the head,
   with the verdict in a `<!-- factory:review ... -->` marker.
5. **Merge gate.** A human merges a PR after CI is green at its exact head
   and the join set `factory:approved`: update a branch that is behind
   `main` first, then merge with `--match-head-commit`, one PR at a time.
   The sweep then moves the ticket to `merged` and `done`, and closes it.
   GitHub cannot require status checks here (private repository, no Pro),
   so merge only a PR whose `factory-checks` ran after the latest push to
   `main`.
6. **Record process gaps.** When a run needs a manual step or hits a factory
   defect, add one comment to the ticket that will fix it (or open a new
   ticket). Do not fix the factory inside an unrelated ticket.

{{MODEL_POLICY}}

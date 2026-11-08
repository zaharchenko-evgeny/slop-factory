# ADR-0023: One comment node

Status: accepted, requested by #888. Extends ADR-0021.

## Context

The factory needs to act on comments from people and agent asks. Air does not
currently deliver all comment events. A comment on a PR can also reach the gate
as an issue comment without a top-level pull request object.

The comment-trigger parts of #590 and #591 describe separate mention and ask
paths. They do not cover all ticket and PR comments or provide one place to
resume work after a person answers.

## Decision

1. A tracker-side rule adds `factory:comment` when a comment is created on a
ticket or PR. It ignores comments with a `factory:from` marker unless they also include a
`factory:ask` marker. Air also wakes the comment node for submitted reviews.
2. One cheap `comment` node reads comments since the last
`factory:comment-seen` cursor, the ticket and PR state, ask markers, and
`workflow.yaml`. It reports one outcome:
   - `route: <node>` sends the item to a workflow node.
   - `reply: <agent>` asks that agent to answer a question.
   - `later` keeps the label so reconcile can retry an active item.
   - `skipped` marks a comment that needs no action and adds a 👀 reaction.
3. The gate applies routes from `workflow.yaml`. It checks the destination
   node and its `when` condition, updates labels and stage, removes
   `factory:comment`, advances the cursor, and replies with the result. It
   refuses invalid routes without writing.
4. An agent ask wakes the same comment node. The answer returns to the asking
   node with the response in `agent_notes`. The existing hop limit applies.
5. Comment runs use `limits.comment_runs_per_day`, separate from
   `runs_per_day`.
6. Remove `human-reply`, `ticket-reply` and the generated `mentions` router.
   Keep `reply-<agent>` nodes as destinations for `reply` outcomes.

## Consequences

The gate owns routing, cursor updates and replies. The tracker rules only wake
the node. The team definition follows ADR-0021's workflow and node model.
This decision supersedes the comment-trigger parts of #590 and #591. The
implementation is split into the slices in #888.

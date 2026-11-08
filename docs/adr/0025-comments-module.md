# ADR-0025: Comment reactions are an opt-in engine module

Status: accepted, requested by #1057 (slice 1). Amends ADR-0023 and the
mention and ask parts of ADR-0021 (#590, #591).

## Context

The factory reacts to comments in three ways:

- `@factory:<agent>` mentions wake that agent in reply mode (#590);
- an agent asks another agent in the thread (#591);
- one comment node routes person comments and agent asks (ADR-0023).

The code for this was spread through the gate, the definition loader, the
node graph checks and deploy. It is not stable yet: several tickets on it are
still open (#893, #892, #941, #952, #861). Every team paid for it, whether it
used it or not, through generated nodes, a wake label and an Actions
workflow.

## Decision

1. The three parts live in one package, `factory/comments/`:
   `mentions.py` (the `mention` guard fact, the generated `mentions` router
   and `reply-<agent>` nodes, the `on: mention` reply target), `asks.py`
   (the `ask` outcome, the `factory:ask` and `factory:answer` markers,
   `limits.mention_hops`) and `node.py` (the ADR-0023 cursor, step input and
   `route`, `reply` and `skip` effects).
2. A team turns it on with `modules: [comments]` in its `factory.yaml`.
   The engine default is `modules: []`. The core imports only the package's
   names (`comments/__init__.py`), never its code, while the module is off.
3. With the module off:
   - `mentionable: true` generates no nodes and no `ask` outcomes, and
     `factory validate` warns `module-off`;
   - a `mention` guard, an `on: mention` effect, or a `route`, `reply` or
     `skip` effect is a `module-off` validation error, and the gate refuses
     it;
   - deploy creates no `factory:comment` label and installs no comment wake
     files.
4. The `factory:from` marker stays in the core. Notify, reviews and lessons
   write it too.

## Consequences

A team without the module has none of the comment machinery in its plan.
To keep the behaviour of ADR-0023, #590 and #591, a team adds
`modules: [comments]`. The open comment tickets continue inside the module.
They are blocked by #1057 until it lands. Later slices of #1057 move the
wake assets under `factory/comments/` and update slop-eval-factory.

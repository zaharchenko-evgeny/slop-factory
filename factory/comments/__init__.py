"""The comments module (ADR-0025): the factory reacts to comments on tickets and PRs. It is off unless the team's
factory.yaml lists `modules: [comments]`; the core imports none of it while it is off.

- `mentions`: `@factory:<agent>` wakes that agent in reply mode (#590): the `mention` guard fact, the generated
  `mentions` router and `reply-<agent>` nodes, and the `on: mention` reply target.
- `asks`: an agent asks another agent in the thread, with a hop budget (#591).
- `node`: one comment node routes person comments and agent asks (ADR-0023): the cursor, the step input and the
  `route`, `reply` and `skip` effects.
"""
NAME = "comments"
GUARDS = ("mention",)  # the guard facts the module adds
EFFECTS = ("route", "reply", "skip")  # the effects of the comment node's outcomes (ADR-0023)

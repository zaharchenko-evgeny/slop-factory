# ADR-0013: Tracker labels as state, one step per event

Status: accepted when #100 merges. Supersedes ADR-0003 and the enforcement part of ADR-0011.

Context: the Air spike (#4) and the prototypes on #100 showed four things.
- A label set by an automation starts the next automation within seconds.
- `push-to-source` pushes only after the run ends.
- The bot cannot approve its own PR.
- `read-only` limits version control only, not labels, comments, or reviews.

The design no longer needs an Air Workflow or a per-ticket loop inside one run.

Options: (a) keep one driver run per ticket and use automations only as event bridges (ADR-0003); (b) make the tracker label the only state and let each event start one step that runs one role.

Decision: (b), as specified in `docs/statechart.md`. The factory script, not the output behaviour, decides every transition. `factory/step.py begin` checks the state, the label provenance, and an atomic ticket lease before a role runs. `end` writes the next state.

Consequences:
- The fork, join, and loop of `workflows/delivery.sm.yaml` become labels and guards.
- Both targets run the same step code. Locally, `drive.py loop` polls the events.
- Read-only output stays on reviewer and approver automations as defence in depth. It is not the gate.
- Each hop starts a new Air environment, about one minute. The design accepts that cost.

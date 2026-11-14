"""The read-only snapshot engine behind the factory progress UI (#1370, #1390).

`snapshot.build(...)` turns the engine's own data (loaders, compiler output, tracker adapters, the local surface's
poll and the gate's guards) into one normalized, JSON-ready dict. Views read only that dict (`views.py`). Nothing here
writes to a tracker, calls `factory begin`, `local.run_event`, `gate.sweep` or `gate.check`, or starts a run.

- `graph`: the workflow graph from the compiled nodes (stages, nodes, triggers, outcomes, conditions, loops).
- `sources`: one ticket source per tracker instance; tickets are deduplicated across overlapping environments.
- `eligibility`: trigger evidence (`local.poll`) and guards (`gate.matches`, `gate.holds`); `unknown` without evidence.
- `age`: the stage age and its warning. `review`: human-review attention. `views`: the view registry.
"""

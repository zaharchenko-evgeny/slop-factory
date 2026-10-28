"""Drive one ticket through the state machine (factory/sm.py) against a tracker adapter.

The adapter supplies the factory operations of docs/agents/issue-tracker.md.
Action results come from the caller; real agent execution is a later slice.
"""
import sm


def drive(m, tracker, nn, executor, results):
    """Claim ticket `nn`, apply each result with `sm.step`, write state and vars back after
    every step, and release the claim once the ticket waits on a human or external actor.
    `results` items use sm.parse_event's forms. Return (ticket, trace)."""
    tracker.claim(nn, executor)  # raises before any write when another executor holds it
    # shortcut: only state and vars persist, not out/pending; resuming a half-run action
    # needs them stored too (slice 2, real agent execution).
    ticket = sm.new_ticket(m, {"state": tracker.get_state(nn), "vars": tracker.read_vars(nn)})
    trace = []
    try:
        for item in results:
            ticket, rep = sm.step(m, ticket, *sm.parse_event(item))
            trace.append(rep)
            tracker.set_state(nn, ticket["state"])
            tracker.write_vars(nn, ticket["vars"])
            if rep["wait"]:
                break
    finally:
        if m["states"][ticket["state"]]["actor"] != "agent":
            tracker.release(nn, executor)
    return ticket, trace

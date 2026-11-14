"""The ticket lease (ADR-0026 `claim` / `release` effects): the assignee holds it, compare-and-set.

A store with `issue(n)["assignees"]` and `assign(n, people)` gets `claim` and `release` from here. A claim by
another executor fails with `ClaimError` and writes nothing; two executors that claim at once both back off.
"""


class ClaimError(Exception):
    pass


def claim(store, n, executor):
    """Assign `executor`, then re-read. True when it wrote, False when it already holds the lease. Raise ClaimError,
    without writing, when another assignee holds it."""
    holders = store.issue(n)["assignees"]
    others = [a for a in holders if a != executor]
    if others:
        raise ClaimError(f"issue {n}: claimed by {', '.join(others)}")
    if holders:
        return False
    store.assign(n, [executor])
    holders = store.issue(n)["assignees"]
    if holders != [executor]:
        # Two executors assigned at once: both back off, so neither holds a half claim.
        release(store, n, executor)
        raise ClaimError(f"issue {n}: claimed by {', '.join(holders) or 'nobody'} after re-read")
    return True


def release(store, n, executor):
    """Drop `executor` from the assignees; True when it wrote. Another executor's lease stays."""
    holders = store.issue(n)["assignees"]
    return executor in holders and bool(store.assign(n, [a for a in holders if a != executor]))

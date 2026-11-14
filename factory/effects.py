"""The effect registry (ADR-0026): every write of the gate goes through `apply`, keyed by `(kind, target)`.

A system declares its handlers in `effects` (factory/ports/system.py `Effect`); the core keeps the factory's rules
here, around every handler:
- `stage: done` closes the ticket first (a retry still finds it by its stage);
- a `merge` runs only when its `needs` facts hold (`refused`); a system merges one repository at a time, the
  primary last (`primary_last`), always pinned to the head the gates judged (`--match-head-commit` on GitHub);
- remote effects (`Effect.remote`: a code-host write that can fail there) run before any tracker write (`order`);
- the `[factory]` prefix and the `Factory-Run:` trailer are added by `frame`, and the team's node marker by `mark`:
  the one place text is framed;
- a handler returns whether it wrote, and returns False when the target already holds (a retry converges).
A handler gets `(system, effect, ctx)`; `ctx` is one dict per `apply`, shared by the handlers a policy chains, so a
read the first one made serves the next.

A feature plugin's effects (plugins/__init__.py `effect` entries) run at the gate's own points, not through a
system: `plugin_handlers` groups the effects a plugin turns into core ones before anything is written (comments:
route, reply, skip; notify: resolve), `plugin_target` finds the plugin that rewrites an `on: <trigger>` effect
(comments: `on: mention`), and the `gate.send` hook sends after the match check (notify).
"""
import importlib

import derive
from tag import current as tag_text, has_tag, same_text, tagged, untag  # noqa: F401  (re-exported: one tag helper, #1365)

# The target of an effect without `on:`: the kinds that write a pull request act on the ticket's PR.
PR_KINDS = ("review", "request-reviewers", "merge", "update-branch", "pr-notes", "pr-notes-sync", "ready-for-review")
ANY = "*"  # a system's catch-all kind (the in-memory system logs any effect)
# The remote kinds of a system that declares no `effects` (a test double, a tracker not yet on the registry).
REMOTE = ("review", "merge", "update-branch")

# The facts a `merge` needs, checked fresh before anything is written (ADR-0021).
NEEDS = {
    "approved-at-head": (lambda f: derive.review_at_head(f) == "approve", "merge refused: no approving review at the current head"),
    "ci-green": (lambda f: f.get("ci") == "green", lambda f: f"merge refused: CI is {f.get('ci')} at the current head"),
    "not-behind": (lambda f: not f.get("behind"), "merge refused: the branch is behind its base (update it first)"),
}
MERGE_NEEDS = ("approved-at-head", "ci-green", "not-behind")


def target(e):
    """`pr` for an effect `on: pr` or a PR kind, else `ticket`; a handler reads any other `on:` itself."""
    return "pr" if e.get("on") == "pr" or (e.get("on") is None and e["kind"] in PR_KINDS) else "ticket"


def registry(system):
    """{(kind, target): Effect} of `system` (none for a tracker that declares no effects)."""
    return {(x.kind, x.target): x for x in getattr(system, "effects", ())}


def spec(system, e):
    """The Effect that applies `e` on `system`, else None."""
    found = registry(system)
    return found.get((e["kind"], target(e))) or found.get((e["kind"], "ticket")) or found.get((ANY, "ticket"))


def _call(x, system, e, ctx):
    fn = x.apply
    if isinstance(fn, str):
        mod, _, name = fn.partition(":")
        fn = getattr(importlib.import_module(mod), name)
    return fn(system, e, ctx)


def apply(system, e, ctx=None):
    """Apply effect `e` on `system` under the core policies; True when it wrote."""
    ctx = {} if ctx is None else ctx
    found = registry(system)
    if e["kind"] == "stage" and e.get("value") == "done" and ("close", "ticket") in found:
        closed = bool(_call(found[("close", "ticket")], system, e, ctx))  # before the stage: a retry still finds it
        return bool(_call(found[("stage", "ticket")], system, e, ctx)) or closed
    x = spec(system, e)
    if x is None:
        raise ValueError(f"unknown effect: {e['kind']}")
    return bool(_call(x, system, e, ctx))


def remote(system, e):
    """Whether `e` writes to a code host and can fail there: its Effect says, else the kind (REMOTE)."""
    if not registry(system):
        return e["kind"] in REMOTE
    x = spec(system, e)
    return bool(x and x.remote)


def order(system, effects):
    """`effects` with the remote writes first and the stage change last: a failed review POST then leaves the labels
    and the stage as they were, instead of a ticket with its risk label removed and no node to wake it (#998)."""
    return sorted(effects, key=lambda e: 0 if remote(system, e) else 2 if e["kind"] == "stage" else 1)


def refused(effects, facts, system=None):
    """The reason the `merge` among `effects` may not run on these facts, else None: every need of the merge
    effect (the core's MERGE_NEEDS plus the system's `Effect.needs`) must hold."""
    for e in effects:
        if e["kind"] != "merge":
            continue
        x = spec(system, e) if system is not None else None
        for need in dict.fromkeys(MERGE_NEEDS + tuple(x.needs if x else ())):
            holds, why = NEEDS[need]
            if not holds(facts):
                return why(facts) if callable(why) else why
    return None


def primary_last(items):
    """A ticket's repositories in merge order: the others first, the primary (listed first) last, so only the
    primary's merge event wakes `finish`."""
    return items[1:] + items[:1]


def plugin_handlers(plugins, effects):
    """{handler spec: [effect]} of the effects a feature plugin turns into core ones: an effect kind it registers
    for its target (`on:`, else `ticket`), unless that target is a plugin trigger (`plugin_target` then)."""
    out = {}
    for e in effects:
        on = e.get("on") or "ticket"
        if on not in plugins.triggers() and (x := plugins.effect(e["kind"], on)):
            out.setdefault(x, []).append(e)
    return out


def plugin_target(plugins, e):
    """The handler spec of the plugin whose trigger `e` targets (`on: mention`), else None."""
    return plugins.effect(e["kind"], e["on"]) if e.get("on") in plugins.triggers() else None


def mark(e, marker):
    """`e` with the team's node marker (definition.comment_marker) at the end of its comment or review text."""
    if e["kind"] == "comment":
        e["value"] = f"{e['value']}\n\n{marker}"
    elif e["kind"] == "review":
        e["summary"] = f"{e.get('summary') or ''}\n\n{marker}"
    return e


def frame(body, run_id=None, once=True):
    """The text a factory role writes to a tracker or code host: the tag line first (unless it starts so) and the
    `Factory-Run: <run id>` trailer last (unless `once` and it has one)."""
    body = tagged(body)
    if run_id and (not once or "Factory-Run:" not in body):
        body += f"\n\nFactory-Run: {run_id}"
    return body


def once_marker(node, outcome, head):
    """The hidden marker of a comment that one node writes once per outcome and head (#1373): runs of a node that
    end at the same head at once each read the PR before another wrote, so the write itself checks for it."""
    return f"<!-- factory:once node={node} outcome={outcome} head={head} -->"

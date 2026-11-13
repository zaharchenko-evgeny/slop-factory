# ADR-0028: State stores are System plugins

Status: accepted, requested by #1270. Amends [ADR-0013](0013-labels-as-state-one-step-per-event.md) and
[ADR-0026](0026-plugins.md).

## Context

ADR-0013 made tracker labels the state of a ticket. ADR-0026 made a tracker a `System` plugin, but it did not say
what a store must guarantee. A team may want its state in markdown files, a local database or the store of another
factory engine. Each one must be a plugin, and the core must not need to know which.

## Decision

Any ticket-state store is a `System` plugin (ADR-0026): a hosted tracker, markdown files, a local database, or
another engine's store. The core reads records and writes effects through the plugin. It never reaches past it.

A store gives these guarantees. `factory/tests/system_contract.py` tests each one, except the lease.

1. **Compare-and-set on the stage.** A `stage` effect may carry `expect`, the stage the caller read. When the
   ticket holds another stage, the effect writes nothing and returns False.
2. **Compare-and-set on the lease.** Taking the lease fails when another holder has it. The lease effect (#1271)
   adds this guarantee and its test.
3. **Actors.** Every ticket event has an `actor`. Every comment has `author_is_bot` and `author_is_factory`.
   `author_is_factory` is true only for the configured factory identity. A bot or a `[factory]` prefix is not proof.
4. **Find.** `find` answers `stage` (ids of tickets in one of the stages; `open_only` leaves out closed tickets),
   `children` and `blocking`. It refuses a query it does not know.
5. **Events.** `event(payload)` maps the payload of the store's trigger to an `Event` whose `kind` is the canonical
   trigger name. It returns None when the payload names no ticket.

Neutral vocabulary:

- A ticket has a set of string **attributes**. Its stage is one of them. A hosted tracker keeps them as labels
  or tags. Another store may keep them as it likes.
- The canonical trigger names are GitHub-style (`issues.labeled`, `pull_request.synchronize`). Each plugin maps
  its own events to them.

ADR-0013 changes from "tracker labels as state" to "ticket attributes as state". The states, the outcomes and one
step per event stay as they are.

## Consequences

- `MemorySystem` and `TagTracker` keep every guarantee. They gained `expect` on `stage`, ticket events with an
  actor, `close` that closes the ticket, and the `blocking` query.
- A system that fails a test lists it in `XFAIL` in its test class, with the follow-up issue. The test must fail,
  so the entry goes when the system is fixed. GitHub: #1280. YouTrack: #1281. Space keeps its tickets in
  memory and passes what `MemorySystem` passes.
- No markdown or SQLite adapter is added here. No surface changes.

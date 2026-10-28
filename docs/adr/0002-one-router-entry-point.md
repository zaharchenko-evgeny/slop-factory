# ADR-0002: One router role as the entry point

Status: accepted (28 Oct 2026)

Decision: a single `router` role classifies every request. Channels (Slack Air Mate, local session, Air cloud session) are thin shells that load it. The router hands off through the tracker for ticket work, or starts an automation run directly for work with no ticket. Channels never talk to each other.

Consequence: adding a channel needs no change to roles or flows.

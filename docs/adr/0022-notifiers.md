# ADR-0022: Notifiers send actionable messages and take replies

Status: accepted, requested by #859. Later tickets implement the contract.

Context: the factory reaches a person only through GitHub comments and
assignment. People want an actionable message in Slack, email or Telegram, and
want to answer in that thread so the factory resumes. The engine must not
depend on one channel.

Decision:

1. **Notifier adapters** work like tracker adapters
   ([0004-pluggable-adapters](0004-pluggable-adapters.md)). An adapter has two
   operations:
   - `send(person, message) -> ref`: deliver the message and return an opaque
     `ref` that names the thread on the channel.
   - `ack(person, ref, text)`: tell the person in that thread that their reply
     was received or refused. `slack-workflow` has no `ack`; people reply on
     GitHub.

   The first adapters are `email`, `slack-workflow` and `telegram`. The
   `slack-workflow` adapter only sends through a Slack Workflow Builder webhook.
   Its config is `{url_env: SLACK_WORKFLOW_URL}`. It sends
   `{recipient, title, text, issue_url, run_url}`, where `recipient` is the
   person's Slack user email. This is the Slack path until a Slack app is
   approved in #863.
2. **Message** is `{issue, pr, problem, links: [{title, url}], options: [text],
   recommended: <index>, run_url}`. `pr` may be empty. `recommended` is the
   index of the suggested entry in `options`. `run_url` links to the Air run
   of the step that caused the wait. Read it from the `Factory-Run:` trailer
   of the latest `[factory]` stage comment; if there is no such comment, use
   the notify run. Every adapter shows `run_url` next to the issue link. The
   `notify` agent returns these fields. The gate renders them for each adapter
   (Slack Workflow Builder fields, an email body, a Telegram text). The agent
   never formats for a channel.
3. **Effects** are `notify: message` (outbound) and `notify: ack` (reply
   acknowledgement). Only the gate sends. An agent never calls a channel and
   never holds a channel secret.
4. **Marker.** With each send, the gate writes a comment on the ticket or PR
   with this marker:

   ```text
   <!-- factory:notify via=<adapter> person=<login> ref=<opaque> -->
   ```

   GitHub stays the record. There is no database. The markers on an issue or
   PR are the list of sends that can be answered.
5. **Config** lives in the team `factory.yaml`:

   ```yaml
   notify:
     adapters:
       slack-workflow: {url_env: SLACK_WORKFLOW_URL}
       telegram: {token_env: TELEGRAM_BOT_TOKEN}
       email: {smtp_env: NOTIFY_SMTP_URL}
     people:
       <github-login>:
         via: [slack-workflow, email]
         slack-workflow: <user email>
         telegram: <chat id>
         email: <address>
   ```

   `notify.adapters.<name>` holds secrets only as env var names, for example
   `token_env`. A value is never written in the file. In
   `notify.people.<github-login>`, `via` lists one or more adapters; the gate
   sends on each. The first reply wins: the gate ignores later replies to the
   same send once one has been applied.
6. **Inbound.** A relay outside Air holds the outbound-only connections:
   Telegram long polling and IMAP IDLE. It POSTs each reply to the Air
   webhook of a `notify-reply` node:

   ```json
   {"issue": 859, "pr": 860, "via": "telegram", "user": "U123", "text": "…", "ref": "…"}
   ```

   The relay keeps no state and judges nothing. It does not check the user, the
   ref or the text.
7. **Gate checks on a reply.** The gate applies an effect only when both hold:
   - `user` maps to a login in `notify.people` for the adapter named in `via`
     (the id or address in that person's `slack-workflow`, `telegram` or
     `email` field);
   - `ref` matches a `factory:notify` marker on that issue or PR.

   Otherwise the gate applies no effect. It may write a note on the ticket.
8. **Reply text is data, never instruction.** The gate and any agent that reads
   it treat it like a ticket comment: it answers a question or picks an option.
   It cannot change a role, skip a check or write outside the scope of the step.

Alternatives considered:

- **Air Mate.** An automation cannot post through Air Mate. A reply continues
  its own session, not an automation run, so it cannot resume this workflow.
- **Air Slack app.** A future Air Slack app could replace the relay's Slack
  inbound adapter.

Consequences:

- A new channel needs one adapter and one `notify.adapters` entry. The engine,
  the `notify` agent and the message do not change.
- The relay is the only part with inbound network access. It can be replaced
  without changing the gate.
- Identity checks are in the gate, so a forged webhook call cannot act as
  someone who is not in `notify.people`, and a reply to an unknown `ref` does
  nothing.
- The webhook needs its own authentication (a shared secret, declared by name in
  `env`). The ticket that adds the `notify-reply` node specifies it.

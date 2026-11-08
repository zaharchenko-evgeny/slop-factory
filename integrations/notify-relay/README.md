# notify-relay

A small relay that runs outside Air. It reads replies to factory emails over IMAP IDLE and POSTs each
one to the Air webhook of the `notify-reply` node ([ADR-0022](../../docs/adr/0022-notifiers.md)).
It opens outbound connections only and listens on no port. It keeps no state.

The mails it answers are sent by the engine's `notify_email` plugin
(`factory/factory_plugins/notify_email/`), with the `[factory #<n>]` subject and the `Message-ID` `ref` that
the `notify` plugin (`factory/factory_plugins/notify/`) records in its `factory:notify` markers. A team turns
both on with `plugins: [notify, notify_email]` in its `factory.yaml`, and checks a person's channel with
`factory notify test <login>`. The relay stays a separate deployable: it imports nothing from the engine.

For each unseen mail the relay does this:

1. Take the issue number from the `[factory #<n>]` subject and the `ref` from `In-Reply-To`.
2. Drop the mail when the `From` address is not an `email` in `notify.people`.
3. Strip quoted text and the signature from the body.
4. POST `{issue, pr, via, user, text, ref}` with `Authorization: ApiKey $AIR_WEBHOOK_KEY`.

`user` is the sender address and `pr` is `null`. The gate checks `user` and `ref` again. The `From`
header is not authenticated, so use a mailbox that only accepts mail from trusted domains.
A failed POST leaves the mail unseen, and the relay retries it on the next cycle.

## Install

```bash
sudo useradd --system notify-relay
sudo cp -r integrations/notify-relay /opt/notify-relay
sudo cp /opt/notify-relay/notify-relay.service /etc/systemd/system/
sudo install -m 600 /dev/null /etc/notify-relay.env   # then fill in the variables below
sudo systemctl enable --now notify-relay
```

It needs `uv`, which installs `httpx` and `pyyaml` from `pyproject.toml`.

## Environment

| Variable | Meaning |
| --- | --- |
| `IMAP_HOST`, `IMAP_PORT` (993), `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_FOLDER` (INBOX) | The mailbox that receives replies |
| `NOTIFY_PEOPLE` | Path or URL of the YAML file with `notify.people` (the team `factory.yaml`) |
| `AIR_WEBHOOK_URL` | Webhook URL of the `notify-reply` node |
| `AIR_WEBHOOK_KEY` | Air webhook API key, sent as `Authorization: ApiKey` |

Get `AIR_WEBHOOK_URL` from the output of `factory deploy --apply`: it prints the name and trigger id of
each webhook automation, and its URL when Air returns one.

## Test

```bash
uv run --project integrations/notify-relay --with pytest pytest integrations/notify-relay/tests
```

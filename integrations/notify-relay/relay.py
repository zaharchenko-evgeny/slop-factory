"""notify-relay: forward replies from a channel to the Air webhook (ADR-0022).

Outbound connections only. The relay keeps no state: it drops replies from unknown senders,
strips quoted text, and POSTs {issue, pr, via, user, text, ref}. The gate checks the rest.
"""
import email
import email.policy
import email.utils
import imaplib
import logging
import os
import re
import select
import time
from typing import Callable

import httpx
import yaml

log = logging.getLogger("notify-relay")

SUBJECT_ISSUE = re.compile(r"\[factory #(\d+)\]")
QUOTE_HEADER = re.compile(r"^(on .+ wrote:|-+ ?original message ?-+|from: .+|sent from my .+)$", re.I)
IDLE_SECONDS = 25 * 60  # Servers drop IDLE after 30 minutes.


def strip_quoted(text: str) -> str:
    """Keep the new part of a reply: cut at the first quote header, signature marker or '>' line."""
    kept = []
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith(">") or stripped == "--" or QUOTE_HEADER.match(stripped):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def load_people(source: str) -> dict[str, str]:
    """Map lower-case email address to GitHub login from a `notify.people` file (path or URL)."""
    if source.startswith(("http://", "https://")):
        response = httpx.get(source, timeout=30, follow_redirects=True)
        response.raise_for_status()
        raw = response.text
    else:
        with open(source, encoding="utf-8") as handle:
            raw = handle.read()
    data = yaml.safe_load(raw) or {}
    people = (data.get("notify") or data).get("people") or {}
    return {str(p["email"]).lower(): login for login, p in people.items() if isinstance(p, dict) and p.get("email")}


def body_text(message) -> str:
    part = message.get_body(preferencelist=("plain",))
    return part.get_content() if part else ""


def reply_payload(raw: bytes, people: dict[str, str]) -> dict | None:
    """The webhook payload for a reply from a known sender to a factory message, else None."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    sender = email.utils.parseaddr(str(message.get("From", "")))[1].lower()
    subject = SUBJECT_ISSUE.search(str(message.get("Subject", "")))
    ref = str(message.get("In-Reply-To", "")).strip()
    if sender not in people:
        log.info("ignored reply from unknown sender")
        return None
    if not subject or not ref:
        log.info("ignored mail without a [factory #n] subject or In-Reply-To")
        return None
    text = strip_quoted(body_text(message))
    if not text:
        return None
    return {"issue": int(subject.group(1)), "pr": None, "via": "email", "user": sender, "text": text, "ref": ref}


def post(payload: dict, url: str, key: str) -> None:
    response = httpx.post(url, json=payload, headers={"Authorization": f"ApiKey {key}"}, timeout=30)
    response.raise_for_status()


def handle(raw: bytes, people: dict[str, str], send: Callable[[dict], None]) -> bool:
    """Forward one mail. True when it was forwarded or ignored for good; send errors propagate."""
    payload = reply_payload(raw, people)
    if payload:
        send(payload)
    return True


def _readline(conn: imaplib.IMAP4_SSL) -> bytes:
    line = conn.readline()
    if not line:
        raise imaplib.IMAP4.abort("connection closed during IDLE")
    return line


def idle(conn: imaplib.IMAP4_SSL, seconds: int) -> None:
    """Wait for new mail with IMAP IDLE (imaplib has no IDLE command)."""
    tag = conn._new_tag()
    conn.send(tag + b" IDLE\r\n")
    _readline(conn)  # "+ idling"
    ready, _, _ = select.select([conn.socket()], [], [], seconds)
    if ready:
        _readline(conn)
    conn.send(b"DONE\r\n")
    while not _readline(conn).startswith(tag):
        pass


def process_unseen(conn: imaplib.IMAP4_SSL, people_source: str, send: Callable[[dict], None]) -> None:
    _, found = conn.search(None, "UNSEEN")
    ids = found[0].split()
    if not ids:
        return
    people = load_people(people_source)
    for msg_id in ids:
        _, data = conn.fetch(msg_id, "(BODY.PEEK[])")
        try:
            handle(data[0][1], people, send)
        except Exception:
            log.exception("could not forward message %s; it stays unseen and is retried", msg_id.decode())
            continue
        conn.store(msg_id, "+FLAGS", "\\Seen")


def run(env=os.environ) -> None:
    people_source, url, key = env["NOTIFY_PEOPLE"], env["AIR_WEBHOOK_URL"], env["AIR_WEBHOOK_KEY"]
    delay = 5
    while True:
        try:
            with imaplib.IMAP4_SSL(env["IMAP_HOST"], int(env.get("IMAP_PORT", "993"))) as conn:
                conn.login(env["IMAP_USER"], env["IMAP_PASSWORD"])
                conn.select(env.get("IMAP_FOLDER", "INBOX"))
                delay = 5
                while True:
                    process_unseen(conn, people_source, lambda payload: post(payload, url, key))
                    idle(conn, IDLE_SECONDS)
        except Exception:
            log.exception("connection lost; retrying in %s s", delay)
            time.sleep(delay)
            delay = min(delay * 2, 300)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run()

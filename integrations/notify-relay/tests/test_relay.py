from email.message import EmailMessage

import pytest

import relay

PEOPLE = {"ann@example.com": "ann"}

BODY = """Option 2 please.

On Sat, 7 Nov 2026 at 10:00, Factory <factory@example.com> wrote:
> Which option?
> 1. wait
--\x20
Ann
"""


def mail(sender="Ann <ann@example.com>", subject="Re: [factory #862] Decide", body=BODY, ref="<ref-1@factory>"):
    message = EmailMessage()
    message["From"], message["Subject"] = sender, subject
    if ref:
        message["In-Reply-To"] = ref
    message.set_content(body)
    return message.as_bytes()


def forward(raw, people=PEOPLE):
    sent = []
    relay.handle(raw, people, sent.append)
    return sent


def test_known_sender_produces_one_post_with_payload():
    assert forward(mail()) == [
        {"issue": 862, "pr": None, "via": "email", "user": "ann@example.com", "text": "Option 2 please.", "ref": "<ref-1@factory>"}
    ]


def test_unknown_sender_produces_no_post():
    assert forward(mail(sender="Eve <eve@example.com>")) == []


def test_mail_without_ticket_subject_or_ref_is_ignored():
    assert forward(mail(subject="Re: lunch")) == []
    assert forward(mail(ref=None)) == []


def test_quoted_text_and_signature_are_removed():
    assert relay.strip_quoted(BODY) == "Option 2 please."
    assert relay.strip_quoted("A\n> quoted\nB") == "A"


def test_people_file_maps_email_to_login(tmp_path):
    path = tmp_path / "factory.yaml"
    path.write_text("notify:\n  people:\n    ann: {via: [email], email: Ann@Example.com}\n    bob: {via: [slack], slack: U1}\n")
    assert relay.load_people(str(path)) == PEOPLE


def test_post_sends_api_key_header(monkeypatch):
    seen = {}

    class Response:
        def raise_for_status(self):
            pass

    def fake_post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return Response()

    monkeypatch.setattr(relay.httpx, "post", fake_post)
    relay.post({"issue": 1}, "https://air.example/hook", "k")
    assert seen["headers"] == {"Authorization": "ApiKey k"} and seen["url"] == "https://air.example/hook"


def test_idle_raises_abort_when_server_closes_after_done(monkeypatch):
    class Conn:
        lines = [b"+ idling\r\n"]
        reads = 0

        def _new_tag(self):
            return b"A1"

        def send(self, data):
            pass

        def socket(self):
            return None

        def readline(self):
            self.reads += 1
            assert self.reads < 10, "idle kept reading after EOF"
            return self.lines.pop(0) if self.lines else b""

    monkeypatch.setattr(relay.select, "select", lambda *a: ([], [], []))
    with pytest.raises(relay.imaplib.IMAP4.abort):
        relay.idle(Conn(), 1)

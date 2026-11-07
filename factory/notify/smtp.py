"""The email adapter: SMTP from the URL in the env var `notify.adapters.email.smtp_env`.

URL: `smtp://[user:password@]host[:port][?from=<address>]` (`smtps://` for TLS from the start). The `Message-ID`
of a send is its `ref`; `ack` replies in that thread.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import make_msgid
from urllib.parse import parse_qs, unquote, urlparse


class EmailAdapter:
    def __init__(self, config):
        env = config.get("smtp_env")
        url = os.environ.get(env or "")
        if not url:
            raise ValueError(f"env var `{env}` (notify.adapters.email.smtp_env) is not set")
        self.url = urlparse(url)
        if self.url.scheme not in ("smtp", "smtps") or not self.url.hostname:
            raise ValueError(f"env var `{env}` is not an smtp:// or smtps:// URL")
        self.sender = (parse_qs(self.url.query).get("from") or [None])[0] \
            or (unquote(self.url.username) if self.url.username and "@" in unquote(self.url.username) else "factory@localhost")

    def _deliver(self, mail):
        tls = self.url.scheme == "smtps"
        port = self.url.port or (465 if tls else 25)
        with (smtplib.SMTP_SSL(self.url.hostname, port, context=ssl.create_default_context(), timeout=30) if tls
              else smtplib.SMTP(self.url.hostname, port, timeout=30)) as server:
            if self.url.username:
                server.login(unquote(self.url.username), unquote(self.url.password or ""))
            server.send_message(mail)

    def _mail(self, person, subject, body):
        mail = EmailMessage()
        mail["From"], mail["To"], mail["Subject"] = self.sender, person["email"], " ".join(subject.split())
        mail["Message-ID"] = make_msgid(domain=self.sender.rpartition("@")[2] or "localhost")
        mail.set_content(body)
        return mail

    def send(self, person, message):
        from . import render  # the package imports this module first
        mail = self._mail(person, f"[factory #{message['issue']}] {message['problem']}", render(message))
        self._deliver(mail)
        return mail["Message-ID"]

    def ack(self, person, ref, text):
        mail = self._mail(person, "Re: factory reply", text)
        mail["In-Reply-To"] = mail["References"] = ref
        self._deliver(mail)

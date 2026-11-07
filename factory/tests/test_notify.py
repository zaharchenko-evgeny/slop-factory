#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Tests for the notifiers (ADR-0022): the email adapter against a fake SMTP server, the `notify` effect of the gate and
its validation. Run: uv run factory/tests/test_notify.py"""
import copy
import json
import os
import shutil
import socketserver
import sys
import tempfile
import threading
import unittest
from email import message_from_bytes
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import definition  # noqa: E402
import gate  # noqa: E402
import notify  # noqa: E402
from gate_port import MemoryTracker  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/v2"
CONFIG = {"adapters": {"email": {"smtp_env": "TEST_NOTIFY_SMTP"}},
          "people": {"ann": {"via": ["email"], "email": "ann@example.com"},
                     "bob": {"via": ["email"], "email": "bob@example.com"}}}
FIELDS = {"problem": "Tests fail\non main", "links": [{"title": "PR", "url": "https://example.com/pr/1"}],
          "options": ["Retry", "Close"], "recommended": 0}


class FakeSmtp(socketserver.ThreadingTCPServer):
    """Accepts mail and keeps each message as raw bytes with its envelope recipients."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        self.mails = []
        super().__init__(("127.0.0.1", 0), Session)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def close(self):
        self.shutdown()
        self.server_close()


class Session(socketserver.StreamRequestHandler):
    def reply(self, text):
        self.wfile.write(text.encode() + b"\r\n")

    def handle(self):
        self.reply("220 fake")
        rcpt, data = [], None
        for raw in self.rfile:
            line = raw.decode().strip()
            verb = line.split(" ")[0].upper()
            if verb == "DATA":
                self.reply("354 go")
                lines = []
                for part in self.rfile:
                    if part == b".\r\n":
                        break
                    lines.append(part)
                self.server.mails.append((rcpt, b"".join(lines)))
                rcpt = []
                self.reply("250 ok")
            elif verb == "QUIT":
                return self.reply("221 bye")
            else:
                rcpt += [line.split(":", 1)[1].strip(" <>")] if verb == "RCPT" else []
                self.reply("250 ok")


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.smtp = FakeSmtp()
        self.addCleanup(self.smtp.close)
        env = patch.dict(os.environ, {"TEST_NOTIFY_SMTP": f"smtp://127.0.0.1:{self.smtp.server_address[1]}?from=factory@example.com"})
        env.start()
        self.addCleanup(env.stop)
        cfg = patch.object(gate, "notify_config", return_value=CONFIG)
        cfg.start()
        self.addCleanup(cfg.stop)
        self.graph = {"nodes": {"notify": {"agent": "notify", "role": "notify", "when": {}, "outcomes": {
            "notified": {"effects": [{"notify": "message"}]},
            "pr-only": {"effects": [{"notify": {"message": "message", "people": ["bob"]}, "on": "pr"}]}}}}}

    def apply(self, outcome, tracker, **extra):
        return gate.apply("notify", outcome, {"ticket": "7", **FIELDS, **extra}, tracker, self.graph)

    def comments(self, tracker):
        return [json.loads(v) for k, v in tracker.tickets["7"]["log"] if k == "comment"]

    def test_one_send_per_person_and_adapter_and_one_comment_with_markers(self):
        tr = MemoryTracker({"7": {"stage": "blocked"}})
        self.assertEqual(self.apply("notified", tr)[0], gate.GO)
        self.assertEqual(sorted(r for r, _ in self.smtp.mails), [["ann@example.com"], ["bob@example.com"]])
        ids = {r[0]: message_from_bytes(raw)["Message-ID"] for r, raw in self.smtp.mails}
        mail = message_from_bytes(self.smtp.mails[0][1])
        self.assertEqual(mail["Subject"], "[factory #7] Tests fail on main")
        self.assertIn("1. Retry (suggested)", mail.get_payload())
        (comment,) = self.comments(tr)
        self.assertTrue(comment.startswith("[factory] Tests fail"))
        self.assertEqual(comment.count("<!-- factory:notify "), 2)
        for who, login in (("ann@example.com", "ann"), ("bob@example.com", "bob")):
            self.assertIn(f"<!-- factory:notify via=email person={login} ref={ids[who]} -->", comment)

    def test_people_limit_and_pr_comment(self):
        tr = MemoryTracker({"7": {"stage": "blocked", "pr": 9}})
        self.apply("pr-only", tr)
        self.assertEqual([r for r, _ in self.smtp.mails], [["bob@example.com"]])
        self.assertEqual(self.comments(tr)[0].count("factory:notify"), 1)

    def test_unknown_person_in_limit_sends_nothing(self):
        tr = MemoryTracker({"7": {"stage": "blocked"}})
        self.graph["nodes"]["notify"]["outcomes"]["pr-only"]["effects"][0]["notify"]["people"] = ["eve"]
        with self.assertRaisesRegex(gate.Bad, "not in notify.people: eve"):
            self.apply("pr-only", tr)
        self.assertEqual(self.smtp.mails, [])

    def test_missing_problem_or_bad_recommended_is_refused(self):
        tr = MemoryTracker({"7": {"stage": "blocked"}})
        with self.assertRaisesRegex(gate.Bad, "payload: missing field `problem`"):
            gate.apply("notify", "notified", {"ticket": "7"}, tr, self.graph)
        with self.assertRaisesRegex(gate.Bad, "recommended"):
            self.apply("notified", tr, recommended=5)
        self.assertEqual(self.smtp.mails, [])

    def test_failed_send_is_reported_and_the_other_send_is_marked(self):
        tr = MemoryTracker({"7": {"stage": "blocked"}})
        self.graph["nodes"]["notify"]["outcomes"]["notified"]["effects"].append({"stage": "done"})
        real = notify.ADAPTERS["email"].send

        def flaky(self, person, message):
            if person["login"] == "ann":
                raise OSError("refused")
            return real(self, person, message)
        with patch.object(notify.ADAPTERS["email"], "send", flaky):
            with self.assertRaisesRegex(RuntimeError, "ann via email: OSError: refused"):
                self.apply("notified", tr)
        (comment,) = self.comments(tr)
        self.assertIn("person=bob", comment)
        self.assertNotIn("person=ann", comment)
        self.assertEqual(tr.tickets["7"]["stage"], "blocked")

    def test_ack_replies_in_the_thread(self):
        adapter = notify.ADAPTERS["email"](CONFIG["adapters"]["email"])
        adapter.ack({"login": "ann", "email": "ann@example.com"}, "<abc@example.com>", "Got it")
        mail = message_from_bytes(self.smtp.mails[0][1])
        self.assertEqual(mail["In-Reply-To"], "<abc@example.com>")


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "team"
        shutil.copytree(FIXTURE, self.root)

    def edit(self, name, change):
        path = self.root / name
        data = yaml.safe_load(path.read_text())
        change(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    def notify_effect(self, spec="message"):
        self.edit("workflow.yaml", lambda w: w["nodes"]["notify"]["outcomes"]["notified"].update(effects=[{"notify": spec}]))

    def errors(self):
        return definition.errors(definition.load(self.root))

    def assertError(self, prefix):
        errs = self.errors()
        self.assertTrue(any(e.startswith(prefix) for e in errs), errs)

    def configure(self, **change):
        config = copy.deepcopy(CONFIG)
        config.update(change)
        self.edit("factory.yaml", lambda f: f.update(notify=config))

    def test_valid_notify_setup_passes(self):
        self.configure()
        self.notify_effect({"message": "message", "people": ["ann"]})
        self.assertEqual(self.errors(), [])

    def test_notify_effect_without_config(self):
        self.notify_effect()
        self.assertError("missing-notify: nodes.notify.notified")

    def test_unknown_adapter(self):
        self.configure(adapters={**CONFIG["adapters"], "pigeon": {}})
        self.assertError("unknown-adapter: notify.adapters.pigeon")
        self.configure(people={"ann": {"via": ["pigeon"]}})
        self.assertError("unknown-adapter: notify.people.ann.via: pigeon")

    def test_person_without_address(self):
        self.configure(people={"ann": {"via": ["email"]}})
        self.assertError("missing-address: notify.people.ann")

    def test_effect_shape_and_unknown_person(self):
        self.configure()
        self.notify_effect({"message": "message", "people": ["eve"]})
        self.assertError("unknown-person: nodes.notify.notified: eve")
        self.notify_effect("nope")
        self.assertError("bad-effect: nodes.notify.notified")


if __name__ == "__main__":
    unittest.main()

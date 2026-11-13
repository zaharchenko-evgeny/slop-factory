#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Tests for the notify and notify_email plugins (ADR-0022, ADR-0026): the email notifier against a fake SMTP server,
the `notify` effect of the gate, its validation, `factory notify test` and the refusals while the plugin is off.
Run: uv run factory/tests/test_notify.py"""
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
import cli  # noqa: E402
import config  # noqa: E402
import definition  # noqa: E402
import gate  # noqa: E402
import plugins  # noqa: E402
from factory_plugins.notify_email.smtp import EmailAdapter  # noqa: E402
from gate_port import MemorySystem  # noqa: E402
from ports.system import Effect  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/v2"
CONFIG = {"adapters": {"email": {"smtp_env": "TEST_NOTIFY_SMTP"}},
          "people": {"ann": {"via": ["email"], "email": "ann@example.com"},
                     "bob": {"via": ["email"], "email": "bob@example.com"}}}
ON = {"plugins": ["comments", "notify_email"]}  # notify_email turns on notify
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
        for p in (patch("factory_plugins.notify.notifiers.team_config", return_value=CONFIG),
                  patch("gate.plugin_registry", return_value=plugins.registry(ON))):
            p.start()
            self.addCleanup(p.stop)
        self.graph = {"nodes": {"notify": {"agent": "notify", "role": "notify", "when": {}, "outcomes": {
            "notified": {"effects": [{"notify": "message"}]},
            "pr-only": {"effects": [{"notify": {"message": "message", "people": ["bob"]}, "on": "pr"}]}}}}}

    def apply(self, outcome, tracker, **extra):
        return gate.apply("notify", outcome, {"ticket": "7", **FIELDS, **extra}, tracker, self.graph)

    def comments(self, tracker):
        return [json.loads(v) for k, v in tracker.tickets["7"]["log"] if k == "comment"]

    def test_one_send_per_person_and_adapter_and_one_comment_with_markers(self):
        tr = MemorySystem({"7": {"stage": "blocked"}})
        self.assertEqual(self.apply("notified", tr)[0], gate.GO)
        self.assertEqual(sorted(r for r, _ in self.smtp.mails), [["ann@example.com"], ["bob@example.com"]])
        ids = {r[0]: message_from_bytes(raw)["Message-ID"] for r, raw in self.smtp.mails}
        mail = message_from_bytes(self.smtp.mails[0][1])
        self.assertEqual(mail["Subject"], "[factory #7] Tests fail on main")
        self.assertIn("1. Retry (suggested)", mail.get_payload())
        (comment,) = self.comments(tr)
        self.assertTrue(comment.startswith("[factory]\nTests fail"))
        self.assertEqual(comment.count("<!-- factory:notify "), 2)
        for who, login in (("ann@example.com", "ann"), ("bob@example.com", "bob")):
            self.assertIn(f"<!-- factory:notify via=email person={login} ref={ids[who]} -->", comment)

    def test_people_limit_and_pr_comment(self):
        tr = MemorySystem({"7": {"stage": "blocked", "pr": 9}})
        self.apply("pr-only", tr)
        self.assertEqual([r for r, _ in self.smtp.mails], [["bob@example.com"]])
        self.assertEqual(self.comments(tr)[0].count("factory:notify"), 1)

    def test_notify_passes_the_preflight_on_a_system_with_only_comments(self):
        class CommentsOnly(MemorySystem):
            effects = (Effect("comment", "ticket", MemorySystem._fx_log), Effect("comment", "pr", MemorySystem._fx_log))

        for outcome in ("notified", "pr-only"):
            with self.subTest(outcome=outcome):
                tr = CommentsOnly({"7": {"stage": "blocked", "pr": 9}})
                self.assertEqual(self.apply(outcome, tr)[0], gate.GO)
                self.assertEqual(len(self.comments(tr)), 1)

    def test_empty_people_limit_sends_to_nobody(self):
        tr = MemorySystem({"7": {"stage": "blocked"}})
        self.graph["nodes"]["notify"]["outcomes"]["pr-only"]["effects"][0]["notify"]["people"] = []
        self.apply("pr-only", tr)
        self.assertEqual(self.smtp.mails, [])

    def test_unknown_person_in_limit_sends_nothing(self):
        tr = MemorySystem({"7": {"stage": "blocked"}})
        self.graph["nodes"]["notify"]["outcomes"]["pr-only"]["effects"][0]["notify"]["people"] = ["eve"]
        with self.assertRaisesRegex(gate.Bad, "not in notify.people: eve"):
            self.apply("pr-only", tr)
        self.assertEqual(self.smtp.mails, [])

    def test_missing_problem_or_bad_recommended_is_refused(self):
        tr = MemorySystem({"7": {"stage": "blocked"}})
        with self.assertRaisesRegex(gate.Bad, "payload: missing field `problem`"):
            gate.apply("notify", "notified", {"ticket": "7"}, tr, self.graph)
        with self.assertRaisesRegex(gate.Bad, "recommended"):
            self.apply("notified", tr, recommended=5)
        self.assertEqual(self.smtp.mails, [])

    def test_failed_send_is_reported_and_the_other_send_is_marked(self):
        tr = MemorySystem({"7": {"stage": "blocked"}})
        self.graph["nodes"]["notify"]["outcomes"]["notified"]["effects"].append({"stage": "done"})
        real = EmailAdapter.send

        def flaky(self, person, message):
            if person["login"] == "ann":
                raise OSError("refused")
            return real(self, person, message)
        with patch.object(EmailAdapter, "send", flaky):
            with self.assertRaisesRegex(RuntimeError, "ann via email: OSError: refused"):
                self.apply("notified", tr)
        (comment,) = self.comments(tr)
        self.assertIn("person=bob", comment)
        self.assertNotIn("person=ann", comment)
        self.assertEqual(tr.tickets["7"]["stage"], "blocked")

    def test_ack_replies_in_the_thread(self):
        adapter = EmailAdapter(CONFIG["adapters"]["email"])
        adapter.ack({"login": "ann", "email": "ann@example.com"}, "<abc@example.com>", "Got it")
        mail = message_from_bytes(self.smtp.mails[0][1])
        self.assertEqual(mail["In-Reply-To"], "<abc@example.com>")

    def test_the_email_notifier_comes_from_the_registry(self):
        registry = plugins.registry({"plugins": ["notify_email"]})
        self.assertTrue(registry.is_on("notify"))  # notify_email requires it
        self.assertIs(registry.impl("notifier", "email"), EmailAdapter)
        self.assertEqual(registry.impls("notifier"), ["email"])
        self.assertIsNone(plugins.registry({"plugins": ["notify"]}).impl("notifier", "email"))

    def test_a_notify_effect_is_refused_while_the_plugin_is_off(self):
        tr = MemorySystem({"7": {"stage": "blocked"}})
        with patch("gate.plugin_registry", return_value=plugins.registry({})):
            with self.assertRaisesRegex(gate.Bad, r"module-off: nodes.notify.notified: notify needs plugins: \[notify\]"):
                self.apply("notified", tr)
            with self.assertRaisesRegex(gate.Bad, r"module-off: nodes.notify.pr-only: notify needs plugins: \[notify\]"):
                self.apply("pr-only", tr)
        self.assertEqual((self.smtp.mails, tr.tickets["7"].get("log", [])), ([], []))

    def test_a_send_through_a_notifier_that_is_off_fails(self):
        tr = MemorySystem({"7": {"stage": "blocked"}})
        with patch("gate.plugin_registry", return_value=plugins.registry({"plugins": ["notify"]})):
            with self.assertRaisesRegex(RuntimeError, "ann via email: LookupError: no notifier `email` is turned on"):
                self.apply("notified", tr)
        self.assertEqual(self.smtp.mails, [])


class CommandTest(unittest.TestCase):
    """`factory notify test <login>`, the word the notify plugin adds to the CLI."""

    def setUp(self):
        self.smtp = FakeSmtp()
        self.addCleanup(self.smtp.close)
        for p in (patch.dict(os.environ, {"TEST_NOTIFY_SMTP": f"smtp://127.0.0.1:{self.smtp.server_address[1]}"}),
                  patch("factory_plugins.notify.notifiers.team_config", return_value=CONFIG),
                  patch("builtins.print")):
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *argv, settings=ON):
        with patch.object(config, "load", return_value=settings):
            return cli._main(["notify", *argv])

    def test_sends_a_test_message_to_one_person(self):
        self.assertEqual(self.run_cli("test", "ann"), 0)
        self.assertEqual([r for r, _ in self.smtp.mails], [["ann@example.com"]])
        self.assertIn("[factory #test] Test message from the factory to ann",
                      " ".join(message_from_bytes(self.smtp.mails[0][1])["Subject"].split()))

    def test_unknown_person_and_bad_usage(self):
        self.assertEqual(self.run_cli("test", "eve"), 2)
        self.assertEqual(self.run_cli("test"), 2)
        self.assertEqual(self.run_cli("send", "ann"), 2)
        self.assertEqual(self.smtp.mails, [])

    def test_a_failed_send_exits_1(self):
        with patch.object(EmailAdapter, "send", side_effect=OSError("refused")):
            self.assertEqual(self.run_cli("test", "ann"), 1)

    def test_the_word_is_unknown_while_the_plugin_is_off(self):
        self.assertEqual(self.run_cli("test", "ann", settings={}), 2)  # the help, as for any unknown word
        self.assertEqual(self.smtp.mails, [])


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = self.tmp / "team"
        shutil.copytree(FIXTURE, self.root)
        self.edit("factory.yaml", lambda f: f.update(ON))

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

    def test_block_and_effect_are_refused_while_the_plugin_is_off(self):
        self.configure()
        self.notify_effect()
        self.edit("factory.yaml", lambda f: f.update(plugins=["comments"]))
        errs = self.errors()
        self.assertIn("module-off: factory.yaml: notify needs plugins: [notify]", errs)
        self.assertIn("module-off: nodes.notify.notified: notify needs plugins: [notify]", errs)
        self.assertFalse([e for e in errs if e.startswith(("unknown-adapter", "missing-"))], errs)

    def test_a_notifier_whose_plugin_is_off(self):
        self.configure()
        self.edit("factory.yaml", lambda f: f.update(plugins=["comments", "notify"]))
        errs = self.errors()
        self.assertIn("module-off: notify.adapters.email: email needs plugins: [notify_email]", errs)
        self.assertIn("module-off: notify.people.ann.via: email needs plugins: [notify_email]", errs)


if __name__ == "__main__":
    unittest.main()

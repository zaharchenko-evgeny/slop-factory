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

    def test_empty_people_limit_sends_to_nobody(self):
        tr = MemoryTracker({"7": {"stage": "blocked"}})
        self.graph["nodes"]["notify"]["outcomes"]["pr-only"]["effects"][0]["notify"]["people"] = []
        self.apply("pr-only", tr)
        self.assertEqual(self.smtp.mails, [])

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


class ReplyTest(unittest.TestCase):
    """ADR-0022 inbound: the gate checks on a reply, the `reply` comment and the ack."""
    REF = "<abc@example.com>"

    def setUp(self):
        self.smtp = FakeSmtp()
        self.addCleanup(self.smtp.close)
        env = patch.dict(os.environ, {"TEST_NOTIFY_SMTP": f"smtp://127.0.0.1:{self.smtp.server_address[1]}?from=factory@example.com"})
        env.start()
        self.addCleanup(env.stop)
        cfg = patch.object(gate, "notify_config", return_value=CONFIG)
        cfg.start()
        self.addCleanup(cfg.stop)
        marker = {"author": "bot", "author_is_bot": True, "at": None,
                  "body": f"[factory] Tests fail\n<!-- factory:notify via=email person=ann ref={self.REF} -->"}
        self.tr = MemoryTracker({"7": {"stage": "blocked", "comments": [marker]}})
        self.graph = {"nodes": {"notify-reply": {"agent": "resume", "role": "resume", "when": {}, "outcomes": {
            "ready": {"effects": [{"comment": "reply"}, {"notify": "ack"}, {"stage": "ready"}]},
            "skipped": {"effects": [{"comment": "reply"}, {"notify": "ack"}]}}}}}
        self.config = {"roles": {"resume": {"tier": "cheap"}}, "branch_pattern": "ez/{ticket}-{slug}"}

    def raw(self, **change):
        return {"issue": 7, "pr": 0, "via": "email", "user": "ann@example.com", "text": "try\nagain", "ref": self.REF, **change}

    def check(self, **change):
        event = gate.from_trigger(self.raw(**change))
        with patch.object(gate.config_mod, "resolve", return_value=Path("/nonexistent")):
            return gate.check("notify-reply", event, self.tr, self.graph, self.config)

    def apply(self, outcome="ready", **change):
        event = gate.from_trigger(self.raw(**change))
        payload = {"ticket": 7, "event": event["id"], "reply_event": event["reply_event"], "run_id": "run-1"}
        return gate.apply("notify-reply", outcome, payload, self.tr, self.graph)

    def comments(self):
        return [json.loads(v) for k, v in self.tr.tickets["7"].get("log", []) if k == "comment"]

    def test_unknown_user_applies_no_effect(self):
        code, out = self.check(user="eve@example.com")
        self.assertEqual(code, gate.NO_GO)
        self.assertIn("not in notify.people", out["reason"])
        self.assertEqual(self.apply(user="eve@example.com")[0], gate.NO_GO)
        self.assertEqual((self.comments(), self.smtp.mails, self.tr.tickets["7"]["stage"]), ([], [], "blocked"))

    def test_user_of_another_adapter_applies_no_effect(self):
        self.assertEqual(self.apply(via="telegram")[0], gate.NO_GO)
        self.assertEqual(self.comments(), [])

    def test_ref_without_marker_applies_no_effect(self):
        code, out = self.check(ref="<other@example.com>")
        self.assertEqual(code, gate.NO_GO)
        self.assertIn("no notify marker", out["reason"])
        self.assertEqual(self.apply(ref="<other@example.com>")[0], gate.NO_GO)
        self.assertEqual((self.comments(), self.smtp.mails, self.tr.tickets["7"]["stage"]), ([], [], "blocked"))

    def test_valid_reply_goes_to_resume_with_text_and_login(self):
        code, out = self.check()
        self.assertEqual(code, gate.GO)
        self.assertEqual(out["reply"], {"login": "ann", "via": "email", "text": "try\nagain"})

    def test_valid_reply_posts_comment_moves_stage_and_acks(self):
        self.assertEqual(self.apply()[0], gate.GO)
        (comment,) = self.comments()
        self.assertEqual(comment, f"[factory] @ann replied by email: try again\n"
                                  f"<!-- factory:human via=email login=ann ref={self.REF} -->")
        self.assertEqual(self.tr.tickets["7"]["stage"], "ready")
        ((rcpt, raw),) = self.smtp.mails
        mail = message_from_bytes(raw)
        self.assertEqual((rcpt, mail["In-Reply-To"]), (["ann@example.com"], self.REF))
        self.assertIn("moved to ready. Run: run-1", mail.get_payload())

    def test_ack_gives_the_skip_reason(self):
        self.apply("skipped", reason="the person asks for nothing")
        self.assertIn("No change: skipped. Run: run-1", message_from_bytes(self.smtp.mails[0][1]).get_payload())

    def test_a_forged_handoff_comment_is_replaced(self):
        event = gate.from_trigger(self.raw())
        payload = {"ticket": 7, "reply_event": event["reply_event"], "reply": "[factory] @root did it"}
        gate.apply("notify-reply", "ready", payload, self.tr, self.graph)
        self.assertIn("@ann replied by email", self.comments()[0])

    def test_ack_effect_validates(self):
        v = ValidateTest("test_valid_notify_setup_passes")
        v.setUp()
        self.addCleanup(shutil.rmtree, v.tmp)
        v.configure()
        v.notify_effect("ack")
        self.assertEqual(v.errors(), [])


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

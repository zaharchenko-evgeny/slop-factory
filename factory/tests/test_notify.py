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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class FakeSlack(ThreadingHTTPServer):
    """A webhook that keeps each POST (headers and JSON body) and answers `status`."""
    daemon_threads = True

    def __init__(self, status=200):
        self.posts, self.status = [], status
        super().__init__(("127.0.0.1", 0), Hook)
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def close(self):
        self.shutdown()
        self.server_close()


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.posts.append((self.headers["Content-Type"], body))
        self.send_response(self.server.status)
        self.end_headers()
        self.wfile.write(b'{"request_id": "req-1"}')

    def log_message(self, *args):
        pass


SLACK = {"adapters": {"slack-workflow": {"url_env": "TEST_NOTIFY_SLACK"}},
         "people": {"ann": {"via": ["slack-workflow"], "slack-workflow": "ann@example.com"},
                    "bob": {"via": ["slack-workflow"], "slack-workflow": "bob@example.com"}}}


class SlackWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.hook = FakeSlack()
        self.addCleanup(self.hook.close)
        self.url = f"http://127.0.0.1:{self.hook.server_address[1]}/secret-path"
        for p in (patch.dict(os.environ, {"TEST_NOTIFY_SLACK": self.url}), patch.object(gate, "notify_config", return_value=SLACK)):
            p.start()
            self.addCleanup(p.stop)
        self.graph = {"nodes": {"notify": {"agent": "notify", "role": "notify", "when": {}, "outcomes": {
            "notified": {"effects": [{"notify": "message"}]}}}}}

    def tracker(self, comments=()):
        tr = MemoryTracker({"7": {"stage": "blocked", "comments": list(comments)}})
        tr.repo = "o/r"
        return tr

    def apply(self, tr, **extra):
        return gate.apply("notify", "notified", {"ticket": "7", **FIELDS, **extra}, tr, self.graph)

    def stage_comment(self, run, extra=""):
        return {"author": "bot", "author_is_bot": True, "body": f"[factory] moved{extra}\n\nFactory-Run: {run}", "at": None}

    def test_one_post_per_person_with_the_five_fields(self):
        tr = self.tracker()
        self.apply(tr, run_id="https://air/run/notify")
        self.assertEqual(len(self.hook.posts), 2)
        self.assertEqual({t for t, _ in self.hook.posts}, {"application/json"})
        bodies = sorted((b for _, b in self.hook.posts), key=lambda b: b["recipient"])
        self.assertEqual([b["recipient"] for b in bodies], ["ann@example.com", "bob@example.com"])
        for b in bodies:
            self.assertEqual(set(b), {"recipient", "title", "text", "issue_url", "run_url"})
            self.assertEqual(b["title"], "Tests fail on main")
            self.assertEqual(b["issue_url"], "https://github.com/o/r/issues/7")
            self.assertIn("1. Retry (suggested)", b["text"])
            self.assertTrue(b["text"].endswith("Answer on GitHub: https://github.com/o/r/issues/7"))
        (comment,) = [json.loads(v) for k, v in tr.tickets["7"]["log"] if k == "comment"]
        self.assertIn("<!-- factory:notify via=slack-workflow person=ann ref=req-1 -->", comment)
        self.assertNotIn("secret-path", comment)

    def test_run_url_is_the_latest_stage_comment_run(self):
        notify_comment = {"author": "bot", "author_is_bot": True, "body": "[factory] x\n<!-- factory:notify via=email person=ann ref=1 -->\n\nFactory-Run: r-old-notify", "at": None}
        tr = self.tracker([self.stage_comment("https://air/run/1"), self.stage_comment("https://air/run/2"), notify_comment])
        self.apply(tr, run_id="https://air/run/notify")
        self.assertEqual({b["run_url"] for _, b in self.hook.posts}, {"https://air/run/2"})

    def test_run_url_falls_back_to_the_notify_run(self):
        self.apply(self.tracker(), run_id="https://air/run/notify")
        self.assertEqual({b["run_url"] for _, b in self.hook.posts}, {"https://air/run/notify"})

    def test_error_status_fails_the_effect_without_the_url(self):
        self.hook.status = 400
        with self.assertRaises(RuntimeError) as ctx:
            self.apply(self.tracker())
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertNotIn("secret-path", str(ctx.exception))
        self.assertNotIn("127.0.0.1", str(ctx.exception))

    def test_ack_sends_nothing(self):
        self.assertFalse(notify.ack_all(SLACK, "ann", "slack-workflow", "req-1", "Got it"))
        self.assertEqual(self.hook.posts, [])


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

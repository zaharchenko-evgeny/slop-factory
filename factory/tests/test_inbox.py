#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/inbox/. Run: uv run factory/tests/test_inbox.py"""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config  # noqa: E402
import inbox  # noqa: E402
from inbox import providers, request  # noqa: E402

config.set_team(ROOT / "factory/tests/fixtures/team")
SETTINGS = config.load()
REPO = SETTINGS["tracker"]["repository"]


class FakeGh:
    """A tiny GitHub: one ticket, optional inbox issue, comments per issue."""

    def __init__(self, labels=("factory:blocked", "factory:needs-human")):
        self.labels, self.comments, self.inbox, self.calls = list(labels), {562: [{"body": "[factory] Blocked: no creds."}]}, None, []
        self.timeline, self.branch = [], "ez/562-inbox"

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["issue", "view"]:
            return json.dumps({"title": "T", "url": "https://x/issues/562", "labels": [{"name": n} for n in self.labels]})
        if args[:2] == ["issue", "list"]:
            return json.dumps([{"number": self.inbox}] if self.inbox else [])
        if args[:2] == ["issue", "create"]:
            self.inbox = 900
            self.comments[900] = []
            return "https://x/issues/900\n"
        if args[:2] == ["issue", "comment"]:
            self.comments[int(args[2])].append({"body": args[-1]})
            return ""
        if args[:2] == ["pr", "view"]:
            return json.dumps({"url": "https://x/pull/7", "state": "OPEN", "headRefName": self.branch})
        if args[0] == "api":
            n = int(args[-1].split("/")[-2])
            items = self.timeline if args[-1].endswith("/timeline") else self.comments[n]
            return json.dumps([items])
        raise AssertionError(args)


def write(tmp, data):
    path = Path(tmp) / "req.json"
    path.write_text(json.dumps(data))
    return path


class Boom:
    def seen(self, key):
        return False

    def send(self, message):
        raise RuntimeError("down")


class InboxTest(unittest.TestCase):
    def test_request_rejects_unknown_kind(self):
        with self.assertRaisesRegex(ValueError, "nope"):
            request.parse('{"kind": "nope", "issue": 1}')
        with self.assertRaisesRegex(ValueError, "colour"):
            request.parse('{"kind": "pr-created", "issue": 1, "colour": 1}')
        with self.assertRaisesRegex(ValueError, "issue"):
            request.parse('{"kind": "pr-created"}')
        self.assertEqual(request.key(request.parse('{"kind": "retro-result"}')), "retro-result:::")

    def test_github_inbox_creates_issue_once_and_comments_once_per_key(self):
        gh = FakeGh()
        message = inbox.facts({"kind": "decision-needed", "issue": 562, "run": "r1"}, SETTINGS, gh)
        provider = providers.REGISTRY["github-inbox"](SETTINGS, gh)
        self.assertTrue(provider.send(message))
        self.assertFalse(provider.send(message))
        self.assertTrue(providers.REGISTRY["github-inbox"](SETTINGS, gh).seen(message["key"]))
        self.assertTrue(provider.send(dict(message, key="decision-needed:562::r2")))
        self.assertEqual(sum(c[:2] == ["issue", "create"] for c in gh.calls), 1)
        first, second = gh.comments[900]
        self.assertIn("[factory] decision-needed: T", first["body"])
        self.assertIn(f"@{SETTINGS['notify']['github_user']}", first["body"])
        self.assertIn("<!-- factory-inbox:decision-needed:562::r1 -->", first["body"])
        self.assertIn("Factory-Run: r1", first["body"])

    def test_deliver_continues_after_a_provider_fails(self):
        gh, settings = FakeGh(), {**SETTINGS, "notify": {**SETTINGS["notify"], "providers": [{"kind": "log"}, {"kind": "github-inbox"}]}}
        providers.REGISTRY["boom"] = lambda s, r: Boom()
        settings["notify"]["providers"].insert(0, {"kind": "boom"})
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                result = inbox.deliver({"kind": "pr-created", "issue": 562}, settings, gh)
        finally:
            del providers.REGISTRY["boom"]
        self.assertEqual(result["sent"], ["log", "github-inbox"])
        self.assertEqual(result["failed"], [{"kind": "boom", "error": "down"}])
        self.assertEqual(json.loads(out.getvalue())["kind"], "pr-created")

    def test_timeline_pr_from_another_repository_is_ignored(self):
        gh = FakeGh()
        ref = lambda n, repo: {"source": {"issue": {"number": n, "pull_request": {"url": "u"}, "repository": {"full_name": repo}}}}
        gh.timeline = [ref(7, REPO), ref(99, "other/repo")]
        self.assertEqual(inbox.facts({"kind": "pr-created", "issue": 562}, SETTINGS, gh)["pr"], 7)

    def test_check_reports_duplicate(self):
        gh, req = FakeGh(), {"kind": "pr-created", "issue": 562, "pr": 7}
        first = inbox.check(req, SETTINGS, gh)
        self.assertEqual((first["duplicate"], first["stage"], first["pr_url"]), (False, "blocked", "https://x/pull/7"))
        self.assertEqual(first["latest"], "[factory] Blocked: no creds.")
        self.assertFalse([c for c in gh.calls if c[:2] in (["issue", "create"], ["issue", "comment"])])
        inbox.deliver(req, SETTINGS, gh)
        self.assertTrue(inbox.check(req, SETTINGS, gh)["duplicate"])

    def test_notify_alias_sends_decision_needed_even_with_needs_human(self):
        gh = FakeGh()  # carries factory:needs-human
        sent = inbox.deliver(inbox.notify_request(562, SETTINGS, gh), SETTINGS, gh)
        self.assertEqual(sent["sent"], ["github-inbox"])
        body = gh.comments[900][0]["body"]
        self.assertIn("decision-needed", body)
        self.assertIn("Blocked: no creds", body)

    def test_check_skips_factory_comment(self):
        for extra in ({"author": "jetbrains-air[bot]"}, {"body": "[factory] Blocked"}):
            got = inbox.check({"kind": "pr-comment", "issue": 562, "pr": 7, **extra}, SETTINGS, FakeGh())
            self.assertEqual(got["skip"], "factory comment")
        got = inbox.check({"kind": "pr-comment", "issue": 562, "pr": 7, "author": "alice", "body": "lgtm"}, SETTINGS, FakeGh())
        self.assertNotIn("skip", got)

    def test_check_skips_inbox_issue(self):
        gh = FakeGh(labels=("factory:inbox",))
        gh.comments[900] = []
        self.assertEqual(inbox.check({"kind": "pr-comment", "issue": 900}, SETTINGS, gh)["skip"], "event on the inbox issue")

    def test_check_skips_non_factory_branch(self):
        gh = FakeGh()
        gh.branch = "feature/x"
        self.assertIn("not a factory branch", inbox.check({"kind": "pr-created", "issue": 562, "pr": 7}, SETTINGS, gh)["skip"])

    def test_cli_rejects_a_bad_request_file(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(inbox.main(["check", "--request", "/nonexistent.json"]), 1)
        self.assertIn("error", out.getvalue())


if __name__ == "__main__":
    unittest.main()

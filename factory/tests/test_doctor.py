#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Unit tests for factory/doctor.py with a stubbed gh. Run: uv run --quiet factory/tests/test_doctor.py"""
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import config as cfg  # noqa: E402
cfg.set_team(HERE.parents[1] / "factory/tests/fixtures/team")
import doctor  # noqa: E402
import nodes  # noqa: E402

REPO = "o/r"


class FakeGh:
    """In-memory GitHub: creates issues and a PR, records every call, fails the calls matching `deny`."""

    def __init__(self, deny=(), open_tickets=()):
        self.deny, self.calls, self.issues, self.prs = deny, [], {}, {}
        self.open_tickets = list(open_tickets)

    def __call__(self, args):
        self.calls.append(args)
        text = " ".join(args)
        if any(d in text for d in self.deny):
            raise RuntimeError("HTTP 403: Resource not accessible by integration")
        if args[:2] == ["issue", "create"]:
            n = 10 + len(self.issues)
            self.issues[n] = args[args.index("--title") + 1]
            return f"https://github.com/{REPO}/issues/{n}\n"
        if args[:2] == ["pr", "create"]:
            self.prs[50] = "open"
            return f"https://github.com/{REPO}/pull/50\n"
        if args[:2] == ["pr", "close"]:
            self.prs.pop(int(args[2]))
        if args[:2] == ["issue", "delete"]:
            self.issues.pop(int(args[2]))
        if args[:2] == ["issue", "list"]:
            return json.dumps([{"number": n, "title": t} for n, t in self.open_tickets])
        if args[0] == "api" and "/actions/runs" in args[-1]:
            return json.dumps({"workflow_runs": [{"id": 7}]})
        if args[:2] == ["run", "view"]:
            return "log line"
        if args[0] == "api" and args[-1].endswith("/commits/main"):
            return json.dumps({"sha": "b" * 40, "parents": [{"sha": "a" * 40}]})
        if args[0] == "api" and "/issues/" in args[-1] and not args[-1].endswith("timeline"):
            return json.dumps({"id": 999})
        return ""


def run(gh, env="read", **kw):
    return {r["probe"]: r for r in doctor.run_probes(REPO, env, nodes.load(), gh, "risk:low", **kw)}


class DoctorTest(unittest.TestCase):
    def test_denied_write_reported_and_ticket_opened(self):
        gh = FakeGh(deny=("dependencies/blocked_by",))
        got = run(gh)
        self.assertEqual(got["write:blocked-by"]["result"], "denied")
        self.assertEqual(got["write:label"]["result"], "ok")
        failed = [r for r in got.values() if r["result"] != "ok" and r["probe"].startswith("write:")]
        doctor.file_tickets(REPO, failed, gh)
        self.assertIn("Doctor: write:blocked-by fails", [c[c.index("--title") + 1] for c in gh.calls if c[:2] == ["issue", "create"]])
        gh.open_tickets = [(7, "Doctor: write:blocked-by fails")]  # an open ticket is updated, not duplicated
        gh.calls.clear()
        doctor.file_tickets(REPO, failed, gh)
        self.assertEqual([c[:3] for c in gh.calls if c[:2] in (["issue", "create"], ["issue", "comment"])], [["issue", "comment", "7"]])

    def test_missing_tool_reported_for_env(self):
        got = run(FakeGh(), "write", which=lambda t: None if t == "pi" else "/bin/" + t)
        self.assertEqual(got["tool:pi"]["result"], "missing")
        self.assertEqual(got["tool:git"]["result"], "ok")
        self.assertEqual(got["skill:ponytail"]["result"], "ok")  # a developer skill, only in `write`
        self.assertNotIn("skill:ponytail", run(FakeGh(), "ops", which=lambda t: "/bin/" + t))

    def test_probe_objects_are_deleted(self):
        for deny in ((), ("blocked_by", "update-branch")):
            gh = FakeGh(deny=deny)
            run(gh)
            self.assertEqual((gh.issues, gh.prs), ({}, {}))
            self.assertTrue(any(c[:2] == ["pr", "close"] and "--delete-branch" in c for c in gh.calls))

    def test_published_text_has_trailer_and_run_log_is_fetched(self):
        gh = FakeGh()
        run(gh, run_id="run-9")
        bodies = [c[c.index("--body") + 1] for c in gh.calls if "--body" in c]
        self.assertTrue(bodies and all(b.endswith("Factory-Run: run-9") for b in bodies))
        self.assertIn(["run", "view", "7", "-R", REPO, "--log"], gh.calls)
        got = run(FakeGh(deny=("run view",)))
        self.assertEqual(got["read:actions-run-logs"]["result"], "denied")


if __name__ == "__main__":
    unittest.main()

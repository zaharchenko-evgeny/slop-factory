#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/contracts.py on the recordings in factory/tests/contracts/. Run: uv run --quiet factory/tests/test_contracts.py"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
cfg.set_team(HERE / "fixtures/team")
import contracts  # noqa: E402
import gate  # noqa: E402

REC = HERE / "contracts/air-mcp.json"
EVENTS = ["issues.labeled", "pull_request.labeled", "pull_request.merged", "pull_request_review.submitted", "issues.closed"]


def ask(server, *calls):
    """The replies of a fake server started from the recordings to each (tool, arguments)."""
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}]
    msgs += [{"jsonrpc": "2.0", "id": i + 2, "method": "tools/call", "params": {"name": t, "arguments": a}}
             for i, (t, a) in enumerate(calls)]
    out = subprocess.run([sys.executable, str(ROOT / "factory/contracts.py"), "serve", str(REC), server], check=True,
                         input="".join(json.dumps(m) + "\n" for m in msgs), capture_output=True, text=True).stdout
    return [json.loads(line) for line in out.splitlines()][1:]


class ContractsTest(unittest.TestCase):
    def test_fake_mcp_rejects_unrecorded_tool(self):
        ok, bad, wrong_arg, missing = ask("air-automations", ("list_automations", {}), ("delete_everything", {}),
                                          ("get_automation", {"automationId": "x", "bogus": 1}), ("get_automation", {}))
        self.assertIn("result", ok)
        self.assertIn("unrecorded air-automations tool: delete_everything", bad["error"]["message"])
        self.assertIn("unknown arguments", wrong_arg["error"]["message"])
        self.assertIn("missing", missing["error"]["message"])
        # a tool of the other server is unrecorded here
        self.assertIn("error", ask("air-automations", ("list_environments", {"repositoryUrl": "x"}))[0])

    def test_every_called_tool_is_recorded(self):
        rec = json.loads(REC.read_text())["servers"]
        for server, tool in [("air-automations", t) for t in ("list_automations", "get_automation", "create_automation",
                             "update_automation", "delete_automation", "run_automation")] + \
                            [("air-cloud", t) for t in ("list_environments", "get_environment", "edit_environment")]:
            self.assertIsNotNone(contracts.find(rec[server]["responses"], tool, {}), tool)
            self.assertIn(tool, [t["name"] for t in rec[server]["tools"]])

    def test_payload_fixtures_parse_for_every_trigger(self):
        for event in EVENTS:
            wrapper = json.loads((HERE / f"contracts/payloads/{event}.json").read_text())
            self.assertEqual(wrapper["eventType"], event)
            got = gate.from_trigger(wrapper)
            self.assertEqual(got["event_name"], event.split(".")[0])
            self.assertIsInstance(got["ticket"], int)

    def test_redact_strips_tokens_emails_and_secret_keys(self):
        out = contracts.redact({"a": "ghp_" + "x" * 30 + " me@example.com", "api_token": "v", "n": [{"k": "Bearer abcdefgh12"}]})
        self.assertEqual(out, {"a": "<token> <email>", "api_token": "<redacted>", "n": [{"k": "<token>"}]})

    def test_recordings_contain_no_tokens(self):
        for path in (HERE / "contracts").rglob("*.json"):
            text = path.read_text()
            self.assertIsNone(contracts.SECRET.search(text), path.name)
            self.assertEqual([m for m in re.findall(contracts.EMAIL, text) if not m.endswith("users.noreply.github.com")], [], path.name)


if __name__ == "__main__":
    unittest.main()

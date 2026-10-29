#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Unit tests for factory/harnesses.py adapters. Run: uv run factory/tests/test_harnesses.py"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import harnesses  # noqa: E402
import preflight  # noqa: E402
import sm  # noqa: E402
import yaml  # noqa: E402

READ = "read,bash,grep,find,ls"
WRITE_TOOLS = ("Bash", "Edit", "Write")
DENIED = {"Edit", "Write", "NotebookEdit"}


class Adapters(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "common.md").write_text("COMMON")
        (self.tmp / "role.md").write_text("ROLE")
        (self.tmp / "skill").mkdir()
        (self.tmp / "skill/SKILL.md").write_text("SKILLTEXT")
        os.environ.pop("FACTORY_AGENT", None)

    def call(self, role="developer", tools="read,bash,edit,write,grep,find,ls"):
        return harnesses.Call("9", role, "m1", tools, "PROMPT", [self.tmp / "common.md", self.tmp / "role.md"],
                              [self.tmp / "skill"], self.tmp)

    def arg(self, cmd, flag):
        return cmd[cmd.index(flag) + 1]

    def test_pi_command_and_usage(self):
        ad = harnesses.ADAPTERS["pi"]
        cmd = ad.command(self.call())
        self.assertEqual(cmd[:3], ["pi", "-p", "--mode"])
        self.assertEqual((self.arg(cmd, "--model"), self.arg(cmd, "--session-id"), self.arg(cmd, "--skill")),
                         ("m1", "factory-9-developer", str(self.tmp / "skill")))
        self.assertIn("--no-session", ad.command(self.call("reviewer", READ)))
        ev = {"type": "message_end", "message": {"role": "assistant", "content": "hi", "usage": {
            "input": 3, "output": 4, "cost": {"total": 0.5}}}}
        self.assertEqual(ad.parse(json.dumps(ev)), ("hi", {"input": 3, "output": 4, "cacheRead": 0,
                                                           "cacheWrite": 0, "cost": 0.5}))

    def test_claude_command_resume_and_readonly(self):
        ad = harnesses.ADAPTERS["claude-code"]
        cmd = ad.command(self.call())
        self.assertEqual(cmd[:2], ["claude", "-p"])
        self.assertEqual((self.arg(cmd, "--output-format"), self.arg(cmd, "--model")), ("json", "m1"))
        self.assertIn("--session-id", cmd)
        self.assertIn("SKILLTEXT", self.arg(cmd, "--append-system-prompt"))
        self.assertIn("COMMON", self.arg(cmd, "--append-system-prompt"))
        raw = json.dumps({"type": "result", "result": "done", "session_id": "s1", "total_cost_usd": 0.25,
                          "usage": {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 5}})
        self.assertEqual(ad.parse(raw, self.call()), ("done", {"input": 1, "output": 2, "cacheRead": 5,
                                                                "cacheWrite": None, "cost": 0.25}))
        self.assertEqual(self.arg(ad.command(self.call()), "--resume"),
                         self.arg(cmd, "--session-id"))  # fix round resumes the same session
        ro = ad.command(self.call("reviewer", READ))
        allowed = self.arg(ro, "--allowedTools").split(",")
        self.assertEqual(allowed[:4], ["Read", "Grep", "Glob", "LS"])
        self.assertEqual(allowed[4:], [f"Bash(git {g}:*)" for g in ("diff", "log", "show", "status")])
        self.assertEqual(set(self.arg(ro, "--disallowedTools").split(",")), DENIED)
        self.assertNotIn("--session-id", ro)
        self.assertEqual(ad.parse("plain", None), ("plain", None))

    def test_codex_command_resume_and_readonly(self):
        ad = harnesses.ADAPTERS["codex"]
        cmd = ad.command(self.call())
        self.assertEqual((cmd[:3], self.arg(cmd, "-m"), self.arg(cmd, "--sandbox")),
                         (["codex", "exec", "--json"], "m1", "workspace-write"))
        self.assertIn("SKILLTEXT", cmd[-1])
        self.assertTrue(cmd[-1].endswith("PROMPT"))
        raw = "\n".join(json.dumps(e) for e in [
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {"input_tokens": 7, "cached_input_tokens": 2, "output_tokens": 3}}])
        self.assertEqual(ad.parse(raw, self.call()), ("done", {"input": 7, "output": 3, "cacheRead": 2,
                                                                "cacheWrite": None, "cost": None}))
        resumed = ad.command(self.call())
        self.assertEqual(resumed[:3], ["codex", "exec", "resume"])
        self.assertIn("t1", resumed)
        self.assertEqual(self.arg(ad.command(self.call("reviewer", READ)), "--sandbox"), "read-only")
        self.assertNotIn("resume", ad.command(self.call("reviewer", READ)))

    def test_codex_developer_can_write_the_worktree_git_dir(self):
        wt = self.tmp / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {self.tmp}/main/.git/worktrees/wt\n")
        c = self.call()
        c.cwd = wt
        for cmd in (harnesses.ADAPTERS["codex"].command(c),):
            self.assertIn(f'sandbox_workspace_write.writable_roots=["{self.tmp}/main/.git"]', cmd)
        (self.tmp / "developer.session").write_text("t1")
        self.assertIn(f'sandbox_workspace_write.writable_roots=["{self.tmp}/main/.git"]',
                      harnesses.ADAPTERS["codex"].command(c))
        r = self.call("reviewer", READ)
        r.cwd = wt
        self.assertFalse([a for a in harnesses.ADAPTERS["codex"].command(r) if "writable_roots" in a])

    def test_no_adapter_gives_a_reviewer_write_tools(self):
        for name in ("claude-code", "codex"):
            cmd = harnesses.ADAPTERS[name].command(self.call("reviewer", READ))
            joined = " ".join(cmd[:-1] if name == "codex" else cmd)
            self.assertNotIn("workspace-write", joined)
            self.assertNotIn("acceptEdits", joined)
            if name == "claude-code":
                self.assertFalse(set(WRITE_TOOLS) & set(self.arg(cmd, "--allowedTools").split(",")))  # no bare Bash

    def test_harness_resolution_junie_and_validate(self):
        config = yaml.safe_load((sm.ROOT / "factory.yaml").read_text())
        self.assertEqual(harnesses.harness_of(config, "reviewer", "full"), "pi")
        del config["roles"]["router"]["harness"]
        config["targets"]["local"]["harness"] = "codex"
        self.assertEqual(harnesses.harness_of(config, "router"), "codex")
        config["roles"]["reviewer"]["modes"]["full"]["harness"] = "claude-code"
        self.assertEqual(harnesses.harness_of(config, "reviewer", "full"), "claude-code")
        self.assertEqual(harnesses.config_errors(config), [])
        config["roles"]["developer"]["harness"] = "emacs"
        self.assertIn("unknown harness 'emacs'", harnesses.config_errors(config)[0])
        machine = yaml.safe_load((sm.ROOT / "workflows/delivery.sm.yaml").read_text())
        with self.assertRaisesRegex(sm.SMError, "unknown harness 'emacs'"):
            sm.validate(machine, config)
        config["roles"]["developer"]["harness"] = "junie"
        sm.validate(machine, config)  # known, a stub
        ok, msg = preflight.check("m", harness="junie")
        self.assertFalse(ok)
        self.assertEqual(len(msg.splitlines()), 1)
        with self.assertRaises(SystemExit):
            harnesses.adapter("junie")


if __name__ == "__main__":
    unittest.main()

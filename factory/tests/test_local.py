#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/local.py, the local target. Run: uv run factory/tests/test_local.py"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
import local  # noqa: E402
import nodes  # noqa: E402

FIXTURE = ROOT / "factory/tests/fixtures/team"
URL = "https://github.com/zaharchenko-evgeny/slop-factory"


class Machine:
    """The laptop: which CLIs are on PATH, whether `gh` is logged in, and each checkout's origin."""

    def __init__(self, tools=("claude", "gh"), logged_in=True, origins=None):
        self.tools, self.logged_in, self.origins, self.calls = set(tools), logged_in, origins or {}, []

    def which(self, name):
        return f"/bin/{name}" if name in self.tools else None

    def run(self, argv, **kw):
        self.calls.append(argv)
        if argv[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(argv, 0 if self.logged_in else 1, "", "")
        if argv[:3] == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(argv, 0, "alice\n", "")
        if argv[0] == "git" and argv[-3:] == ["remote", "get-url", "origin"]:
            origin = self.origins.get(Path(argv[2]).name)
            return subprocess.CompletedProcess(argv, 0 if origin else 2, (origin or "") + "\n", "")
        raise AssertionError(f"unexpected command {argv}")


class LocalDeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()  # the team root is resolved (/private/var on macOS)
        self.team = self.base / "factory"
        shutil.copytree(FIXTURE, self.team)
        (self.team / ".git").mkdir()
        before, cwd = cfg.team_root(), os.getcwd()
        cfg.set_team(self.team)
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: (cfg.set_team(before), os.chdir(cwd)))

    def deploy(self, machine, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = local.deploy(list(argv), run=machine.run, which=machine.which)
        return code, out.getvalue()

    def test_missing_harness_is_named(self):
        code, out = self.deploy(Machine(tools=("gh",), origins={"slop-factory": URL}))
        self.assertEqual(code, 1)
        self.assertIn("missing harness: `claude` (claude-code) is not on PATH", out)

    def test_gh_not_logged_in_refuses(self):
        code, out = self.deploy(Machine(logged_in=False, origins={"slop-factory": URL}))
        self.assertEqual(code, 1)
        self.assertIn("missing gh: not logged in", out)

    def test_a_checkout_with_another_origin_is_missing(self):
        code, out = self.deploy(Machine(origins={"slop-factory": "git@github.com:someone/else.git"}))
        self.assertEqual(code, 1)
        self.assertIn(f"missing checkout: slop-factory at {self.base / 'slop-factory'}", out)

    def test_apply_records_the_laptop(self):
        code, out = self.deploy(Machine(origins={"slop-factory": "git@github.com:zaharchenko-evgeny/slop-factory.git"}), "--apply")
        self.assertEqual(code, 0, out)
        state = json.loads((Path(self.tmp.name) / ".factory/local.json").read_text())
        self.assertEqual((state["harness"], state["login"]), ("claude-code", "alice"))
        self.assertEqual(state["checkouts"], {"slop-factory": str(self.base / "slop-factory")})
        self.assertIn("implement-mid", state["automations"])

    def test_dry_run_writes_nothing(self):
        code, _ = self.deploy(Machine(origins={"slop-factory": URL}))
        self.assertEqual(code, 0)
        self.assertFalse((Path(self.tmp.name) / ".factory/local.json").exists())

    def test_unknown_argument_prints_usage(self):
        self.assertEqual(self.deploy(Machine(), "--bogus")[0], 2)


class PlanLocalTest(unittest.TestCase):
    def test_event_nodes_only(self):
        graph = {"nodes": {
            "implement": {"role": "developer", "env": "write", "tiers": ["cheap", "mid"], "output": "create-pr",
                          "triggers": ["issues.labeled factory:implement-<tier>"]},
            "person": {"role": "human", "triggers": ["manual"]},
            "cron": {"role": "script", "env": "ops", "runner": "actions", "triggers": ["schedule nightly"]},
            "off": {"role": "script", "env": "read", "enabled": False, "triggers": ["pull_request.merged"]},
            "retro": {"role": "retro", "env": "ops", "triggers": ["schedule retro"]}}}
        plan = local.plan_local(graph, {"roles": {"developer": {"tier": "mid"}, "retro": {"tier": "cheap"}}})
        self.assertEqual([(p["automation"], p["output"]) for p in plan], [("implement-cheap", "create-pr"), ("implement-mid", "create-pr")])
        self.assertEqual(plan[1]["triggers"][0]["filters"], {"label": "factory:implement-mid"})

    def test_the_team_graph_has_the_review_and_finish_automations(self):
        before = cfg.team_root()
        cfg.set_team(FIXTURE)
        try:
            names = {p["automation"] for p in local.plan_local(nodes.load(), cfg.load())}
        finally:
            cfg.set_team(before)
        self.assertTrue({"intake", "implement-mid", "review-full", "finish"} <= names, names)

    def test_same_repository(self):
        self.assertTrue(local.same_repository(URL, "git@github.com:zaharchenko-evgeny/slop-factory.git\n"))
        self.assertTrue(local.same_repository(URL + "/", URL + ".git"))
        self.assertFalse(local.same_repository(URL, "https://github.com/zaharchenko-evgeny/slop-eval-factory"))


if __name__ == "__main__":
    unittest.main()

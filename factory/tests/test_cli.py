#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for `factory begin` and `factory end` (factory/cli.py). Run: uv run factory/tests/test_cli.py"""
import contextlib
import io
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
cfg.set_team(ROOT)
import cli  # noqa: E402

cli.load_engine(cli.HERE)

PAYLOAD = {"action": "labeled", "issue": {"number": 7, "title": "Add x", "updated_at": "T1"}}


class BeginEndTest(unittest.TestCase):
    def setUp(self):
        cfg.set_team(ROOT)
        self.old = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        cli.STATE = Path(".factory/begin.json")
        self.addCleanup(lambda: (os.chdir(self.old), self.tmp.cleanup()))

    def tracker(self, **record):
        Path("tickets.json").write_text(json.dumps({"7": {"stage": "in-dev", **record}}))
        Path("event.json").write_text(json.dumps(PAYLOAD))
        return ["--tracker", "memory:tickets.json"]

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            return cli.main(list(argv)), out.getvalue()

    def test_begin_exits_10_for_duplicate_event(self):
        tracker = self.tracker(seen=["issues:T1"])
        code, out = self.run_cli("begin", "implement-mid", "event.json", *tracker)
        self.assertEqual((code, json.loads(out)["reason"]), (10, "duplicate event"))

    def test_end_applies_handoff_for_begun_node(self):
        tracker = self.tracker()
        code, out = self.run_cli("begin", "implement-mid", "event.json", *tracker)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["handoff_path"], ".factory/handoff/implement.json")
        code, out = self.run_cli("end", "done")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(Path("tickets.json").read_text())["7"]["stage"], "in-review")

    def test_end_without_begin_is_an_error(self):
        self.assertEqual(self.run_cli("end", "done")[0], 2)

    def end_review(self, outcome, *opts):
        """Begin a review of ticket 7 at its head, then end it with `outcome`."""
        Path("tickets.json").write_text(json.dumps({"7": {"stage": "in-review", "head": "abc", "ci": "green",
                                                           "risk": "low", "pr": 5, "focus_missing": ["full"]}}))
        Path("event.json").write_text(json.dumps({**PAYLOAD, "label": {"name": "factory:ci-green"}}))
        with mock.patch.object(cli, "from_main", return_value=str(ROOT)):
            code, out = self.run_cli("begin", "review-full", "event.json", "--tracker", "memory:tickets.json")
            self.assertEqual(code, 0, out)
            return self.run_cli("end", outcome, *opts)

    def test_end_approve_needs_no_file(self):
        code, out = self.end_review("approve")
        self.assertEqual(code, 0, out)

    def test_end_changes_requires_blocking_finding(self):
        Path("f.json").write_text(json.dumps([{"id": "M1", "file": "x.py:1", "finding": "name it", "severity": "minor"}]))
        code, out = self.end_review("changes", "--findings", "f.json")
        self.assertEqual(code, 2, out)
        self.assertIn("requires a blocking finding", out)

    def test_end_done_refuses_failed_check(self):
        tracker = self.tracker()
        self.run_cli("begin", "implement-mid", "event.json", *tracker)
        Path("c.json").write_text(json.dumps([{"text": "tests pass", "result": "fail"}]))
        code, out = self.run_cli("end", "done", "--checks", "c.json")
        self.assertEqual(code, 2, out)
        self.assertEqual(json.loads(Path("tickets.json").read_text())["7"]["stage"], "in-dev")

    def test_end_carries_dispute_data(self):
        Path("a.json").write_text(json.dumps(["B1: disputed — intended"]))
        a = argparse.Namespace(outcome="done", findings=None, slices=None, checks=None, reason=None, summary=None,
                               size_estimate=None, addressed="a.json", disputed_focuses="behaviour,full")
        data = cli.handoff_of(a)
        self.assertEqual(data["findings_addressed"], ["B1: disputed — intended"])
        self.assertEqual(data["disputed_focuses"], ["behaviour", "full"])

    def test_begin_fix_prints_ci_failure(self):
        failure = [{"job": "tests", "step": "Run tests", "log_tail": "assert False"}]
        tracker = self.tracker(stage="changes-requested", ci="red", round_complete=True,
                               round_verdict="changes", ci_failure=failure)
        with mock.patch.object(cli, "from_main", return_value=str(ROOT)):
            code, out = self.run_cli("begin", "fix-cheap", "event.json", *tracker)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["context"]["ci_failure"], failure)

    def test_pull_request_node_runs_main_gate_not_the_checkouts(self):
        main = Path("main")
        shutil.copytree(ROOT, main, ignore=shutil.ignore_patterns(".git", ".factory", "tests"))
        (main / "factory/gate.py").write_text(
            'import json, sys\nprint(json.dumps({"gate": "main", "handoff_path": "h.json", "argv": sys.argv[1:]}))\n')
        Path("event.json").write_text("{}")
        pr_gate = mock.Mock(side_effect=AssertionError("the pull request's gate ran"))
        with mock.patch.object(cli, "from_main", return_value=str(main.resolve())), \
                mock.patch.object(cli, "load_engine"), mock.patch.object(cli.gate, "main", pr_gate):
            code, out = self.run_cli("begin", "fix-mid", "event.json")
            self.assertEqual((code, json.loads(out)["gate"]), (0, "main"), out)
            code, out = self.run_cli("end", "h.json")
            self.assertEqual((code, json.loads(out)["argv"][2]), (0, "apply"), out)
        pr_gate.assert_not_called()

    def test_begin_main_uses_existing_main_worktree(self):
        main = Path("main")
        main.mkdir()
        shutil.copy2(ROOT / "factory.descriptor.yaml", main / "factory.descriptor.yaml")
        (main / "factory").mkdir()
        (main / "factory/gate.py").write_text(
            'import json, sys\nprint(json.dumps({"gate": "main", "handoff_path": "h.json"}))\n')
        Path("event.json").write_text("{}")
        with mock.patch.object(cli, "from_main", return_value=str(main.resolve())), \
                mock.patch.object(cli, "load_engine"):
            code, out = self.run_cli("begin", "implement-cheap", "event.json", "--main")
        self.assertEqual((code, json.loads(out)["gate"]), (0, "main"), out)
        state = json.loads(cli.STATE.read_text())
        self.assertEqual(state["engine"], str(main.resolve()))
        self.assertEqual(state["team"], str(main.resolve()))

    def test_pr_nodes_match_deploy(self):
        self.assertEqual(cli.PR_NODES, cli.deploy.PR_TRIGGERED_NODES)


class ImportTimeBoundaryTest(unittest.TestCase):
    def test_pull_request_module_with_import_side_effect_never_runs(self):
        """A pull request that changes the engine cannot run code at import: begin imports main's modules."""
        git = lambda cwd, *a: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True,
                                             capture_output=True)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            origin, pr = tmp / "origin.git", tmp / "pr"
            git(tmp, "init", "--bare", "-b", "main", str(origin))
            files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split()
            for rel in files:
                if (ROOT / rel).is_file():
                    (pr / rel).parent.mkdir(parents=True, exist_ok=True)
                    (pr / rel).write_bytes((ROOT / rel).read_bytes())
            # main's gate is a stub: begin only has to reach it
            (pr / "factory/gate.py").write_text(
                'import json\nif __name__ == "__main__":\n    print(json.dumps({"gate": "main", "handoff_path": "h.json"}))\n')
            git(pr, "init", "-b", "main")
            git(pr, "remote", "add", "origin", str(origin))
            git(pr, "add", "-A")
            git(pr, "commit", "-m", "main")
            git(pr, "push", "origin", "main")
            git(pr, "checkout", "-b", "pr")
            marker = tmp / "pwned"
            for mod in ("gate", "config", "deploy", "nodes"):
                f = pr / "factory" / f"{mod}.py"
                f.write_text(f'open({str(marker)!r}, "w").write("x")\n' + f.read_text())
            Path(pr / "event.json").write_text("{}")
            outer_home = tmp / "outer-home"
            shared = outer_home / "factory-main" / "factory"
            shared.mkdir(parents=True)
            (shared / "gate.py").write_text(
                'import json\nif __name__ == "__main__":\n    print(json.dumps({"gate": "outer-home"}))\n')
            env = {**os.environ, "HOME": str(tmp / "isolated-home")}
            with mock.patch.dict(os.environ, {"HOME": str(outer_home)}):
                done = subprocess.run([sys.executable, str(pr / "factory/cli.py"), "begin", "fix-mid", "event.json"], cwd=pr,
                                      capture_output=True, text=True, env=env)
            self.assertFalse(marker.exists(), "the pull request's engine module ran at import")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout)["gate"], "main")


if __name__ == "__main__":
    unittest.main()

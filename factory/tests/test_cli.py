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
cfg.set_team(ROOT / "factory/tests/fixtures/team")
import cli  # noqa: E402

cli.load_engine(cli.HERE)

PAYLOAD = {"action": "labeled", "issue": {"number": 7, "title": "Add x", "updated_at": "T1"}}


class BeginEndTest(unittest.TestCase):
    def setUp(self):
        cfg.set_team(ROOT / "factory/tests/fixtures/team")
        env_patch = mock.patch.dict(os.environ, {"FACTORY_TEAM": str(ROOT / "factory/tests/fixtures/team")})
        env_patch.start()
        self.addCleanup(env_patch.stop)
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
        tracker = self.tracker(seen=["issues:7:labeled:None"])
        code, out = self.run_cli("begin", "implement-mid", "event.json", *tracker)
        self.assertEqual((code, json.loads(out)["reason"]), (10, "duplicate event"))

    def test_begin_accepts_ez_branch_from_payload(self):
        pr_event = {"action": "labeled", "pull_request": {"number": 300, "updated_at": "T2",
                    "head": {"ref": "refs/heads/ez/299-urgent-ci-log-fetch", "sha": "abc"}},
                    "label": {"name": "factory:ci-green"}}
        Path("event.json").write_text(json.dumps(pr_event))
        Path("tickets.json").write_text(json.dumps({"299": {"stage": "changes-requested", "branch": "ez/299-urgent-ci-log-fetch",
                                                               "pr": 300, "pr_state": "open", "round_complete": True,
                                                               "head": "abc"}}))
        with mock.patch.object(cli, "from_main", return_value=str(ROOT)):
            code, out = self.run_cli("begin", "fix-cheap", "event.json", "--tracker", "memory:tickets.json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["ticket"], 299)

    def test_begin_with_issue_number_only(self):
        self.tracker()
        Path("event.json").unlink()
        code, out = self.run_cli("begin", "implement-mid", "--issue", "7", "--tracker", "memory:tickets.json")
        self.assertEqual((code, json.loads(out)["ticket"]), (0, 7), out)

    def test_begin_with_pr_number_only(self):
        self.tracker(stage="changes-requested", pr=300, pr_state="open", round_complete=True)
        with mock.patch.object(cli, "from_main", return_value=str(ROOT)):
            code, out = self.run_cli("begin", "fix-cheap", "--pr", "300", "--tracker", "memory:tickets.json")
        self.assertEqual((code, json.loads(out)["ticket"]), (0, 7), out)

    def test_end_applies_handoff_for_begun_node(self):
        tracker = self.tracker()
        code, out = self.run_cli("begin", "implement-mid", "event.json", *tracker)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["handoff_path"], ".factory/handoff/implement.json")
        code, out = self.run_cli("end", "done")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(Path("tickets.json").read_text())["7"]["stage"], "in-review")

    def test_begin_lists_notes_for_role(self):
        mine, theirs = {"for": "developer", "hint": "see a.py"}, {"for": "reviewer", "hint": "skip"}
        tracker = self.tracker(agent_notes=[theirs, mine, {"for": "any", "hint": "all"}])
        code, out = self.run_cli("begin", "implement-mid", "event.json", *tracker)
        self.assertEqual(code, 0, out)
        got = json.loads(out)
        self.assertEqual([n["hint"] for n in got["agent_notes"]], ["see a.py", "all"])
        self.assertNotIn("agent_notes", got["context"])

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
        data = cli.handoff_of(cli.end_parser().parse_args(["done", "--addressed", "a.json", "--disputed-focuses", "behaviour,full"]))
        self.assertEqual(data["findings_addressed"], ["B1: disputed — intended"])
        self.assertEqual(data["disputed_focuses"], ["behaviour", "full"])

    def handoff(self, *argv):
        return cli.handoff_of(cli.end_parser().parse_args(list(argv)))

    def test_end_plain_text_checks(self):
        data = self.handoff("done", "--pass", "tests pass", "--fail", "lint", "--not-run", "e2e | no browser")
        self.assertEqual(data["checks"], [{"text": "tests pass", "result": "pass"}, {"text": "lint", "result": "fail"},
                                          {"text": "e2e", "reason": "no browser", "result": "not-run"}])
        with self.assertRaisesRegex(ValueError, "reason"):
            self.handoff("done", "--not-run", "e2e")

    def test_end_plain_text_findings(self):
        data = self.handoff("changes", "--blocking", "B1 src/x.py:10 the empty input crashes | case: x.run('') raises",
                            "--minor", "M1 src/y.py:3 name is vague")
        self.assertEqual(data["findings"], [
            {"id": "B1", "file": "src/x.py:10", "finding": "the empty input crashes", "severity": "blocking",
             "case": "x.run('') raises"},
            {"id": "M1", "file": "src/y.py:3", "finding": "name is vague", "severity": "minor"}])
        with self.assertRaisesRegex(ValueError, "case"):
            self.handoff("changes", "--blocking", "B1 src/x.py:10 crashes")

    def test_end_plain_text_addressed_match_the_gate_forms(self):
        data = self.handoff("done", "--fixed", "B1 abc123", "--disputed", "B2 the spec asks for it")
        self.assertEqual(data["findings_addressed"], ["B1: fixed in abc123", "B2: disputed — the spec asks for it"])

    def test_end_slice_file_and_proposed_checks(self):
        Path("s1.md").write_text("# Part one\n\nScope: a.\nSize: about 40 lines with tests\n")
        data = self.handoff("split", "--slice", "s1.md", "--propose-check", "uv run t.py passes")
        self.assertEqual(data["slices"], [{"title": "Part one", "body": "Scope: a.\nSize: about 40 lines with tests"}])
        self.assertEqual(data["proposed_checks"], ["uv run t.py passes"])

    def test_end_note_options_build_agent_notes(self):
        data = self.handoff("changes", "--note-for", "fixer", "--note-repro", "uv run t.py", "--note-file", "a.py",
                            "--note-file", "b.py")
        self.assertEqual(data["agent_notes"], {"for": "fixer", "files": ["a.py", "b.py"], "repro": "uv run t.py"})
        self.assertNotIn("agent_notes", self.handoff("approve"))

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
        self.assertEqual(state["team"], str((ROOT / "factory/tests/fixtures/team").resolve()))

    def test_sweep_main_uses_team_config(self):
        team = str((ROOT / "factory/tests/fixtures/team").resolve())
        with mock.patch.object(cli, "from_main", return_value=str(ROOT)), \
                mock.patch.object(cli, "load_engine"), \
                mock.patch.object(cli, "run_gate", return_value=0) as run_gate:
            code, _ = self.run_cli("--team", team, "sweep", "--main", "--issue", "7")
        self.assertEqual(code, 0)
        run_gate.assert_called_once_with(str(ROOT), ["sweep", "--issue", "7"], team)

    def test_sweep_main_uses_factory_team_environment(self):
        team = str((ROOT / "factory/tests/fixtures/team").resolve())
        with mock.patch.dict(os.environ, {"FACTORY_TEAM": team}), \
                mock.patch.object(cli, "from_main", return_value=str(ROOT)), \
                mock.patch.object(cli, "load_engine"), \
                mock.patch.object(cli, "run_gate", return_value=0) as run_gate:
            code, _ = self.run_cli("sweep", "--main", "--issue", "7")
        self.assertEqual(code, 0)
        run_gate.assert_called_once_with(str(ROOT), ["sweep", "--issue", "7"], team)

    def test_pr_nodes_match_deploy(self):
        self.assertEqual(cli.PR_NODES, cli.deploy.PR_TRIGGERED_NODES)

    def test_deploy_target_local_goes_to_the_local_target(self):
        with mock.patch("local.deploy", return_value=0) as local_deploy, \
                mock.patch.object(cli.deploy, "main", side_effect=AssertionError("the Air deploy ran")):
            code, _ = self.run_cli("deploy", "--target", "local", "--apply")
        self.assertEqual(code, 0)
        local_deploy.assert_called_once_with(["--apply"])

    def test_run_local_goes_to_the_local_loop(self):
        with mock.patch("local.main", return_value=0) as local_main:
            code, _ = self.run_cli("run", "--local", "--once")
        self.assertEqual(code, 0)
        local_main.assert_called_once_with(["--local", "--once"])


class PackagingTest(unittest.TestCase):
    def test_installed_command_ships_every_engine_module_it_imports(self):
        """Air installs `factory` with `uv tool install`: a module missing from py-modules fails only there (ImportError)."""
        import ast
        import tomllib
        shipped = set(tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["py-modules"])
        engine = {p.stem for p in (ROOT / "factory").glob("*.py")}
        imported = set()
        for mod in shipped:
            for node in ast.walk(ast.parse((ROOT / "factory" / f"{mod}.py").read_text())):
                if isinstance(node, ast.Import):
                    imported |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    imported.add(node.module.split(".")[0])
        self.assertEqual(sorted((imported & engine) - shipped), [])


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

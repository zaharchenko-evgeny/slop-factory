#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Tests for `factory begin` and `factory end` (factory/cli.py). Run: uv run factory/tests/test_cli.py"""
import contextlib
import io
import argparse
import json
import re
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
cfg.set_team(ROOT / "factory/tests/fixtures/v2")
import cli  # noqa: E402

cli.load_engine(cli.HERE)

V2 = ROOT / "factory/tests/fixtures/v2"


PAYLOAD = {"action": "labeled", "issue": {"number": 7, "title": "Add x", "updated_at": "T1"}}


class BeginEndTest(unittest.TestCase):
    def setUp(self):
        cfg.set_team(ROOT / "factory/tests/fixtures/v2")
        env_patch = mock.patch.dict(os.environ, {"FACTORY_TEAM": str(ROOT / "factory/tests/fixtures/v2")})
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

    def test_routine_begins_and_ends_without_a_ticket_or_payload(self):
        with mock.patch("sys.stdin", io.StringIO("")):
            code, out = self.run_cli("begin", "retro")
        self.assertEqual((code, json.loads(out)["go"], json.loads(out)["ticket"]), (0, True, None))
        code, out = self.run_cli("end", "nothing")
        self.assertEqual((code, json.loads(out)["applied"]), (0, True))

    def test_begin_exits_10_for_duplicate_event(self):
        tracker = self.tracker(seen=["issues:7:labeled:None"])
        code, out = self.run_cli("begin", "implement-medium", "event.json", *tracker)
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
            code, out = self.run_cli("begin", "fix", "event.json", "--tracker", "memory:tickets.json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["ticket"], 299)

    def test_begin_with_issue_number_only(self):
        self.tracker()
        Path("event.json").unlink()
        code, out = self.run_cli("begin", "implement-medium", "--issue", "7", "--tracker", "memory:tickets.json")
        self.assertEqual((code, json.loads(out)["ticket"]), (0, 7), out)

    def test_begin_takes_a_youtrack_ticket_id(self):  # #1319: a YouTrack gate owns only PROJECT-N
        self.tracker()
        Path("event.json").unlink()
        self.run_cli("begin", "implement-medium", "--issue", "LME-7", "--tracker", "memory:tickets.json")
        event = json.loads(Path(".factory/event/implement-medium.json").read_text())
        self.assertEqual(event["issue"], {"number": "LME-7"})

    def test_begin_issue_number_is_in_the_copys_repository(self):
        """#1155: a copy whose main is not the tracker (slop-eval-factory) reads `--issue 98` as its own repository's
        issue; Air sets the copy's main as FACTORY_ENV_REPOSITORY."""
        self.tracker()
        Path("event.json").unlink()
        with mock.patch.dict(os.environ, {"FACTORY_ENV_REPOSITORY": "o/team"}):
            os.environ.pop("FACTORY_NODE_REPO", None)
            self.run_cli("begin", "implement-medium", "--issue", "7", "--tracker", "memory:tickets.json")
        event = json.loads(Path(".factory/event/implement-medium.json").read_text())
        self.assertEqual(event["repository"], {"full_name": "o/team"})

    def test_begin_with_pr_number_only(self):
        self.tracker(stage="changes-requested", pr=300, pr_state="open", round_complete=True)
        with mock.patch.object(cli, "from_main", return_value=str(ROOT)):
            code, out = self.run_cli("begin", "fix", "--pr", "300", "--tracker", "memory:tickets.json")
        self.assertEqual((code, json.loads(out)["ticket"]), (0, 7), out)

    def test_end_applies_handoff_for_begun_node(self):
        tracker = self.tracker()
        code, out = self.run_cli("begin", "implement-medium", "event.json", *tracker)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["handoff_path"], ".factory/handoff/implement-medium.json")
        code, out = self.run_cli("end", "done-low")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(Path("tickets.json").read_text())["7"]["stage"], "in-review")

    def test_a_failing_apply_reports_the_error_on_the_ticket(self):
        tracker = self.tracker()
        code, out = self.run_cli("begin", "implement-medium", "event.json", *tracker)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(cli.STATE.read_text())["ticket"], 7)
        reports = []

        def failing_gate(engine, argv, team=None, stderr=False):
            print("RuntimeError: HTTP 403: Resource not accessible by integration", file=sys.stderr)
            return 1
        with mock.patch.object(cli, "run_gate", failing_gate), \
                mock.patch.object(cli, "report_end_failure", lambda state, outcome, err: reports.append((state["node"], outcome, err))):
            code, _ = self.run_cli("end", "done-low")
        self.assertEqual(code, 1)
        self.assertEqual([(n, o) for n, o, _ in reports], [("implement-medium", "done-low")])
        self.assertIn("HTTP 403", reports[0][2])

    def test_a_gate_refusal_on_stdout_is_quoted_in_the_report(self):
        tracker = self.tracker()
        code, out = self.run_cli("begin", "implement-medium", "event.json", *tracker)
        self.assertEqual(code, 0, out)
        reports = []

        def refusing_gate(engine, argv, team=None, stderr=False):
            print(json.dumps({"error": "payload: missing field `message`"}))
            return 2
        with mock.patch.object(cli, "run_gate", refusing_gate), \
                mock.patch.object(cli, "report_end_failure", lambda state, outcome, err: reports.append(err)):
            code, out = self.run_cli("end", "done-low")
        self.assertEqual(code, 2)
        self.assertEqual(reports, ["payload: missing field `message`"])
        self.assertIn("missing field", out)  # the agent still sees the gate's output (#1035)

    def with_env_broken(self):
        """The v2 fixture has no `env-broken` outcome: add the old one (a required check fails on unchanged main) to implement-medium."""
        import nodes
        load = nodes.load

        def patched(path=None):
            graph = load(path)
            graph["nodes"]["implement-medium"]["outcomes"]["env-broken"] = {"effects": [
                {"unlabel": "factory:implement-medium"}, {"park-behind-main-red": "failing_check"}, {"stage": "ready"}]}
            return graph
        return mock.patch.object(nodes, "load", patched)

    def test_end_env_broken_records_check_and_parks_ticket(self):
        tracker = self.tracker()
        with self.with_env_broken():
            code, out = self.run_cli("begin", "implement-medium", "event.json", *tracker)
            self.assertEqual(code, 0, out)
            code, out = self.run_cli("end", "env-broken", "--failing-check", "doc-references")
        self.assertEqual(code, 0, out)
        handoff = json.loads(Path(".factory/handoff/implement-medium.json").read_text())
        self.assertEqual(handoff["failing_check"], "doc-references")
        ticket = json.loads(Path("tickets.json").read_text())["7"]
        self.assertEqual(ticket["stage"], "ready")
        self.assertEqual(ticket["blocked_by"], ["red-doc-references"])
        self.assertEqual(json.loads(Path("tickets.json").read_text())["red-doc-references"]["title"],
                         "Main is red in Air: doc-references")

    def test_end_env_broken_requires_failing_check(self):
        self.tracker()
        with self.with_env_broken():
            code, out = self.run_cli("begin", "implement-medium", "event.json", "--tracker", "memory:tickets.json")
            self.assertEqual(code, 0, out)
            code, out = self.run_cli("end", "env-broken")
        self.assertNotEqual(code, 0)
        self.assertIn("--failing-check", out)

    def test_begin_lists_notes_for_role(self):
        mine, theirs = {"for": "developer", "hint": "see a.py"}, {"for": "reviewer", "hint": "skip"}
        tracker = self.tracker(agent_notes=[theirs, mine, {"for": "any", "hint": "all"}])
        code, out = self.run_cli("begin", "implement-medium", "event.json", *tracker)
        self.assertEqual(code, 0, out)
        got = json.loads(out)
        self.assertEqual([n["hint"] for n in got["agent_notes"]], ["see a.py", "all"])
        self.assertNotIn("agent_notes", got["context"])

    def as_instructed(self):
        """The engine's implement node opening its own PR (#1261): `done` labels the draft PR for the factory review."""
        import nodes
        load = nodes.load

        def patched(path=None):
            graph = load(path)
            node = graph["nodes"]["implement-medium"]
            node["output"] = "as-instructed"
            return graph
        return mock.patch.object(nodes, "load", patched)

    def test_check_prints_the_draft_pr_command_of_an_as_instructed_implement_node(self):
        tracker = self.tracker()
        with self.as_instructed():
            code, out = self.run_cli("begin", "implement-medium", "event.json", *tracker)
        self.assertEqual(code, 0, out)
        got = json.loads(out)
        self.assertEqual(got["pr_create"]["done-low"], f"gh pr create --draft --head {got['branch']} --label factory:review-low "
                                                   '--title "#7 <subject>" --body-file .factory/pr-body.md')

    def test_end_done_without_a_pr_is_refused_for_an_as_instructed_node(self):
        tracker = self.tracker()
        with self.as_instructed():
            self.run_cli("begin", "implement-medium", "event.json", *tracker)
            code, out = self.run_cli("end", "done-low")
        self.assertEqual(code, 2, out)
        self.assertIn("no PR for the ticket's branch: open it as a draft first", out)
        self.assertEqual(json.loads(Path("tickets.json").read_text())["7"]["stage"], "in-dev")

    def test_end_without_begin_is_an_error(self):
        self.assertEqual(self.run_cli("end", "done-low")[0], 2)

    def end_review(self, outcome, *opts):
        """Begin a review of ticket 7 at its head, then end it with `outcome`."""
        Path("tickets.json").write_text(json.dumps({"7": {"stage": "in-review", "head": "abc", "ci": "green",
                                                           "risk": "low", "pr": 5, "focus_missing": ["full"]}}))
        Path("event.json").write_text(json.dumps({**PAYLOAD, "label": {"name": "factory:ci-green"}}))
        with mock.patch.object(cli, "from_main", return_value=str(ROOT)):
            code, out = self.run_cli("begin", "review-high", "event.json", "--tracker", "memory:tickets.json")
            self.assertEqual(code, 0, out)
            return self.run_cli("end", outcome, *opts)

    def test_end_approve_needs_no_file(self):
        code, out = self.end_review("approve")
        self.assertEqual(code, 0, out)

    def test_end_done_refuses_failed_check(self):
        """The gate refuses only an outcome named `done`; v2 names it `done-low`/`done-high` (engine gap, see #1394)."""
        import nodes
        load = nodes.load

        def with_done(path=None):
            graph = load(path)
            outcomes = graph["nodes"]["implement-medium"]["outcomes"]
            outcomes["done"] = outcomes["done-low"]
            return graph
        tracker = self.tracker()
        with mock.patch.object(nodes, "load", with_done):
            self.run_cli("begin", "implement-medium", "event.json", *tracker)
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

    def test_blocking_finding_unseen_part(self):
        data = self.handoff("changes", "--blocking", "B2 src/x.py:10 crashes | case: x('') raises | unseen: new branch")
        self.assertEqual(data["findings"][0]["case"], "x('') raises")
        self.assertEqual(data["findings"][0]["unseen"], "new branch")

    def test_end_field_option_sets_any_key(self):
        data = self.handoff("notified", "--field", "assignee=octo", "--field", "size-estimate=40")
        self.assertEqual((data["assignee"], data["size_estimate"]), ("octo", "40"))
        with self.assertRaisesRegex(ValueError, "KEY=VALUE"):
            self.handoff("notified", "--field", "novalue")

    def test_every_effect_field_has_an_end_option(self):
        """Every payload field an effect in a shipped workflow reads has its own named `factory end` option (#1048):
        a required field without one fails the agent's run (notify's `message` and `assignee`)."""
        import yaml
        fields = set()
        for wf in (ROOT / "factory/tests/fixtures/slop/workflow.yaml", ROOT / "factory/tests/fixtures/v2/workflow.yaml",
                   ROOT / "skills/factory-setup/template/workflow.yaml"):
            for node in (yaml.safe_load(wf.read_text()).get("nodes") or {}).values():
                for out in (node.get("outcomes") or {}).values():
                    for e in (out or {}).get("effects") or []:
                        if not isinstance(e, dict):
                            continue
                        kind, arg = next(iter(e.items()))
                        if kind in ("comment", "create-issues", "route", "reply", "park-behind-main-red", "set-field") \
                                and isinstance(arg, str):
                            fields.add(arg)
                        elif kind == "assign" and isinstance(arg, dict):
                            fields.add(arg["field"])
                        elif kind in ("label", "unlabel"):
                            fields |= set(re.findall(r"<([^>]+)>", str(arg)))
        fields -= {"risk", "tier", "focus",  # set by the gate, not the agent
                   "questions", "children"}  # derived by the gate from `--reason` and `--slice` (gate.intake payload)
        self.assertTrue(fields)
        for f in sorted(fields):
            with self.subTest(field=f):
                data = self.handoff("x", f"--{f.replace('_', '-')}", "v")
                self.assertEqual(data.get(f), "v")

    def test_slice_names_its_repository(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "s.md").write_text("# Team part\nrepo: zaharchenko-evgeny/slop-eval-factory\nThe body.\n")
            Path(d, "t.md").write_text("Engine part\nrepo: slop-factory\nafter: slop-eval-factory, 1\nThe body.\n")
            Path(d, "u.md").write_text("Plain\nThe body.\n")
            self.assertEqual(cli.slice_of(Path(d, "s.md")), {"title": "Team part", "body": "The body.",
                                                             "repo": "zaharchenko-evgeny/slop-eval-factory"})
            self.assertEqual(cli.slice_of(Path(d, "t.md")), {"title": "Engine part", "body": "The body.",
                                                             "repo": "slop-factory", "after": ["slop-eval-factory", 1]})
            self.assertEqual(cli.slice_of(Path(d, "u.md")), {"title": "Plain", "body": "The body."})

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

    def test_end_lesson_option(self):
        data = self.handoff("done", "--lesson", "factory router | it misses ADRs | evidence: #7",
                            "--lesson", "project gate.py | apply runs twice")
        self.assertEqual(data["lessons"], [
            {"scope": "factory", "about": "router", "text": "it misses ADRs", "evidence": "#7"},
            {"scope": "project", "about": "gate.py", "text": "apply runs twice"}])
        self.assertNotIn("lessons", self.handoff("done"))

    def test_end_lesson_bad_scope(self):
        for bad in ("team router | text", "factory router | ", "factory | text"):
            with self.assertRaises(ValueError, msg=bad):
                self.handoff("done", "--lesson", bad)
        self.tracker()
        self.assertEqual(self.run_cli("begin", "implement-medium", "event.json", "--tracker", "memory:tickets.json")[0], 0)
        code, out = self.run_cli("end", "done-low", "--lesson", "team router | text")
        self.assertEqual(code, 2, out)
        self.assertIn("malformed option", json.loads(out)["error"])

    def test_end_lessons_reach_gate(self):
        tracker = self.tracker()
        self.assertEqual(self.run_cli("begin", "implement-medium", "event.json", *tracker)[0], 0)
        code, out = self.run_cli("end", "done-low", "--lesson", "engine cli.py | end dropped lessons | evidence: #869")
        self.assertEqual(code, 0, out)
        record = json.loads(Path("tickets.json").read_text())["7"]
        comments = [json.loads(v) for k, v in record["log"] if k == "comment" and "factory:lesson" in v]
        self.assertEqual(len(comments), 1, record["log"])
        self.assertIn("- [engine] cli.py: end dropped lessons (evidence: #869)", comments[0])
        self.assertIn("factory:lesson", record["attrs"])

    def test_begin_fix_prints_ci_failure(self):
        failure = [{"job": "tests", "step": "Run tests", "log_tail": "assert False"}]
        tracker = self.tracker(stage="changes-requested", ci="red", round_complete=True,
                               round_verdict="changes", ci_failure=failure)
        with mock.patch.object(cli, "from_main", return_value=str(ROOT)):
            code, out = self.run_cli("begin", "fix", "event.json", *tracker)
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["context"]["ci_failure"], failure)

    def test_sweep_main_uses_team_config(self):
        team = str((ROOT / "factory/tests/fixtures/v2").resolve())
        with mock.patch.object(cli, "from_main", return_value=str(ROOT)), \
                mock.patch.object(cli, "load_engine"), \
                mock.patch.object(cli, "run_gate", return_value=0) as run_gate:
            code, _ = self.run_cli("--team", team, "sweep", "--main", "--issue", "7")
        self.assertEqual(code, 0)
        run_gate.assert_called_once_with(str(ROOT), ["sweep", "--issue", "7"], team)

    def test_sweep_main_uses_factory_team_environment(self):
        team = str((ROOT / "factory/tests/fixtures/v2").resolve())
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
        with mock.patch("factory_plugins.local.surface.deploy", return_value=0) as local_deploy, \
                mock.patch.object(cli.deploy, "main", side_effect=AssertionError("the Air deploy ran")):
            code, _ = self.run_cli("deploy", "--target", "local", "--apply")
        self.assertEqual(code, 0)
        local_deploy.assert_called_once_with(["--apply"])

    def test_run_local_goes_to_the_local_loop(self):
        with mock.patch("factory_plugins.local.surface.main", return_value=0) as local_main:
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


class SkillTest(unittest.TestCase):
    def test_note_for_values_match_the_gate(self):
        """#1060: a `--note-for` value the gate refuses fails the whole `factory end`, so the skill lists them exactly."""
        from derive import NOTE_FOR
        skill = (ROOT / "skills/factory-step/SKILL.md").read_text()
        listed = re.search(r"`--note-for <([^>]+)>`", skill)[1].replace("\\|", "|").split("|")
        self.assertEqual(sorted(listed), sorted(NOTE_FOR))


class EnvRolesTest(unittest.TestCase):
    def test_prints_roles_of_enabled_agent_nodes_in_the_environment(self):
        self.addCleanup(cfg.set_team, ROOT / "factory/tests/fixtures/v2")
        fixture = ROOT / "factory/tests/fixtures/slop"
        cfg.set_team(fixture)
        for env in ("engine", "team", "intake"):  # slop-eval-factory#96 (ADR-0027)
            self.assert_slop_roles(fixture, env, self.roles(env))
        self.assertIn("developer", self.roles("team"))  # a node of [engine, team] runs in team too
        self.assertEqual(self.roles("intake"), ["router"])

        with tempfile.TemporaryDirectory() as tmp:
            team = Path(tmp) / "team"
            shutil.copytree(fixture, team)
            factory = team / "factory.yaml"
            factory.write_text(factory.read_text().replace("agents:\n", "agents:\n  extra-agent: { role: extra-role }\n"
                                                            "  disabled-agent: { role: disabled-only }\n", 1))
            workflow = team / "workflow.yaml"
            workflow.write_text(workflow.read_text().replace("nodes:\n", "nodes:\n"
                                    "  extra-role: { agent: extra-agent, enabled: true }\n"
                                    "  disabled-role: { agent: disabled-agent, enabled: false }\n", 1))
            for role in ("extra-role", "disabled-only"):
                (team / "roles" / role).mkdir()
                (team / "roles" / role / "role.md").write_text(f"# {role}\n")
            cfg.set_team(team)
            extended = self.roles("engine")
            self.assertIn("extra-role", extended)
            self.assertNotIn("disabled-only", extended)

    def assert_slop_roles(self, fixture, env, roles):
        self.assertEqual(roles, sorted(set(roles)), env)
        self.assertTrue(all((fixture / "roles" / role / "role.md").is_file()
                            for role in roles if role != "clerk"), env)

    def roles(self, env):
        out = io.StringIO()
        with contextlib.redirect_stdout(out): self.assertEqual(cli.main(["env", "roles", env]), 0)
        return out.getvalue().splitlines()


class CrashReportTest(unittest.TestCase):
    """#984: a crash in an Air run opens one Doctor ticket through gh alone."""
    def gh(self, open_titles):
        calls = []

        def run(args, **kw):
            calls.append(args)
            out = "\n".join(open_titles) if args[1:3] == ["issue", "list"] else "o/r" if args[1:3] == ["repo", "view"] else ""
            return subprocess.CompletedProcess(args, 0, out, "")
        return calls, run

    def test_air_crash_opens_the_ticket_once(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"FACTORY_ENV": "engine"}):
            os.environ.pop("GITHUB_REPOSITORY", None)
            calls, run = self.gh([])
            cli.report_crash("begin", FileNotFoundError("engine/defaults.yaml"), run=run)
            create = next(c for c in calls if c[1:3] == ["issue", "create"])
            self.assertEqual(create[create.index("--title") + 1], "Doctor: factory begin crashes")
            self.assertIn("FileNotFoundError: engine/defaults.yaml", create[create.index("--body") + 1])
            calls, run = self.gh(["Doctor: factory begin crashes"])
            cli.report_crash("begin", FileNotFoundError("x"), run=run)
            self.assertFalse([c for c in calls if c[1:3] == ["issue", "create"]])

    def test_local_crash_reports_nothing(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("FACTORY_ENV", None)
            calls, run = self.gh([])
            cli.report_crash("begin", RuntimeError("x"), run=run)
            self.assertEqual(calls, [])

    def test_crash_report_names_command_not_team_dir(self):
        from unittest import mock
        calls = []
        with mock.patch.object(cli, "report_crash", lambda c, e: calls.append(c)), \
                mock.patch.object(cli, "_main", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                cli.main(["--team", "/home/workspace/factory-team", "deploy", "--dry-run"])
        self.assertEqual(calls, ["deploy"])


class EndFieldsTest(unittest.TestCase):
    """#1035: every handoff field an effect reads can be written by a `factory end` option (or the gate derives it)."""
    FILES = ("factory/tests/fixtures/slop/workflow.yaml", "skills/factory-setup/template/workflow.yaml")
    READS = ("comment", "create-issues", "set-field", "route", "reply", "park-behind-main-red")

    def effect_fields(self, doc):
        found = set()
        def walk(x):
            if isinstance(x, dict):
                for e in x.get("effects") or [] if isinstance(x.get("effects"), list) else []:
                    if isinstance(e, dict):
                        found.update(v for k, v in e.items() if k in self.READS and isinstance(v, str))
                        found.update(v["field"] for k, v in e.items()
                                     if k in ("assign", "unassign", "request-reviewers") and isinstance(v, dict) and "field" in v)
                        found.update(re.findall(r"<([^>]+)>", " ".join(str(e.get(k, "")) for k in ("label", "unlabel"))))
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(doc)
        return found

    def test_end_writes_every_effect_field(self):
        import yaml
        import gate
        root = Path(__file__).resolve().parents[2]
        args = ["x", "--size-estimate", "1", "--reviewer", "x"]
        for action in cli.end_parser()._actions:
            opt = next((o for o in action.option_strings if o.startswith("--")), None)
            if opt and isinstance(action, argparse._StoreAction) and action.dest not in ("findings", "slices", "checks", "addressed", "notes"):
                args += [opt, "1" if action.type is int else "x"]
        handoff = cli.handoff_of(cli.end_parser().parse_args(args))
        written = set(handoff) | set(gate.intake_fields(handoff))
        for name in self.FILES:
            with self.subTest(name):
                missing = self.effect_fields(yaml.safe_load((root / name).read_text())) - written
                self.assertEqual(missing, set(), f"no `factory end` option writes {sorted(missing)}")

    def test_end_reviewer_writes_the_reviewers_list(self):  # #1262
        args = cli.end_parser().parse_args(["ask-human", "--reviewer", "alice", "--reviewer", "bob"])
        self.assertEqual(cli.handoff_of(args)["reviewers"], ["alice", "bob"])

    def test_end_notified_message(self):
        self.assertEqual(cli.handoff_of(cli.end_parser().parse_args(["notified", "--message", "@o hi"]))["message"], "@o hi")
        self.assertEqual(cli.handoff_of(cli.end_parser().parse_args(["notified", "--assignee", "o"]))["assignee"], "o")


class EndFailureTest(unittest.TestCase):
    """#997: a failed `factory end` leaves one comment on the ticket that reconcile reads."""
    def test_comment_names_node_head_outcome_and_the_error(self):
        from unittest import mock
        calls = []
        with mock.patch.object(cli.config, "load", lambda: {"tracker": {"repository": "o/r"}}):
            cli.report_end_failure({"ticket": 7, "node": "review-low", "head": "abc123def456"}, "approve-low",
                                   "Traceback\nRuntimeError: HTTP 403: Resource not accessible by integration\n",
                                   run=lambda args, **kw: calls.append(args))
        args = calls[0]
        self.assertEqual(args[:5], ["gh", "issue", "comment", "7", "-R"])
        body = args[args.index("--body") + 1]
        self.assertTrue(body.startswith("[factory]\nend failed: node=review-low head=abc123def456 outcome=approve-low"))
        self.assertIn("HTTP 403", body)

    def test_a_memory_tracker_run_posts_nothing(self):
        calls = []
        cli.report_end_failure({"ticket": 7, "node": "implement", "tracker": ["--tracker", "tickets.json"]}, "done", "boom",
                               run=lambda args, **kw: calls.append(args))
        self.assertEqual(calls, [])

    def test_no_ticket_no_comment(self):
        calls = []
        cli.report_end_failure({"node": "reconcile"}, "reconciled", "boom", run=lambda args, **kw: calls.append(args))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Unit tests for factory/doctor.py with a stubbed gh. Run: uv run --quiet factory/tests/test_doctor.py"""
import base64
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent))
import config as cfg  # noqa: E402
cfg.set_team(HERE / "fixtures/team")
import doctor  # noqa: E402
import nodes  # noqa: E402
from factory_plugins.github import probes as github_probes  # noqa: E402
from factory_plugins.youtrack import probes as youtrack_probes  # noqa: E402

REPO = "o/r"


class FakeGh:
    """In-memory GitHub: creates issues and a PR, records every call, fails the calls matching `deny`."""

    def __init__(self, deny=(), open_tickets=()):
        self.deny, self.calls, self.issues, self.prs, self.tokens = deny, [], {}, {}, []
        self.open_tickets = list(open_tickets)

    def __call__(self, args, token=None):
        self.calls.append(args)
        self.tokens.append(token)
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
        if args[0] == "api" and "contents/" in args[-1]:
            path = args[-1].split("contents/", 1)[1]
            content = (ROOT / path).read_text()
            return json.dumps({"content": base64.b64encode(content.encode()).decode()})
        if args[0] == "api" and "/issues/" in args[-1] and not args[-1].endswith("timeline"):
            return json.dumps({"id": 999})
        return ""


def run(gh, env="read", **kw):
    return {r["probe"]: r for r in doctor.run_probes(REPO, env, nodes.load(), gh, "risk:low", **kw)}


class YouTrackDoctorTest(unittest.TestCase):
    def test_missing_tag_is_reported_and_nothing_is_written(self):
        calls = []

        def yt(method, path, body=None):
            calls.append(method)
            if path.startswith("/api/admin/projects?"):
                return [{"id": "0-1", "shortName": "LME"}]
            if path.startswith("/api/tags"):
                return [{"name": "factory:implement-mid"}]
            return [{"field": {"name": "State"}, "bundle": {"values": [{"name": "In Progress"}]}}]
        settings = {"tracker": {"url": "u", "project": "LME"},
                    "states": {"in-dev": {"store": {"field": {"name": "State", "value": "In Progress"}}}},
                    "commands": {"implement-<tier>": {"store": {"tag": "factory:implement-<tier>"}},
                                 "close": {"store": {"tag": "close"}}}}
        got = {r["probe"]: r["result"] for r in youtrack_probes.youtrack_probes(settings, ["mid"], {"YOUTRACK_TOKEN": "x"}, yt)}
        self.assertEqual(got, {"youtrack:token:YOUTRACK_TOKEN": "ok", "youtrack:project:LME": "ok",
                               "youtrack:tag:close": "missing", "youtrack:tag:factory:implement-mid": "ok",
                               "youtrack:field:State=In Progress": "ok"})
        self.assertEqual(set(calls), {"GET"})

    def test_unset_token_is_missing(self):
        got = youtrack_probes.youtrack_probes({"tracker": {"url": "u", "project": "LME"}}, [], {})
        self.assertEqual([r["result"] for r in got], ["missing"])


class ProjectWriteProbeTest(unittest.TestCase):
    SETTINGS = {"write_mode": "token", "repositories": [
        {"name": "main", "url": "https://github.com/o/r", "role": "primary"},
        {"name": "app", "url": "https://github.com/o/app", "role": "project"}]}

    def test_missing_write_token_in_token_mode_fails_and_writes_nothing(self):
        gh = FakeGh()
        got = github_probes.project_write_probes(self.SETTINGS, "ops", gh, {})
        self.assertEqual(got, [{"probe": "write:label:o/app", "result": "fail",
                                "detail": "FACTORY_WRITE_TOKEN is not set (write_mode: token)"}])
        self.assertEqual(gh.calls, [])

    def test_probe_label_is_created_and_deleted_with_the_write_token(self):
        gh = FakeGh()
        got = github_probes.project_write_probes(self.SETTINGS, "write", gh, {"FACTORY_WRITE_TOKEN": "pat"})
        self.assertEqual([(r["probe"], r["result"]) for r in got], [("write:label:o/app", "ok")])
        self.assertEqual([c[:2] + c[3:5] for c in gh.calls], [["label", "create", "-R", "o/app"], ["label", "delete", "-R", "o/app"]])
        self.assertEqual(gh.calls[0][2], gh.calls[1][2])
        self.assertEqual(gh.tokens, ["pat", "pat"])

    def test_issues_token_probe_uses_the_issues_token_and_names_a_missing_secret(self):
        settings = {**self.SETTINGS, "write_mode": "per-repo", "issues_token_env": "FACTORY_ISSUES_TOKEN",
                    "environments": {"read": {"secrets": []}}}  # deploy adds the issues token itself
        gh = FakeGh()
        got = github_probes.project_write_probes(settings, "read", gh, {"FACTORY_ISSUES_TOKEN": "iss"})
        self.assertEqual([(r["probe"], r["result"]) for r in got], [("write:label:o/app", "ok")])
        self.assertEqual(gh.tokens, ["iss", "iss"])
        gh = FakeGh()
        got = github_probes.project_write_probes(settings, "read", gh, {})
        self.assertEqual(got, [{"probe": "write:label:o/app", "result": "fail",
                                "detail": "FACTORY_ISSUES_TOKEN is not set (issues_token_env)"}])
        self.assertEqual(gh.calls, [])

    def test_an_environment_that_declares_the_token_is_probed_whatever_its_name(self):
        settings = {**self.SETTINGS, "environments": {"engine": {"secrets": ["FACTORY_WRITE_TOKEN"]}, "team": {"secrets": []}}}
        got = github_probes.project_write_probes(settings, "engine", FakeGh(), {"FACTORY_WRITE_TOKEN": "pat"})
        self.assertEqual([(r["probe"], r["result"]) for r in got], [("write:label:o/app", "ok")])
        self.assertEqual(github_probes.project_write_probes(settings, "team", FakeGh(), {}), [])

    def test_denied_write_without_token_mode_is_reported_and_read_env_is_not_probed(self):
        settings = {**self.SETTINGS, "write_mode": "per-repo"}
        gh = FakeGh(deny=("label create",))
        got = github_probes.project_write_probes(settings, "ops", gh, {})
        self.assertEqual([(r["probe"], r["result"]) for r in got], [("write:label:o/app", "denied")])
        self.assertEqual(gh.tokens, [None])
        self.assertEqual(github_probes.project_write_probes(self.SETTINGS, "read", gh, {}), [])


class DoctorTest(unittest.TestCase):
    def test_run_log_requests_latest_successful_run(self):
        api_calls = []
        run_calls = []

        def api(path):
            api_calls.append(path)
            return json.dumps({"workflow_runs": [{"id": 7}]})

        def run(args):
            run_calls.append(args)
            return "log line"

        self.assertEqual(github_probes.run_log(api, run, REPO), "log line")
        self.assertEqual(api_calls, [f"repos/{REPO}/actions/runs?per_page=1&status=success"])
        self.assertEqual(run_calls, [["run", "view", "7", "-R", REPO, "--log"]])

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

    def test_new_ticket_starts_in_triage_and_a_passing_probe_closes_it(self):
        gh = FakeGh()
        doctor.file_tickets(REPO, [{"probe": "reconcile", "result": "fail", "detail": "boom"}], gh)
        create = next(c for c in gh.calls if c[:2] == ["issue", "create"])
        self.assertEqual(create[create.index("--label") + 1], "factory:triage")
        gh.open_tickets = [(7, "Doctor: reconcile fails"), (8, "Doctor: write:label fails")]
        doctor.close_ticket(REPO, "reconcile", gh)
        self.assertEqual([c[:3] for c in gh.calls if c[:2] == ["issue", "close"]], [["issue", "close", "7"]])

    def test_missing_secret_ticket_says_where_and_what_to_set(self):
        gh = FakeGh()
        doctor.file_tickets(REPO, [{"probe": "env:ISSUES_TOKEN", "result": "missing", "detail": "engine",
                                    "help": "Issues read/write"}], gh)
        create = next(c for c in gh.calls if c[:2] == ["issue", "create"])
        body = create[create.index("--body") + 1]
        self.assertTrue(body.startswith("[factory] Set `ISSUES_TOKEN` in the Air environment of `engine`."), body)
        self.assertIn("Issues read/write", body)
        self.assertIn("as a secret", body)

    def test_denied_failure_ticket_comment_is_reported(self):
        title = "Doctor: write:comment fails"
        gh = FakeGh(deny=("issue comment",), open_tickets=[(7, title)])
        failed = [{"probe": "write:comment", "result": "denied", "detail": "forbidden"}]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            doctor.file_tickets(REPO, failed, gh)
        reports = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(reports), 1)
        self.assertEqual((reports[0]["probe"], reports[0]["result"]), ("report", "denied"))
        self.assertIn("write:comment", reports[0]["detail"])

    def test_denied_probe_branch_creation_does_not_create_issues(self):
        gh = FakeGh(deny=("git/refs",))
        got = run(gh)
        self.assertEqual(got["setup"]["result"], "denied")
        self.assertFalse(any(call[:2] == ["issue", "create"] for call in gh.calls))

    def test_missing_tool_reported_for_env(self):
        got = run(FakeGh(), "write", which=lambda t: None if t == "git" else "/bin/" + t)
        self.assertEqual(got["tool:git"]["result"], "missing")
        self.assertEqual(got["tool:gh"]["result"], "ok")
        self.assertEqual(got["skill:ponytail"]["result"], "ok")  # a developer skill, only in `write`
        self.assertNotIn("skill:ponytail", run(FakeGh(), "ops", which=lambda t: "/bin/" + t))

    def test_declared_variables_and_secrets_are_probed(self):
        data = nodes.load()
        data["environments"]["write"] = {**data["environments"]["write"], "variables": {"YOUTRACK_URL": "u"},
                                         "secrets": ["YOUTRACK_TOKEN", "OTHER"]}
        got = {r["probe"]: r for r in doctor.run_probes(REPO, "write", data, FakeGh(), "risk:low",
                                                        environ={"YOUTRACK_URL": "u", "OTHER": ""})}
        self.assertEqual([got[f"env:{n}"]["result"] for n in ("YOUTRACK_URL", "YOUTRACK_TOKEN", "OTHER")],
                         ["ok", "missing", "missing"])

    def test_main_exits_1_for_unset_declared_secret(self):
        data = nodes.load()
        data["environments"]["write"] = {**data["environments"]["write"], "secrets": ["YOUTRACK_TOKEN"]}
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"FACTORY_ENV": "write"}), mock.patch.object(doctor.nodes_mod, "load", return_value=data), \
                contextlib.redirect_stdout(out):
            os.environ.pop("YOUTRACK_TOKEN", None)
            self.assertEqual(doctor.main(run=FakeGh()), 1)
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertTrue(any(r["probe"] == "env:YOUTRACK_TOKEN" and r["result"] == "missing" for r in lines), lines)

    def test_no_role_needs_a_local_agent_harness(self):
        """Air runs every agent (#523): no role's need_tools names a harness CLI."""
        for env in ("read", "write", "ops"):
            self.assertFalse({"pi", "claude", "codex"} & set(doctor.env_needs(env, nodes.load())[0]), env)
        self.assertIn("git", doctor.env_needs("write", nodes.load())[0])

    def test_missing_team_skill_does_not_fall_back_to_engine_template(self):
        before = cfg.team_root()
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "roles").symlink_to(ROOT / "engine/template-v1/roles", target_is_directory=True)
            cfg.set_team(tmp)
            try:
                got = run(FakeGh(), "write")
            finally:
                cfg.set_team(before)
        self.assertEqual(got["skill:tdd"]["result"], "missing")

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



class FakeGate:
    """A gate tracker with fixed facts per ticket."""

    def __init__(self, facts):
        self._facts = facts

    def sweep_tickets(self, stages, open_only=False):
        return [n for n, f in self._facts.items() if f["stage"] in stages]

    def facts(self, n):
        return self._facts[n]

    stale_facts = facts


class EngineProbeTest(unittest.TestCase):
    def test_ci_label_against_ci_fact_names_ticket_and_both_values(self):
        gate = FakeGate({7: {"stage": "in-review", "ci": "none", "pr_labels": ["factory:ci-green"]}})
        got = doctor.facts_probe(gate, ["in-review"], lambda: [])
        self.assertEqual(got["result"], "fail")
        self.assertIn("#7 ci: gate fact none, GitHub label factory:ci-green", got["detail"])

    def test_merged_pr_before_merged_stage_and_missing_frontier_fail(self):
        gate = FakeGate({8: {"stage": "approved", "ci": "green", "pr_state": "merged", "pr": 3, "pr_labels": []},
                         9: {"stage": "ready", "blockers_open": 0, "assignees": []}})
        got = doctor.facts_probe(gate, ["approved", "ready"], lambda: [])
        self.assertIn("#8 stage: gate fact approved, GitHub PR #3 merged", got["detail"])
        self.assertIn("#9 frontier", got["detail"])
        self.assertEqual(doctor.facts_probe(gate, ["approved", "ready"], lambda: [9])["detail"].count("#9"), 0)

    def test_matching_facts_pass(self):
        gate = FakeGate({7: {"stage": "in-review", "ci": "green", "pr_labels": ["factory:ci-green"]}})
        self.assertEqual(doctor.facts_probe(gate, ["in-review"], lambda: [])["result"], "ok")

    def test_deploy_drift_allows_only_label_description_updates(self):
        labels = "update label factory:ready in o/r (description)\nunchanged\n"
        self.assertEqual(doctor.drift_probe(lambda: (labels, 0))["result"], "ok")
        got = doctor.drift_probe(lambda: (labels + "update automation [factory:review] (prompt)\n", 0))
        self.assertEqual((got["result"], got["detail"]), ("fail", "update automation [factory:review] (prompt)"))
        self.assertEqual(doctor.drift_probe(lambda: ("update label x in o/r (color, description)\n", 0))["result"], "fail")
        self.assertEqual(doctor.drift_probe(lambda: ("", 1))["result"], "fail")

    def test_probe_that_passes_again_closes_its_ticket_with_a_comment(self):
        gh = FakeGh(open_tickets=[(5, "Doctor: factory:facts fails"), (6, "Doctor: deploy:drift fails")])
        doctor.report_engine(REPO, [{"probe": "factory:facts", "result": "ok", "detail": ""},
                                    {"probe": "deploy:drift", "result": "fail", "detail": "x"}], gh)
        closes = [c for c in gh.calls if c[:2] == ["issue", "close"]]
        self.assertEqual([c[2] for c in closes], ["5"])
        self.assertIn("passes again", closes[0][closes[0].index("--comment") + 1])
        self.assertTrue(any(c[:3] == ["issue", "comment", "6"] for c in gh.calls))

    def test_stuck_ticket_is_listed_with_its_failing_guard(self):
        old = {"ready": "2026-01-01T00:00:00Z"}
        facts = {"stage": "ready", "attrs": ["factory:ready"], "label_at": {"factory:ready": "2026-01-01T00:00:00Z"},
                 "ci": "none", "blockers_open": 0}
        nodes = {"dev": {"when": {"stage": "in-dev"}}}
        now = datetime(2026, 1, 1, 7, tzinfo=timezone.utc)
        got = doctor.stuck_tickets(FakeGate({4: facts}), [4], [("dev", "factory:ready", 30)], nodes, now)
        self.assertEqual([n for n, _ in got], [4])
        self.assertIn("dev when.stage", got[0][1])
        soon = datetime(2026, 1, 1, 5, tzinfo=timezone.utc)
        self.assertEqual(doctor.stuck_tickets(FakeGate({4: facts}), [4], [("dev", "factory:ready", 30)], nodes, soon), [])


class SkillInstalledTest(unittest.TestCase):
    def test_skill_installed_by_factory_setup(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home:
            self.assertFalse(doctor.skill_installed("tdd", home, definition_team=True))
            (Path(home) / ".agents/skills/tdd").mkdir(parents=True)
            (Path(home) / ".agents/skills/tdd/SKILL.md").write_text("x")
            self.assertTrue(doctor.skill_installed("tdd", home, definition_team=True))


if __name__ == "__main__":
    unittest.main()

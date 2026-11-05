#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/local.py, the local target. Run: uv run factory/tests/test_local.py"""
import contextlib
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml
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

    def __init__(self, tools=("claude", "gh", "factory"), logged_in=True, origins=None):
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
        code, out = self.deploy(Machine(tools=("gh", "factory"), origins={"slop-factory": URL}))
        self.assertEqual(code, 1)
        self.assertIn("missing harness: `claude` (claude-code) is not on PATH", out)

    def test_every_role_harness_is_checked(self):
        path = self.team / "factory.descriptor.yaml"
        d = yaml.safe_load(path.read_text())
        d["roles"]["reviewer"]["harness"] = "junie"
        path.write_text(yaml.safe_dump(d))
        code, out = self.deploy(Machine(tools=("gh", "factory", "claude"), origins={"slop-factory": URL}))
        self.assertEqual(code, 1)
        self.assertIn("missing harness: `junie` (junie) is not on PATH", out)
        self.assertNotIn("`claude`", out)

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
        self.assertEqual((state["harnesses"], state["login"]), (["claude-code"], "alice"))
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
        self.assertEqual([(p["automation"], p["output"]) for p in plan],
                         [("implement-cheap", "create-pr"), ("implement-mid", "create-pr"), ("retro", "read-only")])
        self.assertEqual(plan[2]["schedules"], ["retro"])
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


class GitHub:
    """The `gh`, `git`, `factory` and harness calls of one `factory run --local` pass; records them in order."""

    def __init__(self, issues=None, prs=None, merged=(), begin_code=0, push_code=0, gh_down=False, branch="ez/7-add-x"):
        self.branch = branch
        self.issues, self.prs, self.merged, self.begin_code, self.calls = issues or {}, prs or {}, list(merged), begin_code, []
        self.push_code, self.gh_down = push_code, gh_down

    def kind(self, argv):
        if argv[:2] == ["gh", "pr"] and argv[2] == "create":
            return "pr-create"
        if argv[0] == "git" and "push" in argv:
            return "push"
        if argv[0] == "git" and "worktree" in argv:
            return "worktree"
        if argv[0] == "git" and "checkout" in argv:
            return "checkout"
        if argv[0] in ("claude", "codex", "pi"):
            return "harness"
        if argv[0] == "uv" and "begin" in argv:
            return "begin"
        if argv[0] == "uv" and "sweep" in argv:
            return "sweep"
        return None

    def __call__(self, argv, **kw):
        self.calls.append((self.kind(argv), argv))
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        rows = lambda items: ok("\n".join(json.dumps(x) for x in items))  # noqa: E731
        if self.gh_down and argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 1, "", "API rate limit exceeded")
        if argv[0] == "git" and "push" in argv and self.push_code:
            return subprocess.CompletedProcess(argv, self.push_code, "", "rejected")
        if argv[:3] in (["gh", "issue", "list"], ["gh", "pr", "list"]):
            if "--label" in argv:
                return rows((self.issues if argv[1] == "issue" else self.prs).get(argv[argv.index("--label") + 1], []))
            return rows(self.merged if "merged" in argv else [])
        if argv[:2] == ["gh", "api"]:
            n = int(argv[3].split("/")[-2])
            labels = {**{k: v for k, v in self.issues.items()}, **{k: v for k, v in self.prs.items()}}
            return rows([{"id": 100 + n, "name": label} for label, items in labels.items() if any(i["number"] == n for i in items)])
        if argv[0] == "uv" and "begin" in argv:
            return subprocess.CompletedProcess(argv, self.begin_code, json.dumps(
                {"go": True, "branch": self.branch, "checkouts": [], "context": {"title": "Add x"}}), "")
        if argv[0] == "git" and "rev-list" in argv:
            return ok("1\n")
        return ok()

    def kinds(self):
        return [k for k, _ in self.calls if k]


class RunLocalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        before, cwd = cfg.team_root(), os.getcwd()
        cfg.set_team(FIXTURE)
        os.chdir(self.tmp.name)
        self.addCleanup(lambda: (cfg.set_team(before), os.chdir(cwd)))
        Path(".factory").mkdir()
        Path(".factory/local.json").write_text(json.dumps(
            {"harness": "claude-code", "factory": str(FIXTURE), "checkouts": {"slop-factory": str(Path(self.tmp.name))}}))

    def run_local(self, gh, *argv, at=datetime.datetime(2026, 1, 1, 10, 18)):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = local.main(["--local", "--once", *argv], run=gh, now=lambda: at)
        return code, [json.loads(x) for x in out.getvalue().splitlines()]

    def test_implement_label_runs_begin_harness_push_and_pr_then_never_again(self):
        gh = GitHub(issues={"factory:implement-mid": [{"number": 7}]})
        code, out = self.run_local(gh)
        self.assertEqual((code, out), (0, [{"ran": "implement-mid", "event": "labeled:107", "result": "pushed"}]))
        kinds = [k for k in gh.kinds() if k in ("begin", "harness", "push", "pr-create")]
        self.assertEqual(kinds, ["begin", "harness", "push", "pr-create"])
        harness = next(a for k, a in gh.calls if k == "harness")
        self.assertIn("acceptEdits", harness)
        self.assertIn("`factory begin implement-mid --issue <the issue number>`", harness[2])  # the Air prompt
        self.assertIn(["-B", "ez/7-add-x"], [a[a.index("-B"):a.index("-B") + 2] for k, a in gh.calls if k == "checkout" and "-B" in a])
        create = next(a for k, a in gh.calls if k == "pr-create")
        self.assertEqual(create[create.index("--head") + 1], "ez/7-add-x")
        again = GitHub(issues={"factory:implement-mid": [{"number": 7}]})
        self.assertEqual(self.run_local(again), (0, []))
        self.assertNotIn("begin", again.kinds())

    def test_no_go_runs_no_harness(self):
        gh = GitHub(issues={"factory:implement-mid": [{"number": 7}]}, begin_code=10)
        self.assertEqual(self.run_local(gh)[1][0]["result"], "no-go")
        self.assertNotIn("harness", gh.kinds())

    def test_a_review_runs_read_only_in_a_detached_worktree_and_pushes_nothing(self):
        gh = GitHub(prs={"factory:review-full": [{"number": 5, "headRefName": "ez/7-add-x"}]})
        code, out = self.run_local(gh)
        self.assertEqual(out, [{"ran": "review-full", "event": "labeled:105", "result": "ran"}])
        self.assertNotIn("push", gh.kinds())
        self.assertNotIn("acceptEdits", next(a for k, a in gh.calls if k == "harness"))
        self.assertTrue(all("--detach" in a for k, a in gh.calls if k == "checkout"))

    def test_role_harness_mode_then_role_then_target(self):
        config = {"targets": {"local": {"harness": "codex"}},
                  "roles": {"reviewer": {"harness": "pi", "modes": {"full": {"harness": "junie"}}}, "router": {}}}
        self.assertEqual(local.role_harness(config, "reviewer", "full"), "junie")
        self.assertEqual(local.role_harness(config, "reviewer", "behaviour"), "pi")
        self.assertEqual(local.role_harness(config, "router"), "codex")
        self.assertEqual(local.role_harness({}, "router"), "claude-code")

    def test_local_models_prefer_the_local_target(self):
        self.assertEqual(local.local_models({"targets": {"local": {"models": {"mid": "a"}}}, "models": {"mid": "b"}}),
                         {"mid": "a"})
        self.assertEqual(local.local_models({"models": {"mid": "b"}}), {"mid": "b"})

    def test_junie_runs_the_task_headless(self):
        argv = local.harness_argv("junie", "anthropic/claude-x", True, "PROMPT")
        self.assertEqual(argv, ["junie", "--task", "PROMPT", "--model", "claude-x", "--output-format", "json"])

    def test_the_claude_prompt_is_not_taken_as_an_allowed_tool(self):
        for write in (False, True):
            argv = local.harness_argv("claude-code", "anthropic/claude-x", write, "PROMPT")
            self.assertEqual(argv[:3], ["claude", "-p", "PROMPT"])
            self.assertEqual(argv.count("PROMPT"), 1)

    def test_a_failed_push_is_reported_and_opens_no_pr(self):
        gh = GitHub(issues={"factory:implement-mid": [{"number": 7}]}, push_code=1)
        self.assertTrue(self.run_local(gh)[1][0]["result"].startswith("push failed: rejected"))
        self.assertNotIn("pr-create", gh.kinds())

    def test_a_failed_begin_is_tried_again_up_to_three_runs(self):
        results = []
        for _ in range(4):
            results += [o["result"][:12] for o in self.run_local(GitHub(issues={"factory:implement-mid": [{"number": 7}]}, begin_code=2))[1]]
        self.assertEqual(results, ["begin failed"] * 3)

    def test_github_down_fails_the_poll_and_records_nothing(self):
        code, out = self.run_local(GitHub(gh_down=True))
        self.assertEqual((code, list(out[0])), (1, ["poll failed"]))
        self.assertFalse(Path(".factory/local/seen.json").exists())

    def test_a_merged_pr_runs_the_finish_sweep_but_the_first_run_takes_old_merges_as_history(self):
        old = {"number": 4, "headRefName": "ez/6-old", "headRefOid": "h0"}
        self.assertEqual(self.run_local(GitHub(merged=[old])), (0, []))
        gh = GitHub(merged=[old, {"number": 5, "headRefName": "ez/7-add-x", "headRefOid": "h1"}])
        code, out = self.run_local(gh)
        self.assertEqual(out, [{"ran": "finish", "event": "merged:5", "result": "swept"}])
        self.assertNotIn("harness", gh.kinds())

    def test_dry_run_lists_and_records_nothing(self):
        gh = GitHub(issues={"factory:implement-mid": [{"number": 7}]})
        self.assertEqual(self.run_local(gh, "--dry-run")[1], [{"would_run": "implement-mid", "event": "labeled:107"}])
        self.assertFalse(Path(".factory/local/seen.json").exists())
        self.assertEqual([k for k in gh.kinds() if k], [])

    def test_without_the_deploy_record_it_refuses(self):
        Path(".factory/local.json").unlink()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(local.main(["--local", "--once"], run=GitHub()), 2)
        self.assertIn("factory deploy --target local --apply", out.getvalue())

    def test_a_due_schedule_runs_once_per_minute_key(self):
        at = datetime.datetime(2026, 1, 1, 10, 17)
        key = "schedule:reconcile:2026-01-01T10:17"
        code, out = self.run_local(GitHub(), at=at)
        self.assertEqual((code, [o["event"] for o in out]), (0, [key]))
        self.assertEqual(self.run_local(GitHub(), at=at), (0, []))
        self.assertIn(f"reconcile:{key}", json.loads(Path(".factory/local/seen.json").read_text()))

    def test_a_scheduled_agent_without_a_branch_still_reaches_the_harness(self):
        gh = GitHub(branch=None)
        code, out = self.run_local(gh, at=datetime.datetime(2026, 1, 1, 2, 0))  # retro, an agent node
        self.assertEqual((code, [o["result"] for o in out if o["event"].startswith("schedule:retro")]), (0, ["ran"]))
        self.assertIn("harness", gh.kinds())
        self.assertNotIn("push", gh.kinds())
        self.assertFalse(any(a[0] == "git" and "ls-remote" in a for _, a in gh.calls))

    def test_dry_run_lists_due_schedules_and_records_nothing(self):
        out = self.run_local(GitHub(), "--dry-run", at=datetime.datetime(2026, 1, 1, 10, 17))[1]
        self.assertEqual(out, [{"would_run": "reconcile", "event": "schedule:reconcile:2026-01-01T10:17"}])
        self.assertFalse(Path(".factory/local/seen.json").exists())

    def test_a_developer_worktree_gets_the_role_skills_excluded_from_git(self):
        tree = Path(self.tmp.name) / "tree"
        subprocess.run(["git", "init", "-q", str(tree)], check=True)
        local.install_skills(subprocess.run, "developer", "claude-code", tree)
        local.install_skills(subprocess.run, "developer", "claude-code", tree)
        for skill in (FIXTURE / "roles/developer/skills.txt").read_text().split():
            self.assertTrue((tree / ".claude/skills" / skill / "SKILL.md").is_file(), skill)
        self.assertTrue((tree / ".claude/skills/factory-step/SKILL.md").is_file())
        self.assertEqual((tree / ".git/info/exclude").read_text().split().count(".claude/skills/"), 1)
        self.assertEqual(subprocess.run(["git", "-C", str(tree), "status", "--porcelain"], capture_output=True, text=True).stdout, "")


class CronTest(unittest.TestCase):
    def test_minute_17_is_due_at_10_17_and_not_at_10_18(self):
        at = lambda m: datetime.datetime(2026, 1, 1, 10, m)  # noqa: E731
        self.assertTrue(local.cron_due("17 * * * *", at(17)))
        self.assertFalse(local.cron_due("17 * * * *", at(18)))

    def test_lists_ranges_and_steps(self):
        t = datetime.datetime(2026, 1, 5, 6, 30)  # a Monday
        for cron in ("*/10 * * * *", "0,30 6,18 * * *", "25-35 6 * * 1-5", "30 6 5 1 1"):
            self.assertTrue(local.cron_due(cron, t), cron)
        for cron in ("*/7 * * * *", "30 7 * * *", "30 6 * * 0", "30 6 6 * *"):
            self.assertFalse(local.cron_due(cron, t), cron)


if __name__ == "__main__":
    unittest.main()

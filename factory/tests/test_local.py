#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Tests for factory/factory_plugins/local/surface.py, the local target. Run: uv run factory/tests/test_local.py"""
import base64
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
import unittest.mock

import yaml
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
from factory_plugins.github import graphql  # noqa: E402
from factory_plugins.local import surface as local  # noqa: E402
import nodes  # noqa: E402
import plugins  # noqa: E402

FIXTURE = ROOT / "factory/tests/fixtures/v2"
URL = "https://github.com/zaharchenko-evgeny/slop-factory"
ORIGINS = {"slop-factory": URL, "slop-eval-factory": "https://github.com/zaharchenko-evgeny/slop-eval-factory"}  # the fixture's repositories
TOOLS = ("claude", "gh", "factory")  # the compiled v2 config names no harness: every role runs in claude-code


class Machine:
    """The laptop: which CLIs are on PATH, whether `gh` is logged in, and each checkout's origin."""

    def __init__(self, tools=TOOLS, logged_in=True, origins=None):
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

    def role_config(self, role, **keys):
        """The loaded config with `keys` set on `role` (the compiled v2 config has no role `harness` or `mcp`)."""
        real = cfg.load

        def load():
            config = real()
            config["roles"] = {**config["roles"], role: {**config["roles"][role], **keys}}
            return config
        return unittest.mock.patch.object(cfg, "load", load)

    def test_missing_harness_is_named(self):
        code, out = self.deploy(Machine(tools=("gh", "factory"), origins=ORIGINS))
        self.assertEqual(code, 1)
        self.assertIn("missing harness: `claude` (claude-code) is not on PATH", out)

    def test_every_role_harness_is_checked(self):
        with self.role_config("reviewer", harness="junie"):
            code, out = self.deploy(Machine(origins=ORIGINS))
        self.assertEqual(code, 1)
        self.assertIn("missing harness: `junie` (junie) is not on PATH", out)
        self.assertNotIn("`claude`", out)

    def test_an_unknown_harness_is_missing_with_the_known_ones(self):
        with self.role_config("reviewer", harness="aider"):
            code, out = self.deploy(Machine(origins=ORIGINS))
        self.assertEqual(code, 1)
        self.assertIn("missing harness: unknown-agent: harness: aider: no selected plugin provides this agent", out)

    def test_gh_not_logged_in_refuses(self):
        code, out = self.deploy(Machine(logged_in=False, origins=ORIGINS))
        self.assertEqual(code, 1)
        self.assertIn("missing gh: not logged in", out)

    def test_a_checkout_with_another_origin_is_missing(self):
        code, out = self.deploy(Machine(origins={**ORIGINS, "slop-factory": "git@github.com:someone/else.git"}))
        self.assertEqual(code, 1)
        self.assertIn(f"missing checkout: slop-factory at {self.base / 'slop-factory'}", out)

    def test_apply_records_the_laptop(self):
        code, out = self.deploy(Machine(origins={**ORIGINS, "slop-factory": "git@github.com:zaharchenko-evgeny/slop-factory.git"}), "--apply")
        self.assertEqual(code, 0, out)
        state = json.loads((Path(self.tmp.name) / ".factory/local.json").read_text())
        self.assertEqual((state["harnesses"], state["login"]), (["claude-code"], "alice"))
        self.assertEqual(state["checkouts"], {name: str(self.base / name) for name in ORIGINS})
        self.assertIn("implement-medium", state["automations"])

    def test_role_connectors_are_warned_as_not_provided(self):
        with self.role_config("developer", mcp=["youtrack"]):
            code, out = self.deploy(Machine(origins=ORIGINS))
        self.assertEqual(code, 0, out)
        self.assertIn("warning: role developer: MCP connectors not provided locally: youtrack", out)

    def test_dry_run_writes_nothing(self):
        code, _ = self.deploy(Machine(origins=ORIGINS))
        self.assertEqual(code, 0)
        self.assertFalse((Path(self.tmp.name) / ".factory/local.json").exists())

    def test_unknown_argument_prints_usage(self):
        self.assertEqual(self.deploy(Machine(), "--bogus")[0], 2)

    def test_isolated_deploy_checks_token_skips_failed_executor_probe_and_warns(self):
        real = cfg.load
        # 1. failed executor probe skips container CLI probe (no duplicate prepare failure) and does not require host `factory`
        def load_sbx(extra=None):
            ex_opts = {"type": "docker-sandbox", "template": "reg/sbx:1", **(extra or {})}
            return lambda: {**real(), "executors": {"sbx": ex_opts},
                            "targets": {**real()["targets"], "local": {**(real()["targets"].get("local") or {}), "executor": "sbx"}}}
        with unittest.mock.patch.object(cfg, "load", load_sbx()):
            code, out = self.deploy(Machine(tools=("gh",), origins=ORIGINS))
        self.assertEqual(code, 1)
        self.assertIn("`sbx` is not on PATH", out)
        self.assertNotIn("factory: not on PATH", out)

        # 2. ok executor probe but missing GITHUB_TOKEN mapping fails deploy cleanly
        class SbxMachine(Machine):
            def run(self, argv, **kw):
                if argv[0] == "sbx":
                    self.calls.append(argv)
                    return subprocess.CompletedProcess(argv, 0, "sbx 0.54.1\n", "")
                return super().run(argv, **kw)
        with unittest.mock.patch.object(cfg, "load", load_sbx({"secrets": {"GITHUB_TOKEN": "MISSING_TOK"}})):
            code, out = self.deploy(SbxMachine(tools=("gh", "sbx"), origins=ORIGINS))
        self.assertEqual(code, 1)
        self.assertIn("GITHUB_TOKEN is not mapped/exported", out)

        # 3. auth_files and secrets_mode=env emit warnings on a passing deploy
        with unittest.mock.patch.object(cfg, "load", load_sbx({"secrets": {"GITHUB_TOKEN": "OK_TOK"},
                                                               "auth_files": ["claude-code"], "secrets_mode": "env"})), \
                unittest.mock.patch.dict(os.environ, {"OK_TOK": "t1"}):
            code, out = self.deploy(SbxMachine(tools=("gh", "sbx"), origins=ORIGINS))
        self.assertEqual(code, 0, out)
        self.assertIn("warning: executor sbx: auth_files copies host logins", out)
        self.assertIn("warning: executor sbx: secrets_mode=env passes secrets via `sbx exec -e`", out)


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
        graph["nodes"]["hook"] = {"role": "retro", "env": "ops", "triggers": ["webhook dry-run-finished"]}
        self.assertNotIn("hook", [p["automation"] for p in local.plan_local(graph, {"roles": {"developer": {"tier": "mid"}, "retro": {"tier": "cheap"}}})])
        self.assertEqual(plan[1]["triggers"][0]["filters"], {"label": "factory:implement-mid"})

    def test_the_team_graph_has_the_review_and_finish_automations(self):
        before = cfg.team_root()
        cfg.set_team(FIXTURE)
        try:
            names = {p["automation"] for p in local.plan_local(nodes.load(), cfg.load())}
        finally:
            cfg.set_team(before)
        self.assertTrue({"intake", "implement-medium", "review-high", "finish"} <= names, names)

    def test_same_repository(self):
        self.assertTrue(local.same_repository(URL, "git@github.com:zaharchenko-evgeny/slop-factory.git\n"))
        self.assertTrue(local.same_repository(URL + "/", URL + ".git"))
        self.assertFalse(local.same_repository(URL, "https://github.com/zaharchenko-evgeny/slop-eval-factory"))


def node_id(rest):
    """The GraphQL node id of a labeled event whose REST id is `rest` (msgpack [0, uint64], as GitHub writes them)."""
    return "LE_" + base64.urlsafe_b64encode(bytes([0x92, 0x00, 0xcf]) + rest.to_bytes(8, "big")).decode().rstrip("=")


class GitHub:
    """The `gh`, `git`, `factory` and harness calls of one `factory run --local` pass; records them in order.
    The poll search answers with each issue and PR of `issues`/`prs`; its labeled event of issue n has REST id 100 + n."""

    def __init__(self, issues=None, prs=None, merged=(), begin_code=0, push_code=0, gh_down=False, branch="ez/7-add-x",
                 bad_ids=False):
        self.branch, self.bad_ids = branch, bad_ids
        self.issues, self.prs, self.merged, self.begin_code, self.calls = issues or {}, prs or {}, list(merged), begin_code, []
        self.push_code, self.gh_down = push_code, gh_down

    def kind(self, argv):
        if argv[:2] in (["docker", "exec"], ["sbx", "exec"]):
            if "begin" in argv:
                return "begin"
            if any(a in ("claude", "codex", "pi") for a in argv):
                return "harness"
        if argv[:2] in (["docker", "run"], ["sbx", "create"]):
            return "prepare"
        if argv[:3] == ["sbx", "policy", "allow"]:
            return "policy"
        if argv[:3] == ["sbx", "secret", "set"]:
            return "secret"
        if argv[:3] in (["docker", "rm", "-f"], ["sbx", "rm", "--force"]):
            return "teardown"
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
            return "precheck" if "--check-only" in argv else "begin"
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
        if argv[:3] == ["gh", "api", "graphql"]:
            return ok(json.dumps({"data": {"search": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self.search_nodes()}}}))
        if argv[:2] == ["gh", "api"]:
            n = int(argv[3].split("/")[-2])
            labels = {**{k: v for k, v in self.issues.items()}, **{k: v for k, v in self.prs.items()}}
            return rows([{"id": 100 + n, "name": label} for label, items in labels.items() if any(i["number"] == n for i in items)])
        if (argv[0] == "uv" or argv[:2] in (["docker", "exec"], ["sbx", "exec"])) and "begin" in argv:
            return subprocess.CompletedProcess(argv, self.begin_code, json.dumps(
                {"go": True, "branch": self.branch, "checkouts": [], "context": {"title": "Add x"}}), "")
        if argv[0] == "git" and "rev-list" in argv:
            return ok("1\n")
        return ok()

    def search_nodes(self):
        nodes = {}
        for pr, table in ((False, self.issues), (True, self.prs)):
            for label, items in table.items():
                for item in items:
                    n = item["number"]
                    node = nodes.setdefault((pr, n), {"number": n, "__typename": "PullRequest" if pr else "Issue",
                                                    "headRefName": item.get("headRefName"), "labels": {"nodes": []},
                                                    "timelineItems": {"pageInfo": {"hasPreviousPage": False}, "nodes": []}})
                    node["labels"]["nodes"].append({"name": label})
                    node["timelineItems"]["nodes"].append({"id": "LE_?" if self.bad_ids else node_id(100 + n), "label": {"name": label}})
        return list(nodes.values())

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
        # the compiled v2 config has no model aliases for the local target: give each tier one
        real = cfg.load
        load = lambda: {**real(), "targets": {**real()["targets"], "local": {"models": {t: "anthropic/claude-x" for t in ("luna", "sonnet", "sol")}}}}
        models = unittest.mock.patch.object(cfg, "load", load)
        models.start()
        self.addCleanup(models.stop)
        # no run checkouts under the workspace: the tracker's `gh` calls run through the test's runner, as on a laptop
        env = unittest.mock.patch.dict(os.environ, {"FACTORY_WORKSPACES": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        Path(".factory").mkdir()
        Path(".factory/local.json").write_text(json.dumps(
            {"harness": "claude-code", "factory": str(FIXTURE), "checkouts": {"slop-factory": str(Path(self.tmp.name))}}))

    def run_local(self, gh, *argv, at=datetime.datetime(2026, 1, 1, 10, 18)):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = local.main(["--local", "--once", *argv], run=gh, now=lambda: at)
        return code, [json.loads(x) for x in out.getvalue().splitlines()]

    def test_implement_label_runs_begin_harness_push_and_pr_then_never_again(self):
        gh = GitHub(issues={"factory:implement-medium": [{"number": 7}]})
        code, out = self.run_local(gh)
        self.assertEqual((code, out), (0, [{"ran": "implement-medium", "event": "labeled:107", "result": "pushed"}]))
        kinds = [k for k in gh.kinds() if k in ("begin", "harness", "push", "pr-create")]
        self.assertEqual(kinds, ["begin", "harness", "push", "pr-create"])
        harness = next(a for k, a in gh.calls if k == "harness")
        self.assertIn("acceptEdits", harness)
        self.assertIn("`factory begin implement-medium --issue <the issue number>`", harness[2])  # the Air prompt
        self.assertIn(["-B", "ez/7-add-x"], [a[a.index("-B"):a.index("-B") + 2] for k, a in gh.calls if k == "checkout" and "-B" in a])
        create = next(a for k, a in gh.calls if k == "pr-create")
        self.assertEqual(create[create.index("--head") + 1], "ez/7-add-x")
        again = GitHub(issues={"factory:implement-medium": [{"number": 7}]})
        self.assertEqual(self.run_local(again), (0, []))
        self.assertNotIn("begin", again.kinds())

    def test_no_go_runs_no_harness(self):
        gh = GitHub(issues={"factory:implement-medium": [{"number": 7}]}, begin_code=10)
        self.assertEqual(self.run_local(gh)[1][0]["result"], "no-go")
        self.assertNotIn("harness", gh.kinds())

    def test_a_review_runs_read_only_in_a_detached_worktree_and_pushes_nothing(self):
        gh = GitHub(prs={"factory:review-low": [{"number": 5, "headRefName": "ez/7-add-x"}]})
        code, out = self.run_local(gh)
        self.assertEqual(out, [{"ran": "review-low", "event": "labeled:105", "result": "ran"}])
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

    def test_role_executor_mode_then_role_then_target_then_host(self):
        config = {"targets": {"local": {"executor": "box"}},
                  "roles": {"reviewer": {"executor": "sbx", "modes": {"full": {"executor": "none"}}}, "router": {}}}
        self.assertEqual(local.role_executor(config, "reviewer", "full"), "none")
        self.assertEqual(local.role_executor(config, "reviewer", "behaviour"), "sbx")
        self.assertEqual(local.role_executor(config, "router"), "box")
        self.assertEqual(local.role_executor({}, "router"), "host")

    def test_host_executor_runs_argv_and_a_timeout_is_exit_124(self):
        import subprocess
        ex = local.executor({}, "host", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "out", ""))
        self.assertEqual((ex.isolated, ex.exec(None, ["x"]).stdout), (False, "out"))

        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        done = local.executor({"executors": {"h": {"type": "host", "timeout": 5}}}, "h", slow).exec(None, ["x"])
        self.assertEqual(done.returncode, 124)

    def test_an_unknown_executor_names_the_known_ones(self):
        with self.assertRaisesRegex(ValueError, "unknown-executor.*host"):
            local.executor({}, "nope")

    def test_local_models_prefer_the_local_target(self):
        self.assertEqual(local.local_models({"targets": {"local": {"models": {"mid": "a"}}}, "models": {"mid": "b"}}),
                         {"mid": "a"})
        self.assertEqual(local.local_models({"models": {"mid": "b"}}), {"mid": "b"})

    def test_junie_runs_the_task_headless(self):
        argv = local.runner("junie").command("PROMPT", "anthropic/claude-x", True)
        self.assertEqual(argv, ["junie", "--task", "PROMPT", "--model", "claude-x", "--output-format", "json"])

    def test_the_claude_prompt_is_not_taken_as_an_allowed_tool(self):
        for write in (False, True):
            argv = local.runner("claude-code").command("PROMPT", "anthropic/claude-x", write)
            self.assertEqual(argv[:3], ["claude", "-p", "PROMPT"])
            self.assertEqual(argv.count("PROMPT"), 1)

    def test_a_failed_push_is_reported_and_opens_no_pr(self):
        gh = GitHub(issues={"factory:implement-medium": [{"number": 7}]}, push_code=1)
        self.assertTrue(self.run_local(gh)[1][0]["result"].startswith("push failed: rejected"))
        self.assertNotIn("pr-create", gh.kinds())

    def test_a_failed_begin_is_tried_again_up_to_three_runs(self):
        results = []
        for _ in range(4):
            results += [o["result"][:12] for o in self.run_local(GitHub(issues={"factory:implement-medium": [{"number": 7}]}, begin_code=2))[1]]
        self.assertEqual(results, ["begin failed"] * 3)

    def test_github_down_fails_the_poll_and_records_nothing(self):
        code, out = self.run_local(GitHub(gh_down=True))
        self.assertEqual((code, list(out[0])), (1, ["poll failed"]))
        self.assertFalse(Path(".factory/local/seen.json").exists())

    def test_a_merged_pr_runs_the_finish_sweep_but_the_first_run_takes_old_merges_as_history(self):
        old = {"number": 4, "headRefName": "ez/6-old", "headRefOid": "h0"}
        self.assertEqual(self.run_local(GitHub(merged=[old])), (0, []))
        gh = GitHub(merged=[old, {"number": 5, "headRefName": "ez/7-add-x", "headRefOid": "h1"}])
        load = nodes.load

        def scripted(path=None):  # v2's finish is a `clerk` agent (`run: decide`); the old one was a script node
            graph = load(path)
            graph["nodes"]["finish"]["role"] = "script"
            return graph
        with unittest.mock.patch.object(nodes, "load", scripted):
            code, out = self.run_local(gh)
        self.assertEqual(out, [{"ran": "finish", "event": "merged:5", "result": "swept"}])
        self.assertNotIn("harness", gh.kinds())

    def test_dry_run_lists_and_records_nothing(self):
        gh = GitHub(issues={"factory:implement-medium": [{"number": 7}]})
        self.assertEqual(self.run_local(gh, "--dry-run")[1], [{"would_run": "implement-medium", "event": "labeled:107"}])
        self.assertFalse(Path(".factory/local/seen.json").exists())
        self.assertEqual([k for k in gh.kinds() if k], [])

    def test_without_the_deploy_record_it_refuses(self):
        Path(".factory/local.json").unlink()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(local.main(["--local", "--once"], run=GitHub()), 2)
        self.assertIn("factory deploy --target local --apply", out.getvalue())

    def test_a_due_schedule_runs_once_per_minute_key(self):
        at = datetime.datetime(2026, 1, 1, 10, 15)  # reconcile runs every 15 minutes
        key = "schedule:reconcile:2026-01-01T10:15"
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
        out = self.run_local(GitHub(), "--dry-run", at=datetime.datetime(2026, 1, 1, 10, 15))[1]
        self.assertEqual(out, [{"would_run": "reconcile", "event": "schedule:reconcile:2026-01-01T10:15"}])
        self.assertFalse(Path(".factory/local/seen.json").exists())

    def test_a_developer_worktree_gets_the_role_skills_excluded_from_git(self):
        # the v2 fixture lists role skills in factory.yaml, not in `roles/<role>/skills.txt` (which install_skills reads)
        team = Path(self.tmp.name) / "team"
        shutil.copytree(FIXTURE, team)
        (team / "roles/developer/skills.txt").write_text("pr tdd\n")
        for skill in ("pr", "tdd"):
            (team / "skills" / skill).mkdir(parents=True)
            (team / "skills" / skill / "SKILL.md").write_text(f"# {skill}\n")
        cfg.set_team(team)
        tree = Path(self.tmp.name) / "tree"
        subprocess.run(["git", "init", "-q", str(tree)], check=True)
        local.install_skills(subprocess.run, "developer", "claude-code", tree)
        local.install_skills(subprocess.run, "developer", "claude-code", tree)
        for skill in (team / "roles/developer/skills.txt").read_text().split():
            self.assertTrue((tree / ".claude/skills" / skill / "SKILL.md").is_file(), skill)
        self.assertTrue((tree / ".claude/skills/factory-step/SKILL.md").is_file())
        self.assertEqual((tree / ".git/info/exclude").read_text().split().count(".claude/skills/"), 1)
        self.assertEqual(subprocess.run(["git", "-C", str(tree), "status", "--porcelain"], capture_output=True, text=True).stdout, "")


class RunnerTest(unittest.TestCase):
    """#1071: the harnesses are agent runner plugins (ports/agent.py)."""
    TOOLS_RO = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git status:*),Bash(factory:*)"
    ARGV = {  # (harness, write) -> the argv `harness_argv` built before #1071, for model anthropic/claude-x
        ("claude-code", True): ["claude", "-p", "P", "--output-format", "json", "--model", "claude-x", "--allowedTools",
                                "Bash,Read,Edit,Write,Grep,Glob", "--permission-mode", "acceptEdits"],
        ("claude-code", False): ["claude", "-p", "P", "--output-format", "json", "--model", "claude-x", "--allowedTools", TOOLS_RO],
        ("codex", True): ["codex", "exec", "--json", "-m", "claude-x", "--sandbox", "workspace-write",
                          "-c", "sandbox_workspace_write.network_access=true", "P"],
        ("junie", True): ["junie", "--task", "P", "--model", "claude-x", "--output-format", "json"],
        ("pi", True): ["pi", "-p", "--model", "anthropic/claude-x", "P"],
    }

    def test_argv_is_unchanged_for_every_harness(self):
        for (harness, write), want in self.ARGV.items():
            self.assertEqual(local.runner(harness).command("P", "anthropic/claude-x", write), want, harness)
        for harness in ("codex", "junie", "pi"):  # read-only nodes run the same argv in these harnesses
            self.assertEqual(local.runner(harness).command("P", "anthropic/claude-x", False), self.ARGV[(harness, True)])

    def test_cli_and_skill_dir_per_harness(self):
        got = {h: (local.runner(h).cli, local.runner(h).skill_dir) for h in ("claude-code", "claude", "codex", "junie", "pi")}
        self.assertEqual(got, {"claude-code": ("claude", ".claude/skills"), "claude": ("claude", ".claude/skills"),
                               "codex": ("codex", ".agents/skills"), "junie": ("junie", ".agents/skills"),
                               "pi": ("pi", ".agents/skills")})

    def test_the_model_alias_harness_picks_the_runner(self):
        config = {"models": {"mid": {"harness": "codex", "model": "gpt-x"}, "cheap": {"harness": "claude", "model": "s"},
                             "old": "anthropic/claude-x"}, "roles": {"developer": {}, "reviewer": {"harness": "pi"}}}
        self.assertEqual(local.role_harness(config, "developer", None, "mid"), "codex")
        self.assertEqual(local.runner(local.role_harness(config, "developer", None, "mid"), config).name, "codex")
        self.assertEqual(local.runner(local.role_harness(config, "developer", None, "cheap"), config).name, "claude-code")
        self.assertEqual(local.role_harness(config, "reviewer", None, "mid"), "pi")  # the role's own harness wins
        self.assertEqual(local.role_harness(config, "developer", None, "old"), "claude-code")
        self.assertEqual(local.local_model(config, "mid"), "gpt-x")
        self.assertEqual(local.local_model(config, "old"), "anthropic/claude-x")
        self.assertEqual([p for p in plugins.selected(config) if p in ("claude", "codex", "junie", "pi")], ["claude", "codex"])

    def test_an_unknown_harness_names_the_known_ones(self):
        with self.assertRaises(ValueError) as e:
            local.runner("aider")
        self.assertEqual(str(e.exception), "unknown-agent: harness: aider: no selected plugin provides this agent "
                                           "(known: claude, claude-code, codex, junie, pi)")

    def test_a_missing_binary_is_a_failed_probe(self):
        probe = local.runner("codex").probe(which=lambda name: None)
        self.assertEqual((probe.ok, probe.result, probe.detail), (False, "missing", "`codex` (codex) is not on PATH"))
        self.assertTrue(local.runner("codex").probe(which=lambda name: f"/bin/{name}").ok)


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


class PollTest(unittest.TestCase):
    def labeled(self, *labels):
        return [{"eventType": kind, "filters": {"label": label}} for kind, label in labels]

    def test_three_watched_labels_read_one_search_and_no_events(self):
        gh = GitHub(issues={"factory:a": [{"number": 7}], "factory:b": [{"number": 7}], "factory:c": [{"number": 8}]})
        plan = [{"triggers": self.labeled(("issues.labeled", "factory:a"), ("issues.labeled", "factory:b"), ("issues.labeled", "factory:c"))}]
        events = local.poll(plan, "zaharchenko-evgeny/slop-factory", gh)
        self.assertEqual(len([x for x in gh.calls if "graphql" in x[1]]), 1)
        self.assertFalse([x for x in gh.calls if any("/events" in a for a in x[1])])
        self.assertEqual([(eid, p["label"]["name"], p["issue"]["number"]) for eid, _, p in events],
                         [("labeled:107", "factory:a", 7), ("labeled:107", "factory:b", 7), ("labeled:108", "factory:c", 8)])

    def test_the_event_id_is_the_rest_id_decoded_from_the_node_id(self):
        gh = GitHub(prs={"factory:review-low": [{"number": 5, "headRefName": "ez/7-add-x"}]})
        plan = [{"triggers": self.labeled(("pull_request.labeled", "factory:review-low"))}]
        [(eid, _, payload)] = local.poll(plan, "zaharchenko-evgeny/slop-factory", gh)
        self.assertEqual(eid, f"labeled:{graphql.rest_id(node_id(105))}")
        self.assertEqual(payload["pull_request"], {"number": 5, "head": {"ref": "ez/7-add-x"}})

    def test_an_undecodable_node_id_falls_back_to_events(self):
        gh = GitHub(issues={"factory:a": [{"number": 7}]}, bad_ids=True)
        plan = [{"triggers": self.labeled(("issues.labeled", "factory:a"))}]
        self.assertEqual([eid for eid, _, _ in local.poll(plan, "zaharchenko-evgeny/slop-factory", gh)], ["labeled:107"])
        self.assertEqual(len([x for x in gh.calls if any("/events" in a for a in x[1])]), 1)


if __name__ == "__main__":
    unittest.main()


class SurfacesTest(unittest.TestCase):
    CONFIG = {"executors": {"box": {"type": "docker"}},
              "surfaces": {"air": {"type": "air"}, "laptop": {"type": "local", "executor": "box", "harness": "codex", "nodes": ["implement"]},
                           "plain": {"type": "local"}},
              "targets": {"local": {"harness": "pi"}}}

    def test_named_instance_settings_and_aliases(self):
        import surfaces
        inst = surfaces.instances(self.CONFIG)
        self.assertEqual(inst["laptop"]["type"], "local")
        self.assertEqual(surfaces.instances({"targets": {"local": {"harness": "pi"}}})["local"], {"type": "local", "harness": "pi"})
        self.assertEqual(local.role_harness(self.CONFIG, "dev", surface="laptop"), "codex")
        self.assertEqual(local.role_executor(self.CONFIG, "dev", surface="laptop"), "box")
        self.assertEqual(local.role_executor(self.CONFIG, "dev", surface="plain"), "host")

    def test_selection_order_flag_then_surface_then_air(self):
        import surfaces
        self.assertEqual(surfaces.selected({"surface": ["a", "b"]}, ["x"]), ["x"])
        self.assertEqual(surfaces.selected({"surface": ["a", "b"]}), ["a", "b"])
        self.assertEqual(surfaces.selected({"surface": "local"}), ["local"])
        self.assertEqual(surfaces.selected({"targets": {"air": {}}}), ["air"])
        self.assertEqual(surfaces.selected({}), [])

    def test_validation(self):
        import surfaces
        bad = {"executors": {}, "surfaces": {"a": {"type": "air", "harness": "pi"},
                                             "l": {"type": "local", "executor": "nope", "max_parallel": 0},
                                             "h": {"type": "local", "max_parallel": 3}}}
        got = " | ".join(surfaces.errors(bad))
        for code in ("air-surface-has-harness", "unknown-executor", "bad-max-parallel", "host-parallel"):
            self.assertIn(code, got)
        self.assertEqual(surfaces.errors(self.CONFIG), [])

    def test_a_node_on_two_surfaces_is_refused_unless_filtered(self):
        import surfaces
        nodes = {"nodes": {"implement": {"role": "dev"}, "review": {"role": "rev"}}}
        cfg = {**self.CONFIG, "surface": ["air", "laptop"]}
        self.assertEqual(surfaces.errors(cfg, nodes), ["node-on-two-surfaces: nodes.implement: air, laptop"])
        self.assertEqual(surfaces.errors(cfg, {"nodes": {"review": {"role": "rev"}}}), [])

    def test_nodes_filter_limits_the_plan(self):
        nodes = {"nodes": {n: {"role": "dev", "triggers": ["label ready"], "output": "read-only"} for n in ("implement", "review")}}
        cfg = {"surfaces": {"laptop": {"type": "local", "nodes": ["implement"]}}, "roles": {"dev": {}}}
        names = {e["node"] for e in local.plan_local(nodes, cfg, "laptop")}
        self.assertEqual(names, {"implement"})

    def test_machine_override_merges_by_name_and_spares_air(self):
        import tempfile, pathlib, surfaces
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "local.yaml"
            f.write_text("executors:\n  box: {image: x}\nsurfaces:\n  laptop: {harness: pi}\n  air: {harness: pi}\n")
            cfg = surfaces.apply_override({"executors": {"box": {"type": "docker"}}, "surfaces": dict(self.CONFIG["surfaces"])}, f)
        self.assertEqual(cfg["executors"]["box"], {"type": "docker", "image": "x"})
        self.assertEqual(cfg["surfaces"]["laptop"]["harness"], "pi")
        self.assertEqual(cfg["surfaces"]["laptop"]["executor"], "box")
        self.assertNotIn("harness", cfg["surfaces"]["air"])

    def test_factory_validate_reports_surface_errors(self):
        import deploy
        before = cfg.team_root()
        cfg.set_team(FIXTURE)
        try:
            config = {**cfg.load(), "surfaces": {"air": {"type": "air", "executor": "box"}}}
            errs = deploy.output_errors(nodes.load(), config)
        finally:
            cfg.set_team(before)
        self.assertIn("air-surface-has-executor", " ".join(errs))

    def test_workers_default_by_executor(self):
        self.assertEqual(local.workers({"surfaces": {"local": {"type": "local"}}}), 1)
        self.assertEqual(local.workers({"executors": {"b": {"type": "docker"}}, "surfaces": {"local": {"type": "local", "executor": "b"}}}), 4)
        self.assertEqual(local.workers({"surfaces": {"local": {"type": "local", "executor": "host", "max_parallel": 1}}}), 1)


class IsolatedRunTest(RunLocalTest):
    """A node run in a `docker` executor: nothing mounted, the workspace built inside, the host pushes the bundle."""
    for _n in [n for n in dir(RunLocalTest) if n.startswith("test_")]:
        locals()[_n] = None  # not the host tests again

    def setUp(self):
        super().setUp()
        real = cfg.load
        load = lambda: {**real(), "executors": {"box": {"type": "docker", "image": "img", "secrets": {"GITHUB_TOKEN": "FACTORY_TEST_TOKEN"},
                                                        "network": "none"}},
                        "targets": {**real()["targets"], "local": {**real()["targets"]["local"], "executor": "box"}}}
        patch = unittest.mock.patch.object(cfg, "load", load)
        patch.start()
        self.addCleanup(patch.stop)
        env = unittest.mock.patch.dict(os.environ, {"FACTORY_TEST_TOKEN": "tok-123"})
        env.start()
        self.addCleanup(env.stop)

    def test_the_run_happens_in_the_container_and_the_host_pushes(self):
        gh = GitHub(issues={"factory:implement-medium": [{"number": 7}]})
        code, out = self.run_local(gh)
        self.assertEqual(out, [{"ran": "implement-medium", "event": "labeled:107", "result": "pushed"}])
        kinds = [k for k in gh.kinds() if k in ("prepare", "begin", "harness", "push", "pr-create", "teardown")]
        self.assertEqual(kinds, ["prepare", "begin", "harness", "push", "pr-create", "teardown"])
        prepare = next(a for k, a in gh.calls if k == "prepare")
        self.assertNotIn("-v", prepare)  # no host mount
        self.assertEqual(prepare[prepare.index("--network") + 1], "none")
        harness = next(a for k, a in gh.calls if k == "harness")
        self.assertNotIn("tok-123", " ".join(harness))  # the token is not on any command line
        self.assertIn("GITHUB_TOKEN", harness)
        push = next(a for k, a in gh.calls if k == "push")
        self.assertIn("refs/factory/ez/7-add-x:refs/heads/ez/7-add-x", push)
        self.assertTrue(Path(".factory/runs").is_dir())

    def test_no_token_is_a_config_error_and_starts_no_container(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FACTORY_TEST_TOKEN")
            gh = GitHub(issues={"factory:implement-medium": [{"number": 7}]})
            _, out = self.run_local(gh)
        self.assertTrue(out[0]["result"].startswith("config: executor docker has no GITHUB_TOKEN"), out)
        self.assertNotIn("prepare", gh.kinds())

    def test_precheck_no_go_skips_container_entirely(self):
        gh = GitHub(issues={"factory:implement-medium": [{"number": 7}]}, begin_code=10)
        self.assertEqual(self.run_local(gh)[1][0]["result"], "no-go")
        self.assertEqual(gh.kinds().count("precheck"), 1)
        self.assertNotIn("prepare", gh.kinds())
        self.assertNotIn("harness", gh.kinds())

    def test_no_go_still_tears_the_container_down(self):
        class RaceGitHub(GitHub):
            def __call__(self, argv, **kw):
                if argv[0] == "uv" and "--check-only" in argv:
                    self.calls.append(("precheck", argv))
                    return subprocess.CompletedProcess(argv, 0, '{"go": true}', "")
                return super().__call__(argv, **kw)
        gh = RaceGitHub(issues={"factory:implement-medium": [{"number": 7}]}, begin_code=10)
        self.assertEqual(self.run_local(gh)[1][0]["result"], "no-go")
        self.assertEqual(gh.kinds().count("teardown"), 1)
        self.assertNotIn("harness", gh.kinds())

    def test_node_timeout_reads_timeout_minutes_and_timeout(self):
        self.assertEqual(local.node_timeout({"timeout_minutes": 15}), 900)
        self.assertEqual(local.node_timeout({"timeout": 42, "timeout_minutes": 15}), 42)
        self.assertIsNone(local.node_timeout({}))

    def test_parallel_same_automation_uses_unique_containers_and_events(self):
        gh = GitHub(issues={"factory:implement-medium": [{"number": 7}, {"number": 8}]})
        code, out = self.run_local(gh)
        self.assertEqual(code, 0)
        self.assertEqual(len(out), 2)
        names = [a[a.index("--name") + 1] for k, a in gh.calls if k == "prepare"]
        self.assertEqual(len(set(names)), 2)  # never collide even in the same second

    def test_parallel_pool_does_not_block_fast_runs_behind_slow_runs(self):
        import threading
        release_slow = threading.Event()
        started_slow = threading.Event()
        completed = []
        polls = [0]

        def fake_poll(plan, source, run=None):
            polls[0] += 1
            if polls[0] == 1:
                return [("labeled:107", plan[0], {"issue": {"number": 7}}),
                        ("labeled:108", plan[0], {"issue": {"number": 8}})]
            return []

        def fake_run_event(entry, payload, state, config, graph, run):
            n = payload["issue"]["number"]
            if n == 7:
                started_slow.set()
                release_slow.wait(timeout=5)
            completed.append(n)
            return "ran"

        Path(".factory/local.json").write_text(json.dumps(
            {"harness": "claude-code", "factory": str(FIXTURE), "poll_seconds": 0.05,
             "checkouts": {"slop-factory": str(Path(self.tmp.name))}}))
        real_poll_stop = [0]

        def poll_then_stop(plan, source, run=None):
            res = fake_poll(plan, source, run)
            real_poll_stop[0] += 1
            if real_poll_stop[0] == 2:
                # second poll happened while slow run #7 was still blocked; #8 already completed!
                self.assertIn(8, completed)
                self.assertNotIn(7, completed)
                release_slow.set()
            if real_poll_stop[0] >= 3:
                raise KeyboardInterrupt()
            return res

        out = io.StringIO()
        with unittest.mock.patch.object(local, "poll", poll_then_stop), \
                unittest.mock.patch.object(local, "run_event", fake_run_event), \
                unittest.mock.patch.object(local, "reconcile_executors", lambda *a: {}), \
                contextlib.redirect_stdout(out):
            try:
                local.main(["--local"], run=GitHub(), sleep=lambda s: None)
            except KeyboardInterrupt:
                pass
        rows = [json.loads(x) for x in out.getvalue().splitlines()]
        self.assertEqual([r["event"] for r in rows][:1], ["labeled:108"])


class SbxIsolatedRunTest(RunLocalTest):
    """A node run in a `docker-sandbox` (`sbx`) executor: model API allowlist, `sbx secret`, dynamic home, bundle export."""
    for _n in [n for n in dir(RunLocalTest) if n.startswith("test_")]:
        locals()[_n] = None

    def setUp(self):
        super().setUp()
        real = cfg.load
        load = lambda: {**real(), "executors": {"sbx": {"type": "docker-sandbox", "template": "reg/sbx:1",
                                                        "secrets": {"GITHUB_TOKEN": "FACTORY_TEST_TOKEN"},
                                                        "network": {"allow": ["pypi.org"]}}},
                        "targets": {**real()["targets"], "local": {**real()["targets"]["local"], "executor": "sbx"}}}
        patch = unittest.mock.patch.object(cfg, "load", load)
        patch.start()
        self.addCleanup(patch.stop)
        env = unittest.mock.patch.dict(os.environ, {"FACTORY_TEST_TOKEN": "tok-123", "ANTHROPIC_API_KEY": "sk-ant-1"})
        env.start()
        self.addCleanup(env.stop)

    def test_sbx_run_injects_secrets_allows_model_api_and_uses_agent_home(self):
        class SbxGitHub(GitHub):
            def __call__(self, argv, **kw):
                if argv[:2] == ["sbx", "exec"] and any("HOME:-/home/factory" in a for a in argv):
                    self.calls.append((None, argv))
                    return subprocess.CompletedProcess(argv, 0, "/home/agent\n/home/agent/.factory-box/work\n/home/agent/.factory-box/repos\n/home/agent/.factory-box/opt\n", "")
                return super().__call__(argv, **kw)
        gh = SbxGitHub(issues={"factory:implement-medium": [{"number": 7}]})
        code, out = self.run_local(gh)
        self.assertEqual(out, [{"ran": "implement-medium", "event": "labeled:107", "result": "pushed"}])
        kinds = [k for k in gh.kinds() if k in ("prepare", "policy", "secret", "begin", "harness", "push", "pr-create", "teardown")]
        self.assertEqual(kinds, ["prepare", "policy", "secret", "secret", "begin", "harness", "push", "pr-create", "teardown"])
        policy = next(a for k, a in gh.calls if k == "policy")
        self.assertIn("api.anthropic.com", policy[4])
        self.assertIn("pypi.org", policy[4])
        # raw secrets were injected via `sbx secret set`, so raw GITHUB_TOKEN is absent and ANTHROPIC_API_KEY is only the sentinel
        harness = next(a for k, a in gh.calls if k == "harness")
        self.assertNotIn("GITHUB_TOKEN", harness)
        self.assertNotIn("tok-123", " ".join(harness))
        self.assertNotIn("sk-ant-1", " ".join(harness))
        self.assertIn("/home/agent/bin", " ".join(harness))
        # export bundles only origin/main..branch
        bundle_calls = [a for _, a in gh.calls if a[:2] == ["sbx", "exec"] and "bundle" in a]
        self.assertTrue(bundle_calls and bundle_calls[0][-1] == "origin/main..ez/7-add-x", bundle_calls)


class FixesAndStartStopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        before, cwd = cfg.team_root(), os.getcwd()
        cfg.set_team(FIXTURE)
        os.chdir(self.tmp.name)
        local.use_surface("local")
        self.addCleanup(lambda: (local.use_surface("local"), cfg.set_team(before), os.chdir(cwd)))

    def test_adr0021_role_skills_are_installed_without_skills_txt(self):
        slop = ROOT / "factory/tests/fixtures/slop"
        team = Path(self.tmp.name) / "team"
        shutil.copytree(slop, team)
        for skill in ("simplified-technical-english", "tdd", "pr"):
            (team / "skills" / skill).mkdir(parents=True, exist_ok=True)
            (team / "skills" / skill / "SKILL.md").write_text(f"# {skill}\n")
        cfg.set_team(team)
        tree = Path(self.tmp.name) / "tree"
        subprocess.run(["git", "init", "-q", str(tree)], check=True)
        local.install_skills(subprocess.run, "developer", "codex", tree)
        for skill in ("simplified-technical-english", "tdd", "pr", "factory-step"):
            self.assertTrue((tree / ".agents/skills" / skill / "SKILL.md").is_file(), skill)

    def test_role_executor_host_override_conflicts_with_max_parallel(self):
        import surfaces
        config = {"executors": {"box": {"type": "docker"}},
                  "surfaces": {"laptop": {"type": "local", "executor": "box", "max_parallel": 2}},
                  "roles": {"reviewer": {"executor": "host"}}}
        errs = " | ".join(surfaces.errors(config))
        self.assertIn("host-parallel", errs)
        self.assertIn("roles.reviewer.executor", errs)

    def test_plan_local_passes_agent_tiers(self):
        graph = {"nodes": {"implement": {"role": "developer", "agent": "dev-strong", "triggers": ["issues.labeled factory:ready"]}}}
        config = {"roles": {"developer": {"tier": "cheap"}},
                  "agents": {"dev-strong": {"role": "developer", "persona": "dev", "tier": "strong"}}}
        plan = local.plan_local(graph, config)
        self.assertEqual(plan[0]["tier"], "strong")

    def test_reused_worktree_resets_and_cleans_dirty_state(self):
        calls = []
        path = Path(self.tmp.name) / "wt"
        path.mkdir()

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.assertEqual(local.worktree(fake_run, Path(self.tmp.name), path, "origin/main", "ez/1-x"), "")
        self.assertEqual([c[3] for c in calls], ["reset", "clean", "checkout"])

    def test_auth_files_auto_and_aliases(self):
        rn = local.runner("claude-code")
        self.assertTrue(local._wants_auth_files({"auth_files": True}, "claude-code", rn))
        self.assertTrue(local._wants_auth_files({"auth_files": "auto"}, "claude-code", rn))
        self.assertTrue(local._wants_auth_files({"auth_files": ["claude"]}, "claude-code", rn))
        self.assertTrue(local._wants_auth_files({"auth_files": ["all"]}, "claude-code", rn))
        self.assertFalse(local._wants_auth_files({"auth_files": ["codex"]}, "claude-code", rn))

    def test_local_start_and_stop_manage_pid_and_signals(self):
        Path(".factory").mkdir(exist_ok=True)
        Path(".factory/local.json").write_text(json.dumps({"factory": str(FIXTURE), "checkouts": {}}))

        class FakeProc:
            def __init__(self, pid=42424):
                self.pid, self.returncode = pid, None

            def poll(self):
                return self.returncode

        spawned = []

        def fake_popen(cmd, **kw):
            spawned.append((cmd, kw))
            return FakeProc()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(local.start(["--dry-run"], popen=fake_popen, sleep=lambda s: None), 0)
        self.assertIn("would start", out.getvalue())
        self.assertEqual(spawned, [])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(local.start([], popen=fake_popen, sleep=lambda s: None), 0)
        self.assertIn("started (pid 42424", out.getvalue())
        self.assertTrue(spawned[0][1]["start_new_session"])
        self.assertEqual(Path(".factory/local/local.pid").read_text().strip(), "42424")

        # already running -> idempotent
        out = io.StringIO()
        with unittest.mock.patch.object(local, "_pid_alive", return_value=True), contextlib.redirect_stdout(out):
            self.assertEqual(local.start([], popen=fake_popen, sleep=lambda s: None), 0)
        self.assertIn("already running (pid 42424)", out.getvalue())
        self.assertEqual(len(spawned), 1)

        # stop sends SIGTERM and cleans PID file once process exits
        signals_sent = []
        alive_states = iter([True, False, False])

        def fake_kill(pid, sig):
            signals_sent.append((pid, sig))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(local.stop([], kill=fake_kill, alive=lambda pid: next(alive_states, False), sleep=lambda s: None), 0)
        import signal as sig_mod
        self.assertEqual(signals_sent, [(42424, sig_mod.SIGTERM)])
        self.assertIn("stopped (pid 42424)", out.getvalue())
        self.assertFalse(Path(".factory/local/local.pid").exists())

        # stop when not running is clean exit 0
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(local.stop([]), 0)
        self.assertIn("not running", out.getvalue())

    def test_air_start_and_stop_toggle_paused_and_route_from_cli(self):
        from factory_plugins.air import deploy as air_deploy
        import deploy as core_deploy
        slop = ROOT / "factory/tests/fixtures/slop"
        cfg.set_team(slop)
        prefix = f"[{core_deploy.project_name(cfg.load())}] "

        class FakeAir:
            def __init__(self, items):
                self.items = {x["automationId"]: dict(x) for x in items}
                self.calls = []

            def call(self, tool, args):
                self.calls.append((tool, args))
                if tool == "list_automations":
                    return list(self.items.values())
                if tool == "get_automation":
                    return self.items[args["automationId"]]
                if tool == "update_automation":
                    self.items[args["automationId"]]["paused"] = args["paused"]
                    return self.items[args["automationId"]]
                raise AssertionError(tool)

            def close(self):
                pass

        fake = FakeAir([
            {"automationId": "a1", "name": f"{prefix}implement-medium", "paused": False},
            {"automationId": "a2", "name": f"{prefix}review-low", "paused": True},
            {"automationId": "other", "name": "[Other] unrelated", "paused": False},
        ])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(air_deploy.stop(["--dry-run"], automations=fake), 0)
        self.assertIn("would disable", out.getvalue())
        self.assertFalse(fake.items["a1"]["paused"])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(air_deploy.stop([], automations=fake), 0)
        self.assertTrue(fake.items["a1"]["paused"])
        self.assertFalse(fake.items["other"]["paused"])  # unrelated automation untouched
        self.assertIn("disabled 1 automations (1 already disabled)", out.getvalue())

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(air_deploy.start([], automations=fake), 0)
        self.assertFalse(fake.items["a1"]["paused"])
        self.assertFalse(fake.items["a2"]["paused"])
        self.assertIn("enabled 2 automations (0 already enabled)", out.getvalue())

    def test_deploy_probes_multiple_harnesses_in_one_sandbox(self):
        real = cfg.load

        def load():
            c = real()
            return {**c, "executors": {"box": {"type": "docker", "image": "img", "secrets": {"GITHUB_TOKEN": "TOK"}}},
                    "roles": {**c["roles"], "reviewer": {**c["roles"]["reviewer"], "harness": "codex"}},
                    "targets": {**c["targets"], "local": {"harness": "claude-code", "executor": "box"}}}

        team = Path(self.tmp.name) / "factory"
        shutil.copytree(FIXTURE, team)
        (team / ".git").mkdir()
        cfg.set_team(team)
        calls = []

        class DockerMachine(Machine):
            def run(self, argv, **kw):
                if argv[0] == "docker":
                    calls.append(argv)
                    return subprocess.CompletedProcess(argv, 0, "ok\n", "")
                return super().run(argv, **kw)

        with unittest.mock.patch.object(cfg, "load", load), unittest.mock.patch.dict(os.environ, {"TOK": "t1"}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = local.deploy([], run=DockerMachine(tools=("gh", "docker"), origins=ORIGINS).run,
                                    which=DockerMachine(tools=("gh", "docker"), origins=ORIGINS).which)
        self.assertEqual(code, 0, out.getvalue())
        self.assertEqual(len([c for c in calls if c[:2] == ["docker", "run"]]), 1)
        self.assertEqual(len([c for c in calls if c[:2] == ["docker", "exec"] and "--version" in c]), 2)

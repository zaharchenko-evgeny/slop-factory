"""The docker and docker-sandbox executors (ports/executor.py): the CLI calls they make, never a real daemon."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from factory_plugins.docker.executor import Executor as Docker  # noqa: E402
from factory_plugins.docker_sandbox.executor import Executor as Sandbox  # noqa: E402


class Fake:
    def __init__(self, fail=(), out=None):
        self.calls, self.inputs, self.fail, self.out = [], [], fail, out or {}

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        self.inputs.append(kw.get("input"))
        for needle in self.fail:
            if needle in argv:
                return subprocess.CompletedProcess(argv, 1, "", "boom")
        if kw.get("timeout") and "slow" in argv:
            raise subprocess.TimeoutExpired(argv, kw["timeout"], output="partial out", stderr="partial err")
        key = " ".join(argv[:3])
        if key in self.out:
            return subprocess.CompletedProcess(argv, 0, self.out[key], "")
        return subprocess.CompletedProcess(argv, 0, self.out.get(argv[1], ""), "")


class DockerTest(unittest.TestCase):
    def test_prepare_mounts_nothing_by_default_and_labels_the_run(self):
        run = Fake()
        self.assertEqual(Docker({"image": "img", "cpus": 2, "memory": "4g"}, run).prepare("7-1"), "factory-7-1")
        cmd = run.calls[0]
        self.assertNotIn("-v", cmd)
        self.assertEqual((cmd[cmd.index("--cpus") + 1], cmd[cmd.index("--memory") + 1]), ("2", "4g"))
        self.assertIn("factory.run=7-1", cmd)

    def test_mounts_and_caches_are_opt_in_and_uv_sets_cache_dir(self):
        run = Fake()
        Docker({"image": "i", "mounts": ["/a:/b:ro"], "cache": ["uv"]}, run).prepare("x")
        self.assertIn("factory-cache-uv:/cache/uv", run.calls[0])
        self.assertIn("UV_CACHE_DIR=/cache/uv", run.calls[0])
        self.assertIn("/a:/b:ro", run.calls[0])

    def test_dockerfile_is_built_once_per_content_and_context(self):
        with tempfile.TemporaryDirectory() as d:
            df = Path(d) / "Dockerfile"
            df.write_text("FROM x\n")
            helper = Path(d) / "helper.sh"
            helper.write_text("echo v1\n")
            run = Fake(fail=("inspect",))
            tag = Docker({"dockerfile": str(df)}, run).image()
            self.assertTrue(tag.startswith("factory-exec-"))
            self.assertEqual([c[1] for c in run.calls], ["image", "build"])
            run2 = Fake()
            self.assertEqual(Docker({"dockerfile": str(df)}, run2).image(), tag)
            self.assertEqual([c[1] for c in run2.calls], ["image"])
            # editing a file copied from the build context changes the hash
            helper.write_text("echo v2\n")
            run3 = Fake(fail=("inspect",))
            tag2 = Docker({"dockerfile": str(df)}, run3).image()
            self.assertNotEqual(tag, tag2)
            self.assertEqual([c[1] for c in run3.calls], ["image", "build"])

    def test_make_writable_chowns_to_container_user(self):
        run = Fake(out={"docker exec c": "1000\n1000\n"})
        Docker({}, run).make_writable("c", "/home/factory/.codex")
        self.assertEqual(run.calls[-1], ["docker", "exec", "-u", "root", "c", "chown", "-R", "1000:1000", "/home/factory/.codex"])

    def test_reconcile_batches_inspect_and_rm(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        run = Fake(out={"ps": "factory-old\nfactory-new\n", "inspect": f"/factory-old {old}\n/factory-new {fresh}\n"})
        self.assertEqual(Docker({}, run).reconcile(), 1)
        self.assertEqual([c[1] for c in run.calls], ["ps", "inspect", "rm"])
        self.assertEqual(run.calls[-1], ["docker", "rm", "-f", "factory-old"])

    def test_exec_writes_log_file_when_given(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "harness.log"
            run = Fake(out={"exec": "hello\n"})
            Docker({}, run).exec("c", ["echo", "hi"], log_file=log)
            self.assertIn("hello", log.read_text())

    def test_put_merges_directories_without_nesting(self):
        with tempfile.TemporaryDirectory() as d:
            src_dir = Path(d) / "skills"
            src_dir.mkdir()
            src_file = Path(d) / "event.json"
            src_file.write_text("{}")
            run = Fake()
            Docker({}, run).put("c", src_dir, "/work/repo/.agents/skills")
            self.assertEqual(run.calls[0][-2:], ["-p", "/work/repo/.agents/skills"])
            self.assertEqual(run.calls[1], ["docker", "cp", f"{src_dir}/.", "c:/work/repo/.agents/skills"])
            run2 = Fake()
            Docker({}, run2).put("c", src_file, "/work/event.json")
            self.assertEqual(run2.calls[1], ["docker", "cp", str(src_file), "c:/work/event.json"])

    def test_env_values_stay_off_the_command_line(self):
        run = Fake()
        Docker({}, run).exec("c", ["git", "x"], cwd="/w", env={"TOKEN": "secret"})
        self.assertNotIn("secret", " ".join(run.calls[0]))
        self.assertEqual(run.calls[0][:6], ["docker", "exec", "-w", "/w", "-e", "TOKEN"])

    def test_timeout_is_exit_124_stops_container_and_keeps_output(self):
        run = Fake()
        done = Docker({"timeout": 3}, run).exec("c", ["slow"])
        self.assertEqual(done.returncode, 124)
        self.assertEqual(done.stdout, "partial out")
        self.assertIn("partial err", done.stderr)
        self.assertIn("timed out after 3s", done.stderr)
        self.assertEqual(run.calls[-1], ["docker", "stop", "-t", "2", "c"])

    def test_teardown_removes_or_commits_on_keep(self):
        run = Fake()
        Docker({}, run).teardown("c")
        self.assertEqual(run.calls, [["docker", "rm", "-f", "c"]])
        run = Fake()
        Docker({"keep": True}, run).teardown("c")
        self.assertEqual([c[1] for c in run.calls], ["commit", "stop"])

    def test_export_uses_base_range_and_is_none_when_nothing_is_committed(self):
        run = Fake()
        self.assertEqual(Docker({}, run).export("c", "/r", "ez/7-x", "/tmp/x.bundle", base="origin/main"), "/tmp/x.bundle")
        self.assertEqual(run.calls[0][-1], "origin/main..ez/7-x")
        self.assertIsNone(Docker({}, Fake(fail=("bundle",))).export("c", "/r", "b", "/tmp/x.bundle", base="origin/main"))

    def test_probe_refuses_an_allowlist_and_a_missing_image(self):
        which = lambda n: "/usr/bin/docker"  # noqa: E731
        self.assertIn("docker-sandbox", Docker({"image": "i", "network": {"allow": ["x"]}}, Fake()).probe(which)[0].detail)
        self.assertEqual(Docker({}, Fake()).probe(which)[0].result, "missing")
        self.assertTrue(Docker({"image": "i"}, Fake()).probe(which)[0].ok)
        self.assertFalse(Docker({"image": "i"}, Fake()).probe(lambda n: None)[0].ok)


class SandboxTest(unittest.TestCase):
    def test_prepare_creates_mountless_shell_sandbox_and_allows_github_plus_extra(self):
        run = Fake()
        ex = Sandbox({"template": "reg/img:1", "cpus": 2, "memory": "4g", "network": {"allow": ["pypi.org"]}}, run)
        self.assertEqual(ex.prepare("7-1", extra_allow=("api.anthropic.com",)), "factory-7-1")
        create, policy = run.calls
        self.assertEqual(create, ["sbx", "create", "--no-input", "--no-progress", "--name", "factory-7-1", "--skills", "off",
                                  "-t", "reg/img:1", "--cpus", "2", "--memory", "4g", "shell"])
        self.assertEqual(policy[:5], ["sbx", "policy", "allow", "network", "github.com,api.github.com,pypi.org,api.anthropic.com"])

    def test_dockerfile_builds_saves_and_loads_once_into_sbx_with_pull_never(self):
        with tempfile.TemporaryDirectory() as d:
            df = Path(d) / "Dockerfile"
            df.write_text("FROM docker.io/sbx/shell-image:latest\n")
            run = Fake(out={"sbx template ls": "REPOSITORY   TAG   IMAGE ID   FLAVOR   CREATED\n"})
            ex = Sandbox({"dockerfile": str(df)}, run)
            self.assertEqual(ex.prepare("7-1"), "factory-7-1")
            create = next(c for c in run.calls if c[:2] == ["sbx", "create"])
            self.assertEqual(create[create.index("--pull") + 1], "never")
            tag = create[create.index("-t") + 1]
            self.assertTrue(tag.startswith("factory-sbx-") and tag.endswith(":latest"))
            repo, ver = tag.split(":")
            # second call sees the real two-column `docker.io/library/<repo>  latest` output and skips build/save/load
            run2 = Fake(out={"sbx template ls": f"REPOSITORY  TAG  IMAGE ID\ndocker.io/library/{repo}  {ver}  abc123\n"})
            self.assertEqual(Sandbox({"dockerfile": str(df)}, run2).template(), (tag, True))
            self.assertEqual(len(run2.calls), 1)

    def test_a_failed_policy_removes_the_sandbox(self):
        run = Fake(fail=("policy",))
        with self.assertRaises(RuntimeError):
            Sandbox({}, run).prepare("x")
        self.assertEqual(run.calls[-1][:3], ["sbx", "rm", "--force"])

    def test_inject_secrets_uses_stdin_sbx_secret_set_and_falls_back_with_warning(self):
        run = Fake()
        ex = Sandbox({}, run)
        rem, warns = ex.inject_secrets("factory-7-1",
                                       {"GITHUB_TOKEN": "ghp_1", "GH_TOKEN": "ghp_1", "ANTHROPIC_API_KEY": "sk_1", "FACTORY_TEAM": "/t"})
        self.assertEqual(rem, {"ANTHROPIC_API_KEY": "proxy-managed", "FACTORY_TEAM": "/t"})
        self.assertEqual(warns, [])
        self.assertEqual(run.calls, [
            ["sbx", "secret", "set", "github", "--sandbox", "factory-7-1", "--force"],
            ["sbx", "secret", "set", "anthropic", "--sandbox", "factory-7-1", "--force"],
        ])
        self.assertEqual(run.inputs, ["ghp_1\n", "sk_1\n"])
        self.assertNotIn("ghp_1", " ".join(" ".join(c) for c in run.calls))
        # failure falls back to exec -e with a warning
        run_fail = Fake(fail=("secret",))
        rem2, warns2 = Sandbox({}, run_fail).inject_secrets("factory-7-1", {"GITHUB_TOKEN": "ghp_1", "GH_TOKEN": "ghp_1"})
        self.assertEqual(rem2["GITHUB_TOKEN"], "ghp_1")
        self.assertEqual(len(warns2), 1)
        self.assertIn("falling back", warns2[0])
        # explicit secrets_mode=env skips sbx secret set and warns
        run_env = Fake()
        rem3, warns3 = Sandbox({"secrets_mode": "env"}, run_env).inject_secrets("factory-7-1", {"GITHUB_TOKEN": "ghp_1"})
        self.assertEqual(run_env.calls, [])
        self.assertEqual(rem3["GITHUB_TOKEN"], "ghp_1")
        self.assertIn("secrets_mode=env", warns3[0])

    def test_exec_and_timeout_stops_vm_instead_of_removing(self):
        run = Fake()
        Sandbox({}, run).exec("s", ["git", "x"], cwd="/w", env={"T": "v"})
        self.assertEqual(run.calls[0], ["sbx", "exec", "-w", "/w", "-e", "T", "s", "--", "git", "x"])
        run = Fake()
        done = Sandbox({"timeout": 1}, run).exec("s", ["slow"])
        self.assertEqual(done.returncode, 124)
        self.assertEqual(done.stdout, "partial out")
        self.assertIn("timed out after 1s", done.stderr)
        self.assertEqual(run.calls[-1], ["sbx", "stop", "s"])  # preserved for collect() before teardown()

    def test_export_uses_base_range(self):
        run = Fake()
        self.assertEqual(Sandbox({}, run).export("s", "/r", "ez/7-x", "/tmp/x.bundle", base="origin/main"), "/tmp/x.bundle")
        self.assertEqual(run.calls[0][-1], "origin/main..ez/7-x")

    def test_keep_leaves_the_sandbox(self):
        run = Fake()
        Sandbox({"keep": True}, run).teardown("s")
        self.assertEqual(run.calls, [])
        Sandbox({}, run).teardown("s")
        self.assertEqual(run.calls, [["sbx", "rm", "--force", "s"]])

    def test_probe_checks_pinned_version_and_dockerfile(self):
        which = lambda n: f"/x/{n}"  # noqa: E731
        run = Fake(out={"version": "sbx 0.54.1"})
        self.assertTrue(Sandbox({"sbx_version": "0.54.1"}, run).probe(which)[0].ok)
        self.assertIn("pinned", Sandbox({"sbx_version": "0.50.0"}, run).probe(which)[0].detail)
        self.assertIn("login", Sandbox({}, Fake(fail=("ls",))).probe(which)[0].detail)
        self.assertFalse(Sandbox({}, run).probe(lambda n: None)[0].ok)
        with tempfile.TemporaryDirectory() as d:
            df = Path(d) / "Dockerfile"
            df.write_text("FROM x\n")
            self.assertTrue(Sandbox({"dockerfile": str(df)}, run).probe(which)[0].ok)
            self.assertIn("docker", Sandbox({"dockerfile": str(df)}, run).probe(lambda n: "/x/sbx" if n == "sbx" else None)[0].detail)
            self.assertIn("does not exist", Sandbox({"dockerfile": str(Path(d) / "nope")}, run).probe(which)[0].detail)

    def test_reconcile_removes_only_expired_factory_sandboxes(self):
        now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        fresh_epoch = int((now - timedelta(minutes=5)).timestamp())
        stale_epoch = int((now - timedelta(hours=2)).timestamp())
        # plain text fallback parses epoch from name and spares fresh or untimestamped sandboxes
        run = Fake(out={"ls": f"factory-7-{stale_epoch}-1  running\nfactory-8-{fresh_epoch}-2  running\nmine  running\nfactory-probe-99  running\n"})
        self.assertEqual(Sandbox({}, run).reconcile(now=now), 1)
        self.assertEqual([c[3] for c in run.calls if c[1] == "rm"], [f"factory-7-{stale_epoch}-1"])
        # Real `sbx ls --json` array with `created_at` timestamps
        payload = json.dumps([
            {"name": "factory-old", "created_at": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")},
            {"name": "factory-new", "created_at": (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")},
            {"name": "other-old", "created_at": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")},
        ])
        run_json = Fake(out={"sbx ls --json": payload})
        self.assertEqual(Sandbox({}, run_json).reconcile(now=now), 1)
        self.assertEqual([c[3] for c in run_json.calls if c[1] == "rm"], ["factory-old"])


if __name__ == "__main__":
    unittest.main()

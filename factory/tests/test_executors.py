"""The docker and docker-sandbox executors (ports/executor.py): the CLI calls they make, never a real daemon."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from factory_plugins.docker.executor import Executor as Docker  # noqa: E402
from factory_plugins.docker_sandbox.executor import Executor as Sandbox  # noqa: E402


class Fake:
    def __init__(self, fail=(), out=None):
        self.calls, self.fail, self.out = [], fail, out or {}

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        for needle in self.fail:
            if needle in argv:
                return subprocess.CompletedProcess(argv, 1, "", "boom")
        if kw.get("timeout") and "slow" in argv:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return subprocess.CompletedProcess(argv, 0, self.out.get(argv[1], ""), "")


class DockerTest(unittest.TestCase):
    def test_prepare_mounts_nothing_by_default_and_labels_the_run(self):
        run = Fake()
        self.assertEqual(Docker({"image": "img", "cpus": 2, "memory": "4g"}, run).prepare("7-1"), "factory-7-1")
        cmd = run.calls[0]
        self.assertNotIn("-v", cmd)
        self.assertEqual((cmd[cmd.index("--cpus") + 1], cmd[cmd.index("--memory") + 1]), ("2", "4g"))
        self.assertIn("factory.run=7-1", cmd)

    def test_mounts_and_caches_are_opt_in(self):
        run = Fake()
        Docker({"image": "i", "mounts": ["/a:/b:ro"], "cache": ["uv"]}, run).prepare("x")
        self.assertIn("factory-cache-uv:/cache/uv", run.calls[0])
        self.assertIn("/a:/b:ro", run.calls[0])

    def test_dockerfile_is_built_once_per_content(self):
        with tempfile.TemporaryDirectory() as d:
            df = Path(d) / "Dockerfile"
            df.write_text("FROM x\n")
            run = Fake(fail=("inspect",))
            tag = Docker({"dockerfile": str(df)}, run).image()
            self.assertTrue(tag.startswith("factory-exec-"))
            self.assertEqual([c[1] for c in run.calls], ["image", "build"])
            run2 = Fake()
            self.assertEqual(Docker({"dockerfile": str(df)}, run2).image(), tag)
            self.assertEqual([c[1] for c in run2.calls], ["image"])

    def test_env_values_stay_off_the_command_line(self):
        run = Fake()
        Docker({}, run).exec("c", ["git", "x"], cwd="/w", env={"TOKEN": "secret"})
        self.assertNotIn("secret", " ".join(run.calls[0]))
        self.assertEqual(run.calls[0][:6], ["docker", "exec", "-w", "/w", "-e", "TOKEN"])

    def test_timeout_is_exit_124(self):
        self.assertEqual(Docker({"timeout": 3}, Fake()).exec("c", ["slow"]).returncode, 124)

    def test_teardown_removes_or_commits_on_keep(self):
        run = Fake()
        Docker({}, run).teardown("c")
        self.assertEqual(run.calls, [["docker", "rm", "-f", "c"]])
        run = Fake()
        Docker({"keep": True}, run).teardown("c")
        self.assertEqual([c[1] for c in run.calls], ["commit", "stop"])

    def test_export_is_none_when_nothing_is_committed(self):
        self.assertIsNone(Docker({}, Fake(fail=("bundle",))).export("c", "/r", "b", "/tmp/x.bundle"))

    def test_probe_refuses_an_allowlist_and_a_missing_image(self):
        which = lambda n: "/usr/bin/docker"  # noqa: E731
        self.assertIn("docker-sandbox", Docker({"image": "i", "network": {"allow": ["x"]}}, Fake()).probe(which)[0].detail)
        self.assertEqual(Docker({}, Fake()).probe(which)[0].result, "missing")
        self.assertTrue(Docker({"image": "i"}, Fake()).probe(which)[0].ok)
        self.assertFalse(Docker({"image": "i"}, Fake()).probe(lambda n: None)[0].ok)


class SandboxTest(unittest.TestCase):
    def test_prepare_creates_a_mountless_shell_sandbox_and_allows_github(self):
        run = Fake()
        self.assertEqual(Sandbox({"template": "reg/img:1", "cpus": 2, "network": {"allow": ["pypi.org"]}}, run).prepare("7-1"), "factory-7-1")
        create, policy = run.calls
        self.assertEqual(create, ["sbx", "create", "--name", "factory-7-1", "-t", "reg/img:1", "--cpus", "2", "shell"])
        self.assertEqual(policy[:5], ["sbx", "policy", "allow", "network", "github.com,api.github.com,pypi.org"])

    def test_a_failed_policy_removes_the_sandbox(self):
        run = Fake(fail=("policy",))
        with self.assertRaises(RuntimeError):
            Sandbox({}, run).prepare("x")
        self.assertEqual(run.calls[-1][:3], ["sbx", "rm", "--force"])

    def test_exec_and_timeout(self):
        run = Fake()
        Sandbox({}, run).exec("s", ["git", "x"], cwd="/w", env={"T": "v"})
        self.assertEqual(run.calls[0], ["sbx", "exec", "-w", "/w", "-e", "T", "s", "--", "git", "x"])
        run = Fake()
        self.assertEqual(Sandbox({"timeout": 1}, run).exec("s", ["slow"]).returncode, 124)
        self.assertEqual(run.calls[-1], ["sbx", "rm", "--force", "s"])

    def test_keep_leaves_the_sandbox(self):
        run = Fake()
        Sandbox({"keep": True}, run).teardown("s")
        self.assertEqual(run.calls, [])
        Sandbox({}, run).teardown("s")
        self.assertEqual(run.calls, [["sbx", "rm", "--force", "s"]])

    def test_probe_checks_the_pinned_version(self):
        which = lambda n: "/x/sbx"  # noqa: E731
        run = Fake(out={"version": "sbx 0.54.1"})
        self.assertTrue(Sandbox({"sbx_version": "0.54.1"}, run).probe(which)[0].ok)
        self.assertIn("pinned", Sandbox({"sbx_version": "0.50.0"}, run).probe(which)[0].detail)
        self.assertIn("login", Sandbox({}, Fake(fail=("ls",))).probe(which)[0].detail)
        self.assertFalse(Sandbox({}, run).probe(lambda n: None)[0].ok)

    def test_reconcile_removes_factory_sandboxes_only(self):
        run = Fake(out={"ls": "factory-7-1  running\nmine  running\nfactory-8-2  stopped\n"})
        self.assertEqual(Sandbox({}, run).reconcile(), 2)
        self.assertEqual([c[3] for c in run.calls if c[1] == "rm"], ["factory-7-1", "factory-8-2"])


if __name__ == "__main__":
    unittest.main()

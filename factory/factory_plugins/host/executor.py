"""The `host` executor (ports/executor.py): runs argv on this machine, no isolation."""
import subprocess

from ports.agent import ProbeResult


class Executor:
    name, isolated = "host", False

    def __init__(self, options=None, run=subprocess.run):
        self.options, self.run = options or {}, run

    def prepare(self, run_id):
        return None

    def exec(self, handle, argv, cwd=None, env=None, timeout=None):
        timeout = timeout or self.options.get("timeout")
        try:
            return self.run(argv, capture_output=True, text=True, cwd=cwd, env=env, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            return subprocess.CompletedProcess(argv, 124, (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else e.stdout or "",
                                               f"timed out after {timeout}s")

    def put(self, handle, src, dst):
        """The host's files are already on the host."""

    def collect(self, handle, paths, dest):
        """The host's run files are already on the host."""

    def export(self, handle, repo, branch, dest):
        return None

    def teardown(self, handle):
        """Nothing to destroy."""

    def probe(self, which=None):
        return [ProbeResult("executor:host", "ok", "runs on this machine")]

    def reconcile(self):
        return 0

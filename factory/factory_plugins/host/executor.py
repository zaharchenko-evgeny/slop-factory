"""The `host` executor (ports/executor.py): runs argv on this machine, no isolation."""
import os
import signal
import subprocess
from pathlib import Path

from ports.agent import ProbeResult


def _kill_group(proc):
    """Terminate the whole process group started with `start_new_session=True`, so timed-out child trees never linger."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


class Executor:
    name, isolated = "host", False

    def __init__(self, options=None, run=subprocess.run):
        self.options, self.run = options or {}, run

    def prepare(self, run_id, extra_allow=()):
        return None

    def exec(self, handle, argv, cwd=None, env=None, timeout=None, log_file=None):
        timeout = timeout or self.options.get("timeout")
        if self.run is subprocess.run and os.name == "posix":
            return self._exec_posix(argv, cwd=cwd, env=env, timeout=timeout, log_file=log_file)
        try:
            done = self.run(argv, capture_output=True, text=True, cwd=cwd, env=env, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else e.stdout or ""
            done = subprocess.CompletedProcess(argv, 124, out, f"timed out after {timeout}s")
        if log_file is not None:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(f"exit {done.returncode}\n## stdout\n{done.stdout or ''}\n## stderr\n{done.stderr or ''}\n")
        return done

    @staticmethod
    def _exec_posix(argv, cwd=None, env=None, timeout=None, log_file=None):
        """Run in a dedicated process group so timeouts clean up the entire child process tree, optionally streaming to `log_file`."""
        if log_file is not None:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a+", encoding="utf-8") as fh:
                pos = fh.tell()
                proc = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, text=True,
                                        cwd=cwd, env=env, start_new_session=True)
                try:
                    rc = proc.wait(timeout=timeout)
                    fh.flush()
                    fh.seek(pos)
                    output = fh.read()
                    return subprocess.CompletedProcess(argv, rc, output, "")
                except subprocess.TimeoutExpired:
                    _kill_group(proc)
                    fh.flush()
                    fh.seek(pos)
                    output = fh.read()
                    fh.write(f"\ntimed out after {timeout}s\n")
                    return subprocess.CompletedProcess(argv, 124, output, f"timed out after {timeout}s")
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                cwd=cwd, env=env, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=timeout)
            return subprocess.CompletedProcess(argv, proc.returncode, out or "", err or "")
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            out, err = proc.communicate()
            err_text = f"{err}\ntimed out after {timeout}s" if err else f"timed out after {timeout}s"
            return subprocess.CompletedProcess(argv, 124, out or "", err_text)

    def put(self, handle, src, dst):
        """The host's files are already on the host."""

    def collect(self, handle, paths, dest):
        """The host's run files are already on the host."""

    def export(self, handle, repo, branch, dest, base=None):
        return None

    def teardown(self, handle):
        """Nothing to destroy."""

    def probe(self, which=None):
        return [ProbeResult("executor:host", "ok", "runs on this machine")]

    def reconcile(self):
        return 0

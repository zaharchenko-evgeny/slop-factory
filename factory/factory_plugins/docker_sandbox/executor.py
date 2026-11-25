"""The `docker-sandbox` executor (ports/executor.py): a mountless `sbx` sandbox per run, named `factory-<run id>`.

Options: sbx_version (pinned, checked by probe), template (full image ref), cpus, timeout, keep, network
({allow: [hosts]}; default GitHub, plus what `allow` adds), env (host variable names passed to every exec).
shortcut: secrets travel as `exec -e` (readable inside the VM), not `sbx secret`: which credentials sbx can inject
for GitHub is unconfirmed (research §6); probe it, then move them.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

from ports.agent import ProbeResult

PREFIX = "factory-"
DEFAULT_ALLOW = ("github.com", "api.github.com")


class Executor:
    name, isolated = "docker-sandbox", True

    def __init__(self, options=None, run=subprocess.run):
        self.options, self.run = options or {}, run

    def sbx(self, *args, **kw):
        return self.run(["sbx", *args], capture_output=True, text=True, **kw)

    def prepare(self, run_id, extra_allow=()):
        o, name = self.options, f"{PREFIX}{run_id}"
        cmd = ["create", "--name", name, *(["-t", o["template"]] if o.get("template") else []),
               *(["--cpus", str(o["cpus"])] if o.get("cpus") else []), "shell"]
        made = self.sbx(*cmd)
        if made.returncode:
            raise RuntimeError(f"sbx create failed: {(made.stderr or made.stdout)[-300:]}")
        hosts = ",".join(dict.fromkeys([*DEFAULT_ALLOW, *((o.get("network") or {}).get("allow") or []), *extra_allow]))
        allowed = self.sbx("policy", "allow", "network", hosts, "--sandbox", name)
        if allowed.returncode:
            self.teardown(name, force=True)
            raise RuntimeError(f"sbx policy failed: {(allowed.stderr or allowed.stdout)[-300:]}")
        return name

    def exec(self, handle, argv, cwd=None, env=None, timeout=None):
        timeout = timeout or self.options.get("timeout")
        env = env or {}
        cmd = ["sbx", "exec", *(["-w", str(cwd)] if cwd else []), *[x for k in env for x in ("-e", k)], handle, "--", *argv]
        try:
            return self.run(cmd, capture_output=True, text=True, env={**os.environ, **env}, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.sbx("rm", "--force", handle)  # the client kill may leave the process in the VM; the VM goes with it
            return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")

    def put(self, handle, src, dst):
        self.exec(handle, ["mkdir", "-p", str(Path(dst).parent)])
        done = self.sbx("cp", str(src), f"{handle}:{dst}")
        if done.returncode:
            raise RuntimeError(f"sbx cp failed: {(done.stderr or done.stdout)[-300:]}")

    def collect(self, handle, paths, dest):
        Path(dest).mkdir(parents=True, exist_ok=True)
        for path in paths:
            self.sbx("cp", f"{handle}:{path}", str(dest))

    def export(self, handle, repo, branch, dest):
        bundle = f"/tmp/{branch.replace('/', '_')}.bundle"
        if self.exec(handle, ["git", "-C", repo, "bundle", "create", bundle, branch]).returncode:
            return None
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        return dest if self.sbx("cp", f"{handle}:{bundle}", str(dest)).returncode == 0 else None

    def teardown(self, handle, force=False):
        if self.options.get("keep") and not force:
            return  # left for `sbx exec -it`; the plugin never snapshots, a saved template would bake secrets in
        try:
            self.sbx("rm", "--force", handle)
        except OSError:
            pass

    def probe(self, which=None):
        which, o = which or shutil.which, self.options
        if not which("sbx"):
            return [ProbeResult("executor:docker-sandbox", "missing", "`sbx` is not on PATH (brew install docker/tap/sbx)")]
        found = self.sbx("version")
        pinned = o.get("sbx_version")
        if pinned and pinned not in (found.stdout + found.stderr):
            return [ProbeResult("executor:docker-sandbox", "missing", f"sbx version {pinned} is pinned, found {(found.stdout or found.stderr).strip()[:60]!r}")]
        if self.sbx("ls", "--json").returncode:
            return [ProbeResult("executor:docker-sandbox", "missing", "`sbx ls` failed: run `sbx login` and `sbx policy init balanced`")]
        return [ProbeResult("executor:docker-sandbox", "ok", "sbx is installed and logged in")]

    def reconcile(self):
        """Remove `factory-*` sandboxes that `sbx ls` lists. Age is not in its output, so a run still in flight on this
        host would be removed too: call it only from a loop that is not running."""
        gone = 0
        for name in re.findall(rf"{PREFIX}[\w.-]+", self.sbx("ls").stdout):
            self.sbx("rm", "--force", name)
            gone += 1
        return gone

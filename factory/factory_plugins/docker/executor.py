"""The `docker` executor (ports/executor.py): one container per run, started from `image` or from the image built from
`dockerfile` (tagged by the file's hash). Nothing is mounted unless `mounts` lists it; `cache` volumes hold no workspace.

Options: image | dockerfile, network (default|none), cpus, memory, timeout, keep, mounts, cache, user.
"""
import hashlib
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ports.agent import ProbeResult

LABEL = "factory.run"


class Executor:
    name, isolated = "docker", True

    def __init__(self, options=None, run=subprocess.run):
        self.options, self.run = options or {}, run

    def docker(self, *args, **kw):
        return self.run(["docker", *args], capture_output=True, text=True, **kw)

    def image(self):
        """The image to run: `image`, else the image built from `dockerfile` (built once per file content)."""
        o = self.options
        if o.get("image"):
            return o["image"]
        df = Path(o["dockerfile"])
        tag = "factory-exec-" + hashlib.sha256(df.read_bytes()).hexdigest()[:12]
        if self.docker("image", "inspect", tag).returncode:
            built = self.docker("build", "-t", tag, "-f", str(df), str(df.parent))
            if built.returncode:
                raise RuntimeError(f"docker build failed: {(built.stderr or built.stdout)[-300:]}")
        return tag

    def prepare(self, run_id):
        o, name = self.options, f"factory-{run_id}"
        net = ["--network", "none"] if o.get("network") == "none" else []
        limits = [*(["--cpus", str(o["cpus"])] if o.get("cpus") else []), *(["--memory", str(o["memory"])] if o.get("memory") else [])]
        vols = [x for m in o.get("mounts") or [] for x in ("-v", m)] + [x for c in o.get("cache") or [] for x in ("-v", f"factory-cache-{c}:/cache/{c}")]
        who = ["--user", o["user"]] if o.get("user") else []
        started = self.docker("run", "-d", "--name", name, "--label", f"{LABEL}={run_id}", *net, *limits, *vols, *who,
                              self.image(), "sleep", "infinity")
        if started.returncode:
            raise RuntimeError(f"docker run failed: {(started.stderr or started.stdout)[-300:]}")
        return name

    def exec(self, handle, argv, cwd=None, env=None, timeout=None):
        """`env` values travel in the client's environment (`-e NAME`), not on the command line."""
        timeout = timeout or self.options.get("timeout")
        env = env or {}
        cmd = ["docker", "exec", *(["-w", str(cwd)] if cwd else []), *[x for k in env for x in ("-e", k)], handle, *argv]
        try:
            return self.run(cmd, capture_output=True, text=True, env={**os.environ, **env}, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")

    def put(self, handle, src, dst):
        self.exec(handle, ["mkdir", "-p", str(Path(dst).parent)])
        done = self.docker("cp", str(src), f"{handle}:{dst}")
        if done.returncode:
            raise RuntimeError(f"docker cp failed: {done.stderr[-300:]}")

    def collect(self, handle, paths, dest):
        Path(dest).mkdir(parents=True, exist_ok=True)
        for path in paths:
            self.docker("cp", f"{handle}:{path}", str(dest))  # a missing path is not an error: the run may have failed early

    def export(self, handle, repo, branch, dest):
        bundle = f"/tmp/{branch.replace('/', '_')}.bundle"
        if self.exec(handle, ["git", "-C", repo, "bundle", "create", bundle, branch]).returncode:
            return None
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        return dest if self.docker("cp", f"{handle}:{bundle}", str(dest)).returncode == 0 else None

    def teardown(self, handle):
        try:
            if self.options.get("keep"):
                self.docker("commit", handle, f"factory-keep-{handle}")
                self.docker("stop", handle)
            else:
                self.docker("rm", "-f", handle)
        except OSError:
            pass

    def probe(self, which=None):
        import shutil
        which = which or shutil.which
        o = self.options
        if not which("docker"):
            return [ProbeResult("executor:docker", "missing", "`docker` is not on PATH")]
        if self.docker("info").returncode:
            return [ProbeResult("executor:docker", "missing", "the docker daemon is not reachable")]
        if not (o.get("image") or o.get("dockerfile")):
            return [ProbeResult("executor:docker", "missing", "set `image` or `dockerfile`")]
        if o.get("dockerfile") and not Path(o["dockerfile"]).exists():
            return [ProbeResult("executor:docker", "missing", f"dockerfile {o['dockerfile']} does not exist")]
        if o.get("network") not in (None, "default", "none"):
            return [ProbeResult("executor:docker", "missing", f"network {o['network']!r}: `docker` supports default or none; an allowlist needs `docker-sandbox`")]
        return [ProbeResult("executor:docker", "ok", "docker is reachable")]

    def reconcile(self):
        """Remove factory containers older than `timeout` (default 1 h) plus 10 minutes."""
        limit = timedelta(seconds=(self.options.get("timeout") or 3600) + 600)
        now, gone = datetime.now(timezone.utc), 0
        for name in self.docker("ps", "-a", "--filter", f"label={LABEL}", "--format", "{{.Names}}").stdout.split():
            made = self.docker("inspect", "-f", "{{.Created}}", name).stdout.strip()
            try:
                age = now - datetime.fromisoformat(made[:26].rstrip("Z").split(".")[0] + "+00:00")
            except ValueError:
                continue
            if age > limit:
                self.docker("rm", "-f", name)
                gone += 1
        return gone

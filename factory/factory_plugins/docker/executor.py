"""The `docker` executor (ports/executor.py): one container per run, started from `image` or from the image built from
`dockerfile` (tagged by the file's hash). Nothing is mounted unless `mounts` lists it; `cache` volumes hold no workspace.

Options: image | dockerfile, network (default|none), cpus, memory, timeout, keep, mounts, cache, user, secrets, env, auth_files.
"""
import hashlib
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ports.agent import ProbeResult

LABEL = "factory.run"
_BUILD_LOCK = threading.Lock()


def _decode(data):
    return data.decode(errors="replace") if isinstance(data, bytes) else (data or "")


CONTEXT_SKIP = {".git", "__pycache__", ".venv", "node_modules", ".factory"}


def _context_hash(df: Path, ctx_root: Path | None = None) -> str:
    """SHA-256 over the Dockerfile and every file in its build context directory, so `COPY` changes bust the cache."""
    h = hashlib.sha256()
    h.update(b"dockerfile\0")
    h.update(df.read_bytes())
    root = ctx_root or df.parent
    if root.is_dir():
        for path in sorted(p for p in root.rglob("*") if p.is_file() and p != df
                           and not (CONTEXT_SKIP & set(p.relative_to(root).parts))):
            h.update(path.relative_to(root).as_posix().encode())
            h.update(b"\0")
            h.update(path.read_bytes())
            h.update(b"\0")
    return h.hexdigest()[:12]


def _resolve_dockerfile(path):
    """Resolve `dockerfile` relative to `team_root()` when it is not absolute and not present at CWD."""
    p = Path(path)
    if p.is_absolute() or p.exists():
        return p
    try:
        import config as config_mod
        cand = config_mod.team_root() / p
        if cand.exists():
            return cand
    except Exception:
        pass
    return p


class Executor:
    name, isolated = "docker", True

    def __init__(self, options=None, run=subprocess.run):
        self.options, self.run = options or {}, run

    def docker(self, *args, **kw):
        return self.run(["docker", *args], capture_output=True, text=True, **kw)

    def image(self):
        """The image to run: `image`, else the image built from `dockerfile` (built once per build context hash)."""
        o = self.options
        if o.get("image"):
            return o["image"]
        df = _resolve_dockerfile(o["dockerfile"])
        ctx_root = _resolve_dockerfile(o["context"]) if o.get("context") else df.parent
        tag = "factory-exec-" + _context_hash(df, ctx_root)
        with _BUILD_LOCK:
            if self.docker("image", "inspect", tag).returncode:
                built = self.docker("build", "-t", tag, "-f", str(df), str(ctx_root))
                if built.returncode:
                    raise RuntimeError(f"docker build failed: {(built.stderr or built.stdout)[-300:]}")
        return tag

    def prepare(self, run_id, extra_allow=()):
        o, name = self.options, f"factory-{run_id}"
        net = ["--network", "none"] if o.get("network") == "none" else []
        limits = [*(["--cpus", str(o["cpus"])] if o.get("cpus") else []), *(["--memory", str(o["memory"])] if o.get("memory") else [])]
        caches = o.get("cache") or []
        vols = [x for m in o.get("mounts") or [] for x in ("-v", m)] + [x for c in caches for x in ("-v", f"factory-cache-{c}:/cache/{c}")]
        cache_env = ["-e", "UV_CACHE_DIR=/cache/uv"] if "uv" in caches else []
        who = ["--user", o["user"]] if o.get("user") else []
        started = self.docker("run", "-d", "--name", name, "--label", f"{LABEL}={run_id}", *net, *limits, *vols, *cache_env, *who,
                              self.image(), "sleep", "infinity")
        if started.returncode:
            raise RuntimeError(f"docker run failed: {(started.stderr or started.stdout)[-300:]}")
        return name

    def exec(self, handle, argv, cwd=None, env=None, timeout=None, log_file=None):
        """`env` values travel in the client's environment (`-e NAME`), not on the command line."""
        timeout = timeout or self.options.get("timeout")
        env = env or {}
        cmd = ["docker", "exec", *(["-w", str(cwd)] if cwd else []), *[x for k in env for x in ("-e", k)], handle, *argv]
        full_env = {**os.environ, **env}
        if log_file is not None and self.run is subprocess.run:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a+", encoding="utf-8") as fh:
                pos = fh.tell()
                proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True, env=full_env)
                try:
                    rc = proc.wait(timeout=timeout)
                    fh.flush()
                    fh.seek(pos)
                    return subprocess.CompletedProcess(cmd, rc, fh.read(), "")
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    self.docker("stop", "-t", "2", handle)
                    fh.flush()
                    fh.seek(pos)
                    out = fh.read()
                    fh.write(f"\ntimed out after {timeout}s\n")
                    return subprocess.CompletedProcess(cmd, 124, out, f"timed out after {timeout}s")
        try:
            done = self.run(cmd, capture_output=True, text=True, env=full_env, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            self.docker("stop", "-t", "2", handle)  # stop any runaway in-container process before `collect`
            out, err = _decode(e.stdout), _decode(e.stderr)
            done = subprocess.CompletedProcess(cmd, 124, out, f"{err}\ntimed out after {timeout}s" if err else f"timed out after {timeout}s")
        if log_file is not None:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(f"exit {done.returncode}\n## stdout\n{done.stdout or ''}\n## stderr\n{done.stderr or ''}\n")
        return done

    def make_writable(self, handle, dst):
        """`docker cp` keeps host numeric UID; chown copied auth files to the container's active user."""
        uid_gid = self.exec(handle, ["sh", "-c", "id -u && id -g"]).stdout.splitlines()
        if len(uid_gid) >= 2 and uid_gid[0].isdigit() and uid_gid[1].isdigit():
            self.docker("exec", "-u", "root", handle, "chown", "-R", f"{uid_gid[0]}:{uid_gid[1]}", str(dst))

    def put(self, handle, src, dst):
        src_path = Path(src)
        if src_path.is_dir():
            self.exec(handle, ["mkdir", "-p", str(dst)])
            done = self.docker("cp", f"{src_path}/.", f"{handle}:{dst}")
        else:
            self.exec(handle, ["mkdir", "-p", str(Path(dst).parent)])
            done = self.docker("cp", str(src), f"{handle}:{dst}")
        if done.returncode:
            raise RuntimeError(f"docker cp failed: {done.stderr[-300:]}")

    def collect(self, handle, paths, dest):
        Path(dest).mkdir(parents=True, exist_ok=True)
        for path in paths:
            self.docker("cp", f"{handle}:{path}", str(dest))  # a missing path is not an error: the run may have failed early

    def export(self, handle, repo, branch, dest, base=None):
        bundle = f"/tmp/{branch.replace('/', '_')}.bundle"
        rev = f"{base}..{branch}" if base else branch
        if self.exec(handle, ["git", "-C", repo, "bundle", "create", bundle, rev]).returncode:
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
        which = which or shutil.which
        o = self.options
        if not which("docker"):
            return [ProbeResult("executor:docker", "missing", "`docker` is not on PATH")]
        if self.docker("info").returncode:
            return [ProbeResult("executor:docker", "missing", "the docker daemon is not reachable")]
        if not (o.get("image") or o.get("dockerfile")):
            return [ProbeResult("executor:docker", "missing", "set `image` or `dockerfile`")]
        if o.get("dockerfile") and not _resolve_dockerfile(o["dockerfile"]).exists():
            return [ProbeResult("executor:docker", "missing", f"dockerfile {o['dockerfile']} does not exist")]
        if o.get("context") and not _resolve_dockerfile(o["context"]).is_dir():
            return [ProbeResult("executor:docker", "missing", f"context {o['context']} does not exist")]
        if o.get("network") not in (None, "default", "none"):
            return [ProbeResult("executor:docker", "missing", f"network {o['network']!r}: `docker` supports default or none; an allowlist needs `docker-sandbox`")]
        return [ProbeResult("executor:docker", "ok", "docker is reachable")]

    def reconcile(self):
        """Remove factory containers older than `timeout` (default 1 h) plus 10 minutes."""
        limit = timedelta(seconds=(self.options.get("timeout") or 3600) + 600)
        now, gone = datetime.now(timezone.utc), 0
        names = self.docker("ps", "-a", "--filter", f"label={LABEL}", "--format", "{{.Names}}").stdout.split()
        if not names:
            return 0
        inspected = self.docker("inspect", "-f", "{{.Name}} {{.Created}}", *names).stdout.splitlines()
        stale = []
        for line in inspected:
            parts = line.strip().split(None, 1)
            if len(parts) < 2:
                continue
            name, made = parts[0].lstrip("/"), parts[1]
            try:
                dt = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", made))
                age = now - (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
            except ValueError:
                continue
            if age > limit:
                stale.append(name)
        if stale:
            self.docker("rm", "-f", *stale)
            gone = len(stale)
        return gone

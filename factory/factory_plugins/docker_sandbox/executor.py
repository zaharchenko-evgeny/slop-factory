"""The `docker-sandbox` executor (ports/executor.py): a mountless `sbx` sandbox per run, named `factory-<run id>`.

Options: sbx_version (pinned, checked by probe), template (full image ref) | dockerfile (built via docker, saved and
loaded into sbx once per file content), cpus, memory, timeout, keep, network ({allow: [hosts]}; default GitHub +
harness model APIs, plus what `allow` adds), secrets ({NAME: HOST_VAR}), secrets_mode (auto|env; default `auto`
uses `sbx secret set --sandbox --force` via stdin and falls back to `exec -e`), env (host variable names passed to
every exec), auth_files.
"""
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ports.agent import ProbeResult

PREFIX = "factory-"
DEFAULT_ALLOW = ("github.com", "api.github.com")
SECRET_SERVICES = {"GITHUB_TOKEN": "github", "ANTHROPIC_API_KEY": "anthropic", "OPENAI_API_KEY": "openai"}
SENTINEL_KEYS = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}  # shell sandboxes do not export these; proxy swaps header
NAME_EPOCH_RE = re.compile(rf"^{PREFIX}[^-]+-(\d{{10,}})(?:-[\w.-]+)?$")
_BUILD_LOCK = threading.Lock()
_PUT_SEQ = itertools.count(1)


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


def _parse_created(value):
    if not value or not isinstance(value, str):
        return None
    try:
        cleaned = re.sub(r"(\.\d{6})\d+", r"\1", value.strip())
        dt = datetime.fromisoformat(cleaned)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _template_listed(output, tag):
    """True when `sbx template ls` output contains `tag` (either `repo:ver` or two columns `.../repo  ver`)."""
    if tag in output:
        return True
    repo, _, ver = tag.rpartition(":")
    if not repo:
        return False
    for line in output.splitlines():
        cols = line.split()
        if len(cols) >= 2 and (cols[0] == repo or cols[0].endswith("/" + repo)) and cols[1] == ver:
            return True
    return False


class Executor:
    name, isolated = "docker-sandbox", True

    def __init__(self, options=None, run=subprocess.run):
        self.options, self.run = options or {}, run

    def sbx(self, *args, **kw):
        return self.run(["sbx", *args], capture_output=True, text=True, **kw)

    def template(self):
        """`(ref, local)`: the template image ref to run and whether it was loaded locally from `dockerfile`."""
        o = self.options
        if o.get("template"):
            return o["template"], False
        if not o.get("dockerfile"):
            return None, False
        df = _resolve_dockerfile(o["dockerfile"])
        ctx_root = _resolve_dockerfile(o["context"]) if o.get("context") else df.parent
        tag = f"factory-sbx-{_context_hash(df, ctx_root)}:latest"
        with _BUILD_LOCK:
            listed = self.sbx("template", "ls")
            if listed.returncode == 0 and _template_listed(listed.stdout, tag):
                return tag, True
            built = self.run(["docker", "build", "-t", tag, "-f", str(df), str(ctx_root)], capture_output=True, text=True)
            if built.returncode:
                raise RuntimeError(f"docker build failed: {(built.stderr or built.stdout)[-300:]}")
            with tempfile.TemporaryDirectory() as tmp:
                tar = Path(tmp) / "template.tar"
                saved = self.run(["docker", "image", "save", tag, "-o", str(tar)], capture_output=True, text=True)
                if saved.returncode:
                    raise RuntimeError(f"docker image save failed: {(saved.stderr or saved.stdout)[-300:]}")
                loaded = self.sbx("template", "load", str(tar))
                if loaded.returncode:
                    raise RuntimeError(f"sbx template load failed: {(loaded.stderr or loaded.stdout)[-300:]}")
        return tag, True

    def prepare(self, run_id, extra_allow=()):
        o, name = self.options, f"{PREFIX}{run_id}"
        tpl, is_local = self.template()
        pull = o.get("pull") or ("never" if is_local else None)
        cmd = ["create", "--no-input", "--no-progress", "--name", name, "--skills", "off",
               *(["--pull", pull] if pull else []),
               *(["-t", tpl] if tpl else []),
               *(["--cpus", str(o["cpus"])] if o.get("cpus") else []),
               *(["--memory", str(o["memory"])] if o.get("memory") else []), "shell"]
        made = self.sbx(*cmd)
        if made.returncode:
            raise RuntimeError(f"sbx create failed: {(made.stderr or made.stdout)[-300:]}")
        hosts = ",".join(dict.fromkeys([*DEFAULT_ALLOW, *((o.get("network") or {}).get("allow") or []), *extra_allow]))
        allowed = self.sbx("policy", "allow", "network", hosts, "--sandbox", name)
        if allowed.returncode:
            self.teardown(name, force=True)
            raise RuntimeError(f"sbx policy failed: {(allowed.stderr or allowed.stdout)[-300:]}")
        return name

    def inject_secrets(self, handle, env, host_vars=None):
        """Inject known service secrets through `sbx secret set --sandbox --force` via stdin (host proxy) when
        `secrets_mode` != `env`. Returns `(remaining_env, warnings)`: `GITHUB_TOKEN`/`GH_TOKEN` are dropped (`sbx`
        provides sentinel `GH_TOKEN=gho_sbxproxymanaged...`); model API keys are replaced with a non-secret
        `proxy-managed` sentinel so harness CLIs send requests that the host proxy authenticates."""
        remaining, warnings = dict(env), []
        if self.options.get("secrets_mode") == "env":
            if any(k in remaining for k in SECRET_SERVICES):
                warnings.append("docker-sandbox: secrets_mode=env passes secrets via `sbx exec -e` (readable inside the VM)")
            return remaining, warnings
        for key, service in SECRET_SERVICES.items():
            if key not in remaining:
                continue
            val = remaining[key]
            done = self.sbx("secret", "set", service, "--sandbox", handle, "--force", input=f"{val}\n")
            if done.returncode == 0:
                if key in SENTINEL_KEYS:
                    remaining[key] = "proxy-managed"
                else:
                    remaining.pop(key)
                    if key == "GITHUB_TOKEN" and remaining.get("GH_TOKEN") == val:
                        remaining.pop("GH_TOKEN")
                continue
            warnings.append(f"docker-sandbox: `sbx secret set {service}` unavailable; falling back to `sbx exec -e` for {key}")
        return remaining, warnings

    def make_writable(self, handle, dst):
        """`sbx cp` from macOS/Linux keeps the host numeric UID; chown copied auth files to `agent` so harnesses can write."""
        self.sbx("exec", "-u", "root", handle, "--", "chown", "-R", "agent:agent", str(dst))

    def exec(self, handle, argv, cwd=None, env=None, timeout=None, log_file=None):
        timeout = timeout or self.options.get("timeout")
        env = env or {}
        cmd = ["sbx", "exec", *(["-w", str(cwd)] if cwd else []), *[x for k in env for x in ("-e", k)], handle, "--", *argv]
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
                    self.sbx("stop", handle)
                    fh.flush()
                    fh.seek(pos)
                    out = fh.read()
                    fh.write(f"\ntimed out after {timeout}s\n")
                    return subprocess.CompletedProcess(cmd, 124, out, f"timed out after {timeout}s")
        try:
            done = self.run(cmd, capture_output=True, text=True, env=full_env, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            self.sbx("stop", handle)  # stop the VM (killing in-VM processes) while keeping files for `collect` before `teardown`
            out, err = _decode(e.stdout), _decode(e.stderr)
            done = subprocess.CompletedProcess(cmd, 124, out, f"{err}\ntimed out after {timeout}s" if err else f"timed out after {timeout}s")
        if log_file is not None:
            Path(log_file).parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, "a", encoding="utf-8") as fh:
                fh.write(f"exit {done.returncode}\n## stdout\n{done.stdout or ''}\n## stderr\n{done.stderr or ''}\n")
        return done

    def put(self, handle, src, dst):
        src_path = Path(src)
        if src_path.is_dir():
            stage = f"/tmp/factory-put-{os.getpid()}-{next(_PUT_SEQ)}"
            done = self.sbx("cp", str(src_path), f"{handle}:{stage}")
            if done.returncode:
                raise RuntimeError(f"sbx cp failed: {(done.stderr or done.stdout)[-300:]}")
            merged = self.exec(handle, ["sh", "-c", 'mkdir -p "$1" && cp -Rp "$2"/. "$1"/ && rm -rf "$2"',
                                        "sh", str(dst), stage])
            if merged.returncode:
                raise RuntimeError(f"sbx put merge failed: {(merged.stderr or merged.stdout)[-300:]}")
            return
        self.exec(handle, ["mkdir", "-p", str(Path(dst).parent)])
        done = self.sbx("cp", str(src), f"{handle}:{dst}")
        if done.returncode:
            raise RuntimeError(f"sbx cp failed: {(done.stderr or done.stdout)[-300:]}")

    def collect(self, handle, paths, dest):
        Path(dest).mkdir(parents=True, exist_ok=True)
        for path in paths:
            self.sbx("cp", f"{handle}:{path}", str(dest))

    def export(self, handle, repo, branch, dest, base=None):
        bundle = f"/tmp/{branch.replace('/', '_')}.bundle"
        rev = f"{base}..{branch}" if base else branch
        if self.exec(handle, ["git", "-C", repo, "bundle", "create", bundle, rev]).returncode:
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
        if o.get("dockerfile"):
            if not _resolve_dockerfile(o["dockerfile"]).exists():
                return [ProbeResult("executor:docker-sandbox", "missing", f"dockerfile {o['dockerfile']} does not exist")]
            if o.get("context") and not _resolve_dockerfile(o["context"]).is_dir():
                return [ProbeResult("executor:docker-sandbox", "missing", f"context {o['context']} does not exist")]
            if not which("docker"):
                return [ProbeResult("executor:docker-sandbox", "missing", "`docker` is not on PATH (needed to build and load `dockerfile` into `sbx`)")]
        return [ProbeResult("executor:docker-sandbox", "ok", "sbx is installed and logged in")]

    def _list_sandboxes(self):
        """[(name, created_dt | None)] of `factory-*` sandboxes from `sbx ls --json` (falling back to plain text)."""
        listed = self.sbx("ls", "--json")
        if listed.returncode == 0 and listed.stdout.strip():
            try:
                data = json.loads(listed.stdout)
                items = data.get("sandboxes", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                out = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    name = item.get("name") or item.get("Name") or ""
                    if name.startswith(PREFIX):
                        created = _parse_created(item.get("created_at") or item.get("created") or item.get("createdAt"))
                        out.append((name, created))
                return out
            except json.JSONDecodeError:
                pass
        raw = listed.stdout if listed.returncode == 0 and listed.stdout.strip() else self.sbx("ls").stdout
        return [(name, None) for name in dict.fromkeys(re.findall(rf"{PREFIX}[\w.-]+", raw))]

    def reconcile(self, now=None):
        """Remove `factory-*` sandboxes older than `timeout` (default 1 h) plus 10 minutes, using `created_at` from
        `sbx ls --json` or the epoch in `factory-<ticket>-<epoch>-<seq>`. Sandboxes without an age are spared."""
        limit = timedelta(seconds=(self.options.get("timeout") or 3600) + 600)
        now, gone = now or datetime.now(timezone.utc), 0
        for name, created in self._list_sandboxes():
            if created is None:
                m = NAME_EPOCH_RE.match(name)
                if not m:
                    continue
                created = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
            if now - created > limit:
                self.sbx("rm", "--force", name)
                gone += 1
        return gone

"""`factory ui --team <path>`: the read-only local server of the factory progress UI (#1370, #1391).

It serves the packaged frontend assets (`ui/assets/`, built into the wheel; no Node at run time) and the snapshot
(`snapshot.build`) as JSON at `/api/snapshot`. Rules:

- Localhost only: it binds 127.0.0.1 and answers only requests whose `Host` header names a loopback address
  (a DNS-rebinding page cannot read it).
- Read-only: `GET` is the only method. Every other method gets 405. There is no write endpoint.
- Credentials stay on the server: the tracker adapters read them from the environment and the `gh` login. Served
  bodies have the value of every secret-looking environment variable (`TOKEN`, `SECRET`, `PASSWORD`, `KEY`,
  `CREDENTIAL`) replaced by `[redacted]`.
- Setup never starts it. The operator runs it (`skills/factory-setup/SKILL.md`).
"""
import argparse
import json
import mimetypes
import os
import re
import sys
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|KEY|CREDENTIAL", re.I)
MIN_SECRET = 6  # shorter values would redact ordinary words


def secret_values(environ=None):
    return sorted({v for k, v in (os.environ if environ is None else environ).items()
                   if SECRET_NAME.search(k) and len(v) >= MIN_SECRET}, key=len, reverse=True)


def redact(data, secrets):
    for value in secrets:
        data = data.replace(value.encode(), b"[redacted]")
    return data


def host_ok(header):
    """True when the `Host` header names a loopback address (any port)."""
    host = (header or "").strip().lower()
    if host.startswith("["):
        name = host[:host.find("]") + 1]
    else:
        name = host.partition(":")[0]
    return name in LOOPBACK


class Handler(BaseHTTPRequestHandler):
    server_version = "factory-ui"

    def log_message(self, *args):
        pass

    def send(self, status, body, ctype="text/plain; charset=utf-8", allow=None):
        body = redact(body, self.server.secrets)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if allow:
            self.send_header("Allow", allow)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not host_ok(self.headers.get("Host")):
            return self.send(403, b"forbidden host")
        path = self.path.split("?", 1)[0]
        if path == "/api/snapshot":
            return self.snapshot()
        if path == "/":
            path = "/index.html"
        else:
            path = path.removeprefix("/assets") if path.startswith("/assets/") else path
        target = (ASSETS / path.lstrip("/")).resolve()
        if ASSETS not in target.parents or not target.is_file():
            return self.send(404, b"not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send(200, target.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text/") or ctype.endswith("javascript") else ""))

    def snapshot(self):
        try:
            with self.server.lock:  # one build at a time: no overlapping tracker reads
                snap = self.server.build()
            body = json.dumps(snap).encode()
            return self.send(200, body, "application/json")
        except Exception as e:
            body = json.dumps({"error": f"{type(e).__name__}: {e}"[:300]}).encode()
            return self.send(500, body, "application/json")

    def refuse(self):
        self.send(405, b"read-only server: only GET is allowed", allow="GET")

    def __getattr__(self, name):
        # BaseHTTPRequestHandler looks up do_<METHOD>; any method but GET (even BREW) is refused with 405, not 501.
        if name.startswith("do_"):
            return self.refuse
        raise AttributeError(name)


def make_server(build, port=0, secrets=None):
    """A server bound to 127.0.0.1 (`port` 0: a free one). `build()` returns the snapshot dict."""
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.build, server.lock = build, threading.Lock()
    server.secrets = secret_values() if secrets is None else secrets
    return server


def live_build(now=lambda: datetime.now(timezone.utc)):
    """The snapshot builder of the installed factory (team already set), built from the engine's own loaders."""
    import config
    import nodes
    from ui import snapshot, sources

    def build():
        cfg, graph = config.load(), nodes.load()
        return snapshot.build(cfg, graph, sources.live_sources(cfg, graph), now())
    return build


def main(argv, build=None, opener=webbrowser.open, serve_forever=True):
    """`factory ui [--port N] [--no-open]`; the team comes from `--team` (cli.py). `opener(url)` opens the browser."""
    p = argparse.ArgumentParser(prog="factory ui", description="Serve the read-only factory progress UI on localhost.")
    p.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: a free one)")
    p.add_argument("--no-open", action="store_true", help="do not open the browser")
    args = p.parse_args(argv)
    server = make_server(build or live_build(), args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"factory ui: {url} (read-only; Ctrl-C stops it)", flush=True)
    if not args.no_open:
        opener(url)
    if not serve_forever:
        server.server_close()
        return 0
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

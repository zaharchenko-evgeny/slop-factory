"""Serve the packaged UI on loopback; HTTP exposes no tracker operation or command runner."""
from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import config
from .data import SnapshotStore, public
from .refresh import RefreshController

STATIC = Path(__file__).parent / 'static'


def make_server(store, port=0, static=STATIC):
    root = static.resolve()

    class FactoryHTTPServer(ThreadingHTTPServer):
        def server_close(self):
            super().server_close()
            self.refresh_controller.close()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, body=b'', mime='text/plain; charset=utf-8'):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            authority = f'127.0.0.1:{self.server.server_port}'
            # Fixed authority prevents DNS rebinding; Origin and fetch metadata prevent cross-site refreshes.
            return self.headers.get('Host') == authority and \
                self.headers.get('Origin', f'http://{authority}') == f'http://{authority}' and \
                self.headers.get('Sec-Fetch-Site', 'none') in ('same-origin', 'none')

        def do_GET(self):
            if not self.allowed():
                return self.reply(403, b'Only same-origin loopback requests are accepted')
            target = urlsplit(self.path)
            if target.path in ('/api/snapshot', '/api/status'):
                options = parse_qs(target.query, keep_blank_values=True)
                allowed = {'older'} if target.path == '/api/status' else {'older', 'refresh'}
                if set(options) - allowed:
                    return self.reply(400, b'Unknown API option')
                if any(values != ['1'] for key, values in options.items()
                       if key in ('older', 'refresh')):
                    return self.reply(400, b'API options must be 1 when supplied')
                older = options.get('older') == ['1']
                if target.path == '/api/status':
                    body = self.server.refresh_controller.status(older)
                else:
                    if options.get('refresh') == ['1']:
                        self.server.refresh_controller.request(older)
                    snapshot, snapshot_run_id, refresh = self.server.refresh_controller.snapshot(older)
                    body = {'snapshot': snapshot, 'snapshotRunId': snapshot_run_id,
                            'refresh': refresh}
                return self.reply(200, json.dumps(public(body)).encode(), 'application/json; charset=utf-8')
            if target.path.startswith('/api/'):
                return self.reply(404)
            path = root / ('index.html' if target.path == '/' else unquote(target.path).lstrip('/'))
            resolved = path.resolve()
            if not resolved.is_relative_to(root) or not resolved.is_file():
                return self.reply(404)
            return self.reply(200, resolved.read_bytes(), mimetypes.guess_type(resolved)[0] or 'application/octet-stream')

        def do_POST(self):
            self.reply(405, b'Read-only server')

        do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_POST

    server = FactoryHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.refresh_controller = RefreshController(store)
    return server


def main(argv):
    parser = argparse.ArgumentParser(prog='factory ui', description=__doc__)
    parser.add_argument('--team', type=Path, help='Factory team checkout')
    parser.add_argument('--port', type=int, default=8765, help='Loopback port (default: 8765; 0 chooses an unused port)')
    parser.add_argument('--no-open', action='store_true', help='Print URL without opening the browser')
    args = parser.parse_args(argv)
    if args.team:
        config.set_team(args.team)
    if not 0 <= args.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    if not (STATIC / 'index.html').is_file():
        parser.error('packaged UI assets are missing; install a built engine wheel')
    try:
        store = SnapshotStore()
        server = make_server(store, args.port)
    except (ValueError, KeyError, OSError) as exc:
        print(f'factory ui: could not load factory or bind loopback ({type(exc).__name__}). Check the team path or select another --port.')
        return 2
    url = f'http://127.0.0.1:{server.server_port}/'
    print(f'Factory progress UI: {url}\nRead-only. Press Ctrl-C to stop.', flush=True)
    if not args.no_open:
        # Open after serve_forever starts so the browser can connect immediately.
        opener = threading.Timer(0.1, webbrowser.open, args=(url,))
        opener.daemon = True
        opener.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

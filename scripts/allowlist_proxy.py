#!/usr/bin/env python3
"""HTTPS CONNECT proxy that allows only the given host patterns; denied hosts are appended to a log file.

Usage: allowlist_proxy.py PORT DENIED_LOG PATTERN...
"""
import fnmatch, socketserver, sys, threading
from select import select

PORT, DENIED, PATTERNS = int(sys.argv[1]), sys.argv[2], sys.argv[3:]


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        request = self.rfile.readline().decode().split()
        while self.rfile.readline().strip():
            pass  # skip the headers
        if len(request) < 2 or request[0] != "CONNECT":
            return self.wfile.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
        host, _, port = request[1].rpartition(":")
        if not any(fnmatch.fnmatch(host, p) for p in PATTERNS):
            with open(DENIED, "a") as log:
                log.write(host + "\n")
            return self.wfile.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        try:
            import socket
            upstream = socket.create_connection((host, int(port)))
        except OSError:
            return self.wfile.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        pair = [self.connection, upstream]
        while True:
            ready, _, _ = select(pair, [], [])
            for s in ready:
                data = s.recv(65536)
                if not data:
                    return upstream.close()
                (upstream if s is self.connection else self.connection).sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


Server(("127.0.0.1", PORT), Handler).serve_forever()

"""`factory ui` is read-only and local: every non-GET request gets 405, the server binds 127.0.0.1, other hosts are refused."""
import http.client
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from ui import server  # noqa: E402

SNAPSHOT = {"tickets": [], "errors": [], "token": "tok-123456-secret"}


class ReadOnly(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []
        cls.srv = server.make_server(lambda: cls.calls.append(1) or SNAPSHOT, secrets=["tok-123456-secret"])
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def request(self, method, path="/api/snapshot", host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, headers={"Host": host} if host else {})
        res = conn.getresponse()
        body = res.read()
        conn.close()
        return res, body

    def test_binds_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")

    def test_every_non_get_method_is_405_on_every_path(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT", "BREW", "PROPFIND", "LINK"):
            for path in ("/api/snapshot", "/", "/assets/app.js", "/api/anything"):
                with self.subTest(method=method, path=path):
                    res, _ = self.request(method, path)
                    self.assertEqual(res.status, 405)
                    self.assertEqual(res.getheader("Allow"), "GET")
        self.assertEqual(self.calls, [])  # a refused request never built a snapshot

    def test_get_serves_index_assets_and_snapshot(self):
        for path, ctype in (("/", "text/html"), ("/assets/app.js", "javascript"), ("/assets/app.css", "text/css"), ("/api/snapshot", "application/json")):
            with self.subTest(path=path):
                res, body = self.request("GET", path)
                self.assertEqual(res.status, 200)
                self.assertIn(ctype, res.getheader("Content-Type"))
                self.assertTrue(body)

    def test_secret_values_never_leave_the_server(self):
        res, body = self.request("GET")
        self.assertNotIn(b"tok-123456-secret", body)
        self.assertIn(b"[redacted]", body)

    def test_unknown_and_escaping_paths_are_404(self):
        for path in ("/nope", "/assets/../server.py", "/../server.py", "/%2e%2e/server.py"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0].status, 404)

    def test_non_loopback_host_header_is_refused(self):
        for host in ("evil.example", "evil.example:80", "127.0.0.1.evil.example", ""):
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", host=host or " ")[0].status, 403)
        for host in ("localhost:1", "127.0.0.1:9", "[::1]:5"):
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/", host=host)[0].status, 200)

    def test_snapshot_failure_is_a_500_with_a_message(self):
        srv = server.make_server(lambda: 1 / 0, secrets=[])
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            conn.request("GET", "/api/snapshot")
            res = conn.getresponse()
            self.assertEqual(res.status, 500)
            self.assertIn(b"ZeroDivisionError", res.read())
        finally:
            srv.shutdown()
            srv.server_close()


class Main(unittest.TestCase):
    def test_main_opens_the_browser_once_with_the_loopback_url(self):
        opened = []
        self.assertEqual(server.main([], build=lambda: SNAPSHOT, opener=opened.append, serve_forever=False), 0)
        self.assertEqual(len(opened), 1)
        self.assertRegex(opened[0], r"^http://127\.0\.0\.1:\d+/$")

    def test_no_open_skips_the_browser(self):
        opened = []
        server.main(["--no-open"], build=lambda: SNAPSHOT, opener=opened.append, serve_forever=False)
        self.assertEqual(opened, [])


if __name__ == "__main__":
    unittest.main()

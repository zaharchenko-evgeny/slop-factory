"""`ui_wheel_launch`: build the wheel, install it in a fresh venv without Node, run `factory ui --team <fixture>` with a
stubbed browser opener, fetch the assets from localhost and check that no credential is in a served response.

Needs `uv` and a package index (for setuptools, pyyaml and jinja2); it skips when `uv` is missing."""
import http.client
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "factory/tests/fixtures/team"
SECRET = "ghp_wheelLaunchSecret0123456789"


def get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", path)
    res = conn.getresponse()
    out = res.status, res.read()
    conn.close()
    return out


@unittest.skipUnless(shutil.which("uv"), "uv is not installed")
class WheelLaunch(unittest.TestCase):
    def test_installed_wheel_serves_assets_and_snapshot_without_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            dist, venv, bin_ = tmp / "dist", tmp / "venv", tmp / "bin"
            bin_.mkdir()
            subprocess.run(["uv", "build", "--wheel", "--out-dir", str(dist), str(ROOT)], check=True, capture_output=True, text=True)
            wheel = next(dist.glob("slop_factory-*.whl"))
            subprocess.run(["uv", "venv", "--python", sys.executable, str(venv)], check=True, capture_output=True, text=True)
            subprocess.run(["uv", "pip", "install", "--python", str(venv / "bin/python"), str(wheel)], check=True, capture_output=True, text=True)
            # The browser opener stub (webbrowser honors $BROWSER) and a failing `gh` that quotes the credential back.
            opened = tmp / "opened.txt"
            opener = bin_ / "opener"
            opener.write_text(f'#!/bin/sh\necho "$1" >> {opened}\n')
            gh = bin_ / "gh"
            gh.write_text(f'#!/bin/sh\necho "HTTP 401 bad credentials {SECRET}" >&2\nexit 1\n')
            for f in (opener, gh):
                f.chmod(f.stat().st_mode | stat.S_IXUSR)
            # No Node: the PATH holds the stubs and the system tools only, never a node directory.
            path = os.pathsep.join([str(bin_), "/usr/bin", "/bin"])
            self.assertIsNone(shutil.which("node", path=path))
            env = {"PATH": path, "HOME": str(tmp), "BROWSER": str(opener), "GH_TOKEN": SECRET, "DISPLAY": ":0"}
            cmd = [str(venv / "bin/factory"), "ui", "--team", str(FIXTURE)]
            proc = subprocess.Popen(cmd, cwd=tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                url = proc.stdout.readline()
                port = int(re.search(r"http://127\.0\.0\.1:(\d+)/", url).group(1))
                bodies = {}
                for path_ in ("/", "/assets/app.js", "/assets/index.css", "/api/snapshot"):
                    status, body = get(port, path_)
                    self.assertEqual(status, 200, path_)
                    bodies[path_] = body
                self.assertIn(b"<html", bodies["/"])
                self.assertIn(b"api/snapshot", bodies["/assets/app.js"])
                self.assertIn(b'"tickets"', bodies["/api/snapshot"])
                for path_, body in bodies.items():
                    self.assertNotIn(SECRET.encode(), body, path_)
                for _ in range(50):  # the opener runs in a child process
                    if opened.exists():
                        break
                    time.sleep(0.1)
                self.assertEqual(opened.read_text().strip(), f"http://127.0.0.1:{port}/")
            finally:
                proc.terminate()
                proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()

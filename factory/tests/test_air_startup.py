#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for .air/cloud/startup.sh with a temporary HOME and stubbed curl, git, uv.
Run: uv run factory/tests/test_air_startup.py"""
import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config as cfg  # noqa: E402
cfg.set_team(Path(__file__).resolve().parents[2])

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".air/cloud/startup.sh"

STUBS = {
    "curl": 'echo curl >> "$LOG"\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\ncat "$STUB_TAR" > "$out"\n',
    "gh": "",
    "git": 'echo git "$@" >> "$LOG"\n',
    "uv": 'echo uv "$@" >> "$LOG"\nprintf "#!/bin/sh\\n" > "$HOME/.local/bin/decide"; chmod +x "$HOME/.local/bin/decide"\n',
}


class StartupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.home, self.repo, self.log = t / "home", t / "repo", t / "log"
        stubs = t / "stubs"
        for d in (self.home, self.repo, stubs):
            d.mkdir()
        self.log.touch()
        for name, body in STUBS.items():
            p = stubs / name
            p.write_text("#!/bin/sh\n" + body)
            p.chmod(0o755)
        # The script pins real SHA-256s; the stub serves its own file, so the pin is rewritten to match it.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo("conftest")
            info.mode, info.size = 0o755, 10
            tf.addfile(info, io.BytesIO(b"#!/bin/sh\n\n"[:10]))
        data = gzip.compress(buf.getvalue(), mtime=0)
        (t / "stub.tgz").write_bytes(data)
        stub_tar = hashlib.sha256(data).hexdigest()
        self.script = t / "startup.sh"
        self.script.write_text(re.sub(r"^CONFTEST_SHA=.*$", f"CONFTEST_SHA={stub_tar}", SCRIPT.read_text(), flags=re.M))
        self.env = {"HOME": str(self.home), "LOG": str(self.log), "STUB_TAR": str(t / "stub.tgz"), "PATH": f"{stubs}:/usr/bin:/bin"}

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self):
        return subprocess.run(["sh", str(self.script)], cwd=self.repo, env=self.env, capture_output=True, text=True)

    def test_idempotent(self):
        (self.repo.parent / "jbeval/.git").mkdir(parents=True)
        self.assertEqual(self.run_script().returncode, 0)
        first = self.log.read_text()
        self.assertIn("curl", first)
        self.assertIn("uv tool install", first)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.log.read_text(), first)
        self.assertEqual((self.home / ".bashrc").read_text().count(".local/bin"), 1)

    def test_checksum_mismatch_fails_closed(self):
        self.script.write_text(SCRIPT.read_text())  # the real pins: the stub's file does not match
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("checksum mismatch", r.stderr)
        self.assertFalse((self.home / ".local/bin/conftest").exists())

    def test_every_download_is_pinned(self):
        text = SCRIPT.read_text()
        self.assertEqual(len(re.findall(r"^\w+_SHA=[0-9a-f]{64}$", text, re.M)), 3)
        self.assertNotRegex(text, r"curl[^\n]*\|\s*(env [^\n]*)?sh")

    def test_no_jbeval(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("uv", self.log.read_text())

    def test_light_env_skips_approver_tools(self):
        self.env["FACTORY_ENV"] = "light"
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("FACTORY_ENV=light", r.stdout)
        self.assertNotIn("curl", self.log.read_text())

    def test_air_models_in_config(self):
        m = cfg.load()["targets"]["air"]["models"]
        self.assertEqual(set(m), {"cheap", "mid", "strong", "review", "review_strong"})
        self.assertNotEqual(m["review"].split("/")[0], m["mid"].split("/")[0])


if __name__ == "__main__":
    unittest.main()

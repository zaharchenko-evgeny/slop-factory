#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for .air/cloud/startup.sh with a temporary HOME and stubbed curl, git, uv.
Run: uv run factory/tests/test_air_startup.py"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".air/cloud/startup.sh"

STUBS = {
    "curl": 'echo curl >> "$LOG"\ntmp=$(mktemp -d); printf "#!/bin/sh\\n" > "$tmp/conftest"; chmod +x "$tmp/conftest"; tar -cz -C "$tmp" conftest\n',
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
        self.env = {"HOME": str(self.home), "LOG": str(self.log), "PATH": f"{stubs}:/usr/bin:/bin"}

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self):
        return subprocess.run(["sh", str(SCRIPT)], cwd=self.repo, env=self.env, capture_output=True, text=True)

    def test_idempotent(self):
        (self.repo.parent / "jbeval/.git").mkdir(parents=True)
        self.assertEqual(self.run_script().returncode, 0)
        first = self.log.read_text()
        self.assertIn("curl", first)
        self.assertIn("uv tool install", first)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertEqual(self.log.read_text(), first)
        self.assertEqual((self.home / ".bashrc").read_text().count(".local/bin"), 1)

    def test_no_jbeval(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("uv", self.log.read_text())

    def test_air_models_in_config(self):
        m = yaml.safe_load((ROOT / "factory.yaml").read_text())["targets"]["air"]["models"]
        self.assertEqual(set(m), {"cheap", "mid", "strong", "review", "review_strong"})
        self.assertNotEqual(m["review"].split("/")[0], m["mid"].split("/")[0])


if __name__ == "__main__":
    unittest.main()

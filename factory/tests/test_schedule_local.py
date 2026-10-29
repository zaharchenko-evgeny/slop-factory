#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/schedule_local.py; launchd is never touched. Run: uv run factory/tests/test_schedule_local.py"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import schedule_local  # noqa: E402

FIXTURE = yaml.safe_load("""
targets:
  local:
    run_dir: .factory/runs
    schedule: { label: test.loop, interval: 10m, path: /a:/b }
""")


class Schedule(unittest.TestCase):
    def test_render(self):
        job = schedule_local.render(FIXTURE, Path("/f"))
        self.assertEqual(job["Label"], "test.loop")
        self.assertEqual(job["StartInterval"], 600)
        self.assertEqual(job["WorkingDirectory"], "/f")
        self.assertEqual(job["EnvironmentVariables"], {"PATH": "/a:/b"})
        self.assertEqual(job["ProgramArguments"][:4], ["/usr/bin/lockf", "-t", "0", "/f/.factory/runs/loop/loop.lock"])
        self.assertEqual(job["ProgramArguments"][4:], ["uv", "run", "factory/drive.py", "loop", "--once"])
        self.assertEqual(job["StandardOutPath"], "/f/.factory/runs/loop/loop.log")

    @unittest.skipUnless(shutil.which("lockf"), "needs lockf (macOS)")
    def test_second_cycle_exits_at_once(self):
        with tempfile.TemporaryDirectory() as d:
            lock = str(Path(d) / "l")
            holder = subprocess.Popen(["lockf", "-t", "0", lock, "sleep", "5"])
            try:
                import time; time.sleep(0.5)
                r = subprocess.run(["lockf", "-t", "0", lock, "touch", str(Path(d) / "ran")])
                self.assertNotEqual(r.returncode, 0)
                self.assertFalse((Path(d) / "ran").exists())
            finally:
                holder.kill()


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/schedule_local.py; launchd is never touched. Run: uv run factory/tests/test_schedule_local.py"""
import copy
import os
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
        self.assertEqual(job["ProgramArguments"][4:], ["uv", "run", "factory/gate.py", "sweep", "--tracker", "github"])
        self.assertEqual(job["StandardOutPath"], "/f/.factory/runs/loop/loop.log")

    def test_path_expands_home(self):
        cfg = copy.deepcopy(FIXTURE)
        cfg["targets"]["local"]["schedule"]["path"] = "$HOME/.local/bin:/b"
        job = schedule_local.render(cfg, Path("/f"))
        self.assertEqual(job["EnvironmentVariables"]["PATH"], os.path.expanduser("~") + "/.local/bin:/b")

    def keychain_job(self, root: Path) -> dict:
        cfg = copy.deepcopy(FIXTURE)
        cfg["targets"]["local"]["schedule"]["env_from_keychain"] = {"ANTHROPIC_API_KEY": "test-anthropic"}
        return schedule_local.render(cfg, root)

    def test_render_keychain_wrapper(self):
        args = self.keychain_job(Path("/f"))["ProgramArguments"]
        self.assertEqual(args[:2], ["/bin/sh", "-c"])
        self.assertIn("security find-generic-password -s test-anthropic", args[2])
        self.assertIn("export ANTHROPIC_API_KEY", args[2])
        self.assertIn("exec /usr/bin/lockf -t 0 /f/.factory/runs/loop/loop.lock uv run factory/gate.py sweep --tracker github", args[2])

    def test_central_access_needs_no_keychain(self):
        cfg = copy.deepcopy(FIXTURE)
        cfg["targets"]["local"].update(model_access="central")
        cfg["targets"]["local"]["schedule"]["env_from_keychain"] = {"ANTHROPIC_API_KEY": "test-anthropic"}
        self.assertEqual(schedule_local.render(cfg, Path("/f"))["ProgramArguments"][0], "/usr/bin/lockf")

    def run_stub(self, security: str) -> tuple[subprocess.CompletedProcess, Path]:
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        (d / "security").write_text("#!/bin/sh\n" + security)
        (d / "uv").write_text('#!/bin/sh\necho "uv ran key=$ANTHROPIC_API_KEY" > "$PWD/ran"\n')
        for f in ("security", "uv"):
            (d / f).chmod(0o755)
        (d / ".factory/runs/loop").mkdir(parents=True)
        args = self.keychain_job(d)["ProgramArguments"]
        r = subprocess.run(args, cwd=d, capture_output=True, text=True,
                           env={"PATH": f"{d}:/usr/bin:/bin"})
        return r, d / "ran"

    def test_failed_lookup_logs_names_and_runs_nothing(self):
        r, ran = self.run_stub('echo "s3cret-partial" >&2; exit 44\n')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual((r.stdout + r.stderr).strip(),
                         "keychain lookup failed: ANTHROPIC_API_KEY (service test-anthropic)")
        self.assertFalse(ran.exists())

    @unittest.skipUnless(Path("/usr/bin/lockf").exists(), "needs /usr/bin/lockf (macOS)")
    def test_lookup_exports_secret_to_cycle(self):
        r, ran = self.run_stub('echo s3cret\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(ran.read_text().strip(), "uv ran key=s3cret")
        self.assertNotIn("s3cret", r.stdout + r.stderr)

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

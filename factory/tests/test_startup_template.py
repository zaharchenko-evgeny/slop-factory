#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The template's Air startup script installs exactly the skills of an environment's roles. Run: uv run factory/tests/test_startup_template.py"""
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "template/.air/cloud/startup.sh"
TEAMS = {"template": ROOT / "template", "slop": ROOT / "factory/tests/fixtures/slop"}


class StartupTemplateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wrapper = Path(tmp.name) / "factory"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT}/factory/cli.py" "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
        self.env = {**os.environ, "PATH": f"{tmp.name}:{os.environ['PATH']}", "FACTORY_DRY": "1",
                    "FACTORY_ENGINE_DIR": str(ROOT), "HOME": tmp.name}

    def factory(self, team, *args):
        return subprocess.run(["factory", "--team", str(team), *args], capture_output=True, text=True, env=self.env,
                              check=True).stdout

    def test_skills_are_those_of_the_environment_roles_plus_factory_step(self):
        for name, team in TEAMS.items():
            for env in ("read", "write", "ops"):
                with self.subTest(team=name, env=env):
                    roles = self.factory(team, "env", "roles", env).split()
                    self.assertTrue(roles)
                    expected = {"factory-step"}
                    for role in roles:
                        expected |= set((ROOT / "template/roles" / role / "skills.txt").read_text().split())
                    done = subprocess.run(["sh", str(SCRIPT)], cwd=team, capture_output=True, text=True,
                                          env={**self.env, "FACTORY_ENV": env})
                    self.assertEqual(done.returncode, 0, done.stderr)
                    line = re.search(r"would install skills: (.*)", done.stdout).group(1)
                    self.assertEqual(set(line.split()), expected)
                    self.assertIn(f"roles={' '.join(roles)} ", done.stdout)


if __name__ == "__main__":
    unittest.main()

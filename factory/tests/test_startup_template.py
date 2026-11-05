#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The template's Air startup script installs exactly the skills of an environment's roles. Run: uv run factory/tests/test_startup_template.py"""
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "template/.air/cloud/startup.sh"
TEAMS = {"template": ROOT / "template", "slop": ROOT / "factory/tests/fixtures/slop"}


def team_fixture(src, dest):
    """A team repository: the fixture's files plus its own copy of the template's roles and skills."""
    team = Path(dest) / src.name
    shutil.copytree(src, team, ignore=shutil.ignore_patterns("roles", "skills"))
    for d in ("roles", "skills"):
        shutil.copytree(ROOT / "template" / d, team / d)
    return team


class StartupTemplateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tempfile.mkdtemp(dir=tmp.name)
        wrapper = Path(tmp.name) / "factory"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT}/factory/cli.py" "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
        self.env = {**os.environ, "PATH": f"{tmp.name}:{os.environ['PATH']}", "FACTORY_DRY": "1",
                    "FACTORY_ENGINE_DIR": str(ROOT), "HOME": tmp.name}

    def factory(self, team, *args):
        return subprocess.run(["factory", "--team", str(team), *args], capture_output=True, text=True, env=self.env,
                              check=True).stdout

    def test_skills_are_those_of_the_environment_roles_plus_factory_step(self):
        for name, src in TEAMS.items():
            team = team_fixture(src, self.tmp)
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

    def test_missing_team_role_file_is_an_error(self):
        team = team_fixture(TEAMS["template"], self.tmp)
        role = self.factory(team, "env", "roles", "read").split()[0]
        (team / "roles" / role / "skills.txt").unlink()
        done = subprocess.run(["sh", str(SCRIPT)], cwd=team, capture_output=True, text=True,
                              env={**self.env, "FACTORY_ENV": "read"})
        self.assertEqual(done.returncode, 1)
        self.assertIn(f"startup: missing team file: roles/{role}/skills.txt", done.stderr)

    def test_descriptor_values_are_unquoted(self):
        text = SCRIPT.read_text()
        func = text[text.index("descriptor_value() {"):]
        func = func[:func.index("\n}\n") + 3]
        for raw, want in (("'v1'", "v1"), ('"v1"', "v1"), ("v1  # c", "v1"), ("'https://h/r.git'", "https://h/r.git")):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as team:
                (Path(team) / "factory.project.yaml").write_text(f"engine: {raw}\n")
                done = subprocess.run(["sh", "-c", f'TEAM="{team}"\n{func}\ndescriptor_value engine'],
                                      capture_output=True, text=True)
                self.assertEqual(done.stdout.strip(), want)


if __name__ == "__main__":
    unittest.main()

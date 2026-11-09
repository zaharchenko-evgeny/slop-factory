#!/usr/bin/env python3
"""README contract checks. Run: uv run factory/tests/test_readme.py"""
import re
import subprocess
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import check_refs  # noqa: E402

README = (ROOT / "README.md").read_text()


class ReadmeTest(unittest.TestCase):
    def test_readme_names_every_subcommand(self):
        result = subprocess.run(["uv", "run", "--quiet", "factory/cli.py", "--help"], cwd=ROOT,
                                capture_output=True, text=True)
        commands = [part.split(" (")[0] for part in
                    re.search(r"command: (.*?)\.", result.stdout).group(1).split(", ")]
        for command in commands:
            with self.subTest(command=command):
                self.assertIn(f"`{command}`", README)

    def test_environment_doc_has_a_row_for_every_variable(self):
        sources = [p for p in [*(ROOT / "factory").glob("*.py"), *(ROOT / "engine/template-v1").rglob("*")] if p.is_file()]
        names = {n for p in sources for n in re.findall(r"FACTORY_[A-Z_]+", p.read_text())}
        doc = (ROOT / "docs/environment.md").read_text()
        for name in sorted(names):
            with self.subTest(name=name):
                self.assertIn(f"| `{name}` |", doc)
        self.assertIn("(docs/environment.md)", README)

    def test_check_refs_uses_git_remote_without_a_team_descriptor(self):
        remote = mock.Mock(returncode=0, stdout="https://github.com/example/engine.git\n")
        with mock.patch.object(check_refs.config, "load", side_effect=FileNotFoundError), \
                mock.patch.object(check_refs.subprocess, "run", return_value=remote) as run:
            self.assertEqual(check_refs.repository_name(), "example/engine")
        run.assert_called_once_with(["git", "remote", "get-url", "origin"], cwd=check_refs.ROOT,
                                    capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()

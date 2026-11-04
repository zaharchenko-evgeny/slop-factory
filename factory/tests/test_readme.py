#!/usr/bin/env python3
"""README contract checks. Run: uv run factory/tests/test_readme.py"""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
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

    def test_readme_names_every_descriptor_key(self):
        schema = json.loads((ROOT / "engine/descriptor.schema.json").read_text())
        for key in schema["properties"]:
            with self.subTest(key=key):
                self.assertIn(f"`{key}`", README)

    def test_readme_documents_overrides(self):
        self.assertIn("## Team-owned runtime files", README)
        normalized = " ".join(README.split())
        self.assertIn("Runtime `roles/`, `skills/`, workflows, policies, and judgments come only from the team's repository", normalized)


if __name__ == "__main__":
    unittest.main()

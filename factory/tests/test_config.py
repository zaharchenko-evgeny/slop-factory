#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/config.py and the factory command. Run: uv run factory/tests/test_config.py"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import cli  # noqa: E402
import config  # noqa: E402
import gate  # noqa: E402
import nodes  # noqa: E402
from gate_port import MemoryTracker  # noqa: E402

DESCRIPTOR = yaml.safe_load((ROOT / config.DESCRIPTOR).read_text())


def team(tmp, descriptor=None, files=None):
    root = Path(tmp)
    (root / config.DESCRIPTOR).write_text(yaml.safe_dump(descriptor or DESCRIPTOR))
    for rel, text in (files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


class TeamRootTest(unittest.TestCase):
    def tearDown(self):
        config.set_team(None)

    def test_check_returns_the_team_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = team(tmp, files={"roles/reviewer/role.md": "# mine\n"})
            config.set_team(root)
            tracker = MemoryTracker({"7": {"stage": "in-review", "head": "abc", "ci": "green", "blockers_open": 0}})
            _, out = gate.check("review", {"id": "e1", "ticket": "7"}, tracker, nodes.load(), config.load())
            self.assertEqual(Path(out["role_file"]), root / "roles/reviewer/role.md")
            self.assertEqual(config.resolve("roles/router/role.md"), ROOT / "roles/router/role.md")

    def test_missing_key_comes_from_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = {k: v for k, v in DESCRIPTOR.items() if k != "branch_pattern"}
            d["tracker"] = {"adapter": "github-issues", "repository": "o/r", "hold_label": "mine"}
            config.set_team(team(tmp, d))
            c = config.load()
            self.assertEqual(c["branch_pattern"], "ez/{ticket}-{slug}")
            self.assertEqual(c["tracker"]["stages"]["ready"], "factory:ready")
            self.assertEqual((c["tracker"]["hold_label"], c["tracker"]["repository"]), ("mine", "o/r"))

    def test_unknown_descriptor_key_fails_validate(self):
        nested = ({"limits": {"colour": 1}}, {"gates": {"colour": 1}}, {"knowledge": {"colour": 1}},
                  {"targets": {"air": {"colour": 1}}}, {"roles": {"developer": {"colour": 1}}},
                  {"roles": {"reviewer": {"modes": {"full": {"colour": 1}}}}},
                  {"repositories": [{"name": "x", "colour": 1}]}, {"checks": [{"name": "x", "colour": 1}]})
        for bad in ({"colour": 1}, {"tracker": {**DESCRIPTOR["tracker"], "colour": 1}}, *nested):
            with tempfile.TemporaryDirectory() as tmp:
                config.set_team(team(tmp, {**DESCRIPTOR, **bad}))
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = cli.main(["validate"])
                self.assertEqual(code, 1)
                self.assertIn("unknown-key", out.getvalue())

    def test_validate_passes_for_this_repository(self):
        config.set_team(ROOT)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["validate"]), 0)
        self.assertIn("factory: ok", out.getvalue())

    def test_team_option_and_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = team(tmp)
            self.assertEqual(config.pop_team(["check", "--team", str(root), "--node", "x"]), ["check", "--node", "x"])
            self.assertEqual(config.team_root(), root.resolve())


if __name__ == "__main__":
    unittest.main()

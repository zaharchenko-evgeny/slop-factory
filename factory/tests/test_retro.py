#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for the nightly retro's facts. Run: uv run factory/tests/test_retro.py"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config as cfg  # noqa: E402
cfg.set_team(ROOT / "factory/tests/fixtures/team")
import cli  # noqa: E402
import retro  # noqa: E402

cli.load_engine(cli.HERE)
BOT, ME = {"login": "jetbrains-air[bot]", "type": "Bot"}, {"login": "me", "type": "User"}


def label(name, at, by=BOT, event="labeled"):
    return {"event": event, "label": {"name": f"factory:{name}"}, "created_at": at, "actor": by}


class RoutineBeginTest(unittest.TestCase):
    def test_routine_begin_needs_no_payload(self):
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            os.chdir(d)
            self.addCleanup(os.chdir, old)
            cli.STATE = Path(".factory/begin.json")
            Path("empty.json").write_text("")
            for name in ("retro", "distill"):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cli.begin([name, "empty.json"], ROOT / "factory/tests/fixtures/team"), 0)
                self.assertTrue(json.loads(out.getvalue())["go"])
                self.assertIsNone(json.loads(out.getvalue())["ticket"])


class CollectTest(unittest.TestCase):
    def test_collect_removes_label_then_marks(self):
        """The label goes before the comments are read; each lesson comment is taken once."""
        calls, comments = [], {10: [{"id": 1, "html_url": "u1", "body": "[factory] Lessons\n<!-- factory:lesson -->\n- a"},
                                    {"id": 2, "body": "not a lesson"},
                                    {"id": 3, "body": "[factory] Lessons\n<!-- factory:lesson collected -->\n- old"}]}

        class Tracker:
            def _list(self, path):
                calls.append(("list", path))
                if path.startswith("issues?"):
                    return [{"number": 10}]
                return comments[int(path.split("/")[1])]

            def _edit(self, n, add=(), remove=()):
                calls.append(("unlabel", n, list(remove)))

            def _api(self, path, *flags):
                calls.append(("patch", path, flags[-1]))
                comments[10][0]["body"] = flags[-1].removeprefix("body=")

        got = retro.collect(Tracker(), "factory:lesson")
        self.assertEqual(got, [{"ticket": 10, "url": "u1", "body": "[factory] Lessons\n\n- a"}])
        order = [c[0] for c in calls]
        self.assertLess(order.index("unlabel"), order.index("patch"))
        self.assertIn("<!-- factory:lesson collected -->", comments[10][0]["body"])
        self.assertEqual(retro.collect(Tracker(), "factory:lesson"), [])   # nothing is read twice


if __name__ == "__main__":
    unittest.main()

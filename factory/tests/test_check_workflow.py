#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""`factory check-workflow` (factory/cli.py): a good workflow passes, each bad fixture breaks its rule.
Run: uv run --with pytest pytest factory/tests/test_check_workflow.py (TLC and Node run; the first run downloads the pinned jar)."""
import contextlib
import io
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import cli  # noqa: E402

FIXTURES = ROOT / "factory/tests/fixtures/workflow-check"
BAD = {"one-stage-label": "OneStageLabel", "moving-node": "MovingNode",
       "no-deadlock": "NoDeadlock", "done-unreachable": "DoneReachable"}


def run(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.main(["check-workflow", *map(str, args)])
    return code, out.getvalue()


def test_known_good_workflow_passes():
    assert run(ROOT / "factory/tests/fixtures/slop") == (0, "broken rules: none\n")


@pytest.mark.parametrize("name,rule", BAD.items())
def test_bad_fixture_breaks_its_rule(name, rule):
    code, out = run(FIXTURES / name)
    assert code == 1
    assert rule in out.removeprefix("broken rules: ").strip().split(", ")


def test_every_rule_has_a_fixture():
    sys.path.insert(0, str(ROOT / "skills/factory-setup"))
    import statechart
    assert set(BAD.values()) == set(statechart.RULES)
    assert {p.name for p in FIXTURES.iterdir()} == set(BAD)


def test_missing_workflow_exits_2():
    with tempfile.TemporaryDirectory() as tmp:
        code, out = run(tmp)
    assert code == 2 and "not found" in out

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""`factory check-workflow` (factory/cli.py): a good workflow passes, each bad fixture breaks its rule.
Run: uv run --with pytest pytest factory/tests/test_check_workflow.py (TLC and Node run; the first run downloads the pinned jar)."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

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


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv builds and runs the wheel")
def test_installed_wheel_runs_the_check(tmp_path):
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(ROOT)], check=True, capture_output=True)
    wheel = next(tmp_path.glob("*.whl"))
    import zipfile
    names = {n.split(".data/data/", 1)[-1] for n in zipfile.ZipFile(wheel).namelist()}
    assert {"share/factory-engine/engine/defaults.yaml", "share/factory/statechart.py"} <= names  # #973
    env = {k: v for k, v in os.environ.items() if k not in ("FACTORY_ENGINE", "FACTORY_TEAM")}
    team = tmp_path / "team"
    shutil.copytree(ROOT / "factory/tests/fixtures/slop", team)
    workflow_path = team / "workflow.yaml"
    workflow = yaml.safe_load(workflow_path.read_text())
    workflow["nodes"]["notify"].pop("environment", None)
    workflow["nodes"]["notify-pr"].pop("environment", None)
    workflow_path.write_text(yaml.safe_dump(workflow, sort_keys=False))
    done = subprocess.run(["uv", "run", "--no-project", "--with", str(wheel), "factory", "--team", str(team),
                           "validate"], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert done.returncode == 0, done.stderr  # the installed engine files load (#984)
    for name, code in (("../slop", 0), ("no-deadlock", 1)):
        run = subprocess.run(["uv", "run", "--no-project", "--with", str(wheel), "factory", "check-workflow",
                              str(FIXTURES / name)], capture_output=True, text=True)
        assert run.returncode == code, run.stderr

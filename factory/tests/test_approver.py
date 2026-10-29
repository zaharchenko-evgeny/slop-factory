#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/approver.py with a stub `decide`; no network or model. The policy tests need
`conftest` and skip when it is missing. Run: uv run factory/tests/test_approver.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import approver  # noqa: E402
import sm  # noqa: E402

FIX = HERE / "fixtures/approver"
CONFIG = yaml.safe_load((sm.ROOT / "factory.yaml").read_text())
# Prints $STUB_DIR/decide.json as the records and keeps its arguments and DECISIONS_MODEL.
DECIDE = """#!/bin/bash
printf '%s\\n' "$@" "model=$DECISIONS_MODEL" > "$STUB_DIR/decide-args"
cat "$STUB_DIR/decide.json"
"""


def records(*answers):
    return json.dumps([{"questions": [{"id": i, "resolution": r, "answer": a} for i, r, a in answers]}])


def summary(name):
    return json.loads((FIX / name).read_text())


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        (self.tmp / "decide").write_text(DECIDE)
        (self.tmp / "decide").chmod(0o755)
        for k, v in {"STUB_DIR": str(self.tmp), "FACTORY_DECIDE": str(self.tmp / "decide")}.items():
            self.addCleanup(os.environ.pop, k, None)
            os.environ[k] = v


@unittest.skipUnless(shutil.which("conftest"), "conftest is not installed; skipping the policy tests")
class HardRules(unittest.TestCase):
    def test_docs_only_passes(self):
        self.assertEqual(approver.hard(sm.ROOT, summary("docs-only.json")), [])

    def test_workflow_change_is_rejected(self):
        self.assertEqual(approver.hard(sm.ROOT, summary("workflow.json")), ["touches .github/workflows/ci.yml"])

    def test_dependency_change_is_rejected(self):
        self.assertEqual(approver.hard(sm.ROOT, summary("dependency.json")), ["changes dependencies in pyproject.toml"])

    def test_review_ci_risk_and_size(self):
        s = {**summary("docs-only.json"), "ci": "unknown", "risk": "high", "lines_changed": 999,
             "reviews": [{"reviewer": "review_full", "verdict": "changes-requested"}]}
        self.assertEqual(sorted(approver.hard(sm.ROOT, s)),
                         ["999 changed lines, more than 200", "CI is unknown, not passed",
                          "reviewer review_full did not approve", "risk is high"])
        self.assertIn("no reviewer report", approver.hard(sm.ROOT, {**s, "reviews": []}))


class SoftRules(Env):
    def test_all_decided_yes_approves_and_passes_engine_and_model(self):
        (self.tmp / "decide.json").write_text(records(("risk-low", "decided", "yes"), ("scope-limited", "decided", "yes")))
        self.assertEqual(approver.soft(sm.ROOT, "text", "agent:claude", "anthropic/m-1"), [])
        args = (self.tmp / "decide-args").read_text().split("\n")
        self.assertIn("agent:claude", args)
        self.assertIn("model=m-1", args)

    def test_host_or_no_needs_human(self):
        (self.tmp / "decide.json").write_text(records(("risk-low", "host", "uncertain"), ("scope-limited", "decided", "no"),
                                                      ("intent-clear", "decided", "yes")))
        self.assertEqual(approver.soft(sm.ROOT, "t", "e", "m"), ["risk-low: host uncertain", "scope-limited: decided no"])

    def test_missing_tool_or_bad_output_needs_human(self):
        (self.tmp / "decide.json").write_text("not json")
        self.assertEqual(approver.soft(sm.ROOT, "t", "e", "m"), ["decide output unreadable"])
        (self.tmp / "decide.json").write_text("[]")
        self.assertEqual(approver.soft(sm.ROOT, "t", "e", "m"), ["decide returned no answers"])
        os.environ["FACTORY_DECIDE"] = "no-such-decide-31"
        self.assertEqual(approver.soft(sm.ROOT, "t", "e", "m"), ["soft rules not checked: no-such-decide-31 is not installed"])


class Decide(Env):
    def setUp(self):
        super().setUp()
        self.wt = self.tmp / "wt"
        self.wt.mkdir()
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        for k, v in env.items():
            self.addCleanup(os.environ.pop, k, None)
            os.environ[k] = v
        git(self.wt, "init", "-q", "-b", "main")
        (self.wt / "a.py").write_text("# /// script\n# dependencies = []\n# ///\n")
        git(self.wt, "add", ".")
        git(self.wt, "commit", "-qm", "init")
        git(self.wt, "update-ref", "refs/remotes/origin/main", "HEAD")

    def commit(self, files):
        for name, text in files.items():
            (self.wt / name).parent.mkdir(parents=True, exist_ok=True)
            (self.wt / name).write_text(text)
        git(self.wt, "add", ".")
        git(self.wt, "commit", "-qm", "change")

    def test_summary_finds_paths_lines_and_dependencies(self):
        self.commit({"docs/x.md": "one\ntwo\n", "a.py": '# /// script\n# dependencies = ["requests"]\n# ///\n',
                     "sub/uv.lock": "x\n"})
        s = approver.summarize(self.wt, "main", {"review_full": {"verdict": "approved"}}, "passed", "low")
        self.assertEqual(sorted(s["paths"]), ["a.py", "docs/x.md", "sub/uv.lock"])
        self.assertEqual(s["lines_changed"], 5)
        self.assertEqual(sorted(s["dependency_changes"]), ["a.py", "sub/uv.lock"])
        self.assertEqual(s["reviews"], [{"reviewer": "review_full", "verdict": "approved"}])

    @unittest.skipUnless(shutil.which("conftest"), "conftest is not installed; skipping the verdict test")
    def test_verdicts(self):
        (self.tmp / "decide.json").write_text(records(("risk-low", "decided", "yes")))
        self.commit({"docs/x.md": "doc\n"})
        reviews = {"review_full": {"verdict": "approved"}}
        a = approver.decide(sm.ROOT, CONFIG, self.wt, "main", reviews, "passed", "low", "ticket")
        self.assertEqual((a["verdict"], a["reasons"]), ("would-auto-approve", []))
        self.commit({".github/workflows/ci.yml": "on: push\n"})
        (self.tmp / "decide-args").unlink()
        a = approver.decide(sm.ROOT, CONFIG, self.wt, "main", reviews, "passed", "low", "ticket")
        self.assertEqual((a["verdict"], a["reasons"]), ("needs-human", ["touches .github/workflows/ci.yml"]))
        self.assertFalse((self.tmp / "decide-args").exists())  # soft rules only after the hard ones pass

    def live(self, **am):
        a = {**CONFIG["roles"]["approver"], "dry_run": False, "auto_merge": {"enabled": True, **am}}
        return {**CONFIG, "roles": {**CONFIG["roles"], "approver": a}}

    def test_dry_run_off_needs_auto_merge_enabled(self):
        config = {**CONFIG, "roles": {**CONFIG["roles"], "approver": {**CONFIG["roles"]["approver"], "dry_run": False}}}
        with self.assertRaises(ValueError):
            approver.decide(sm.ROOT, config, self.wt, "main", {}, "passed", "low", "ticket")

    @unittest.skipUnless(shutil.which("conftest"), "conftest is not installed; skipping the verdict test")
    def test_live_hard_rules_approve_without_the_judge_and_judgment_waits(self):
        self.commit({"docs/x.md": "doc\n"})
        reviews = {"review_full": {"verdict": "approved"}}
        a = approver.decide(sm.ROOT, self.live(require="hard-rules"), self.wt, "main", reviews, "passed", "low", "t",
                            ["x"], "low")
        self.assertEqual((a["verdict"], a["reasons"], a["summary"]["labels"], a["summary"]["complexity"]),
                         ("auto-approve", [], ["x"], "low"))
        self.assertFalse((self.tmp / "decide-args").exists())  # the judge did not run
        for require in ("judgment", {"low": "judgment"}):
            a = approver.decide(sm.ROOT, self.live(require=require), self.wt, "main", reviews, "passed", "low", "t")
            self.assertEqual(a["verdict"], "needs-human")

    def test_may_merge_checks_enabled_risk_and_hold(self):
        on = self.live(max_risk="low")
        self.assertTrue(approver.may_merge(on, "low", []))
        self.assertFalse(approver.may_merge(on, "low", ["factory:hold"]))
        self.assertFalse(approver.may_merge(on, "medium", []))
        self.assertFalse(approver.may_merge(CONFIG, "low", []))  # default: disabled
        self.assertEqual(approver.require({"auto_merge": {"require": {"low": "hard-rules"}}}, "low"), "hard-rules")


if __name__ == "__main__":
    unittest.main()

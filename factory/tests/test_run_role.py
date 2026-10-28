#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Unit tests for factory/run_role.py with a stub agent and gh run by /bin/bash. Run: uv run factory/tests/test_run_role.py"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import preflight  # noqa: E402
import run_role  # noqa: E402
import sm  # noqa: E402

# Before the call count reaches $STUB_OK_AT it prints nothing, or with STUB_PROSE=1 a handoff
# followed by prose, with STUB_UNFENCED=1 an unfenced handoff followed by prose; records argv and cwd per call.
# STUB_PEEK=1: a reviewer prints secret-<mode>, waits, then greps its dirs for secrets.
# preflight.py calls get $STUB_REPLY.
AGENT = """#!/bin/bash
n=$(($(cat "$STUB_DIR/calls" 2>/dev/null || echo 0) + 1))
echo "$n" > "$STUB_DIR/calls"
pwd > "$STUB_DIR/cwd"
printf '%s\\n' "$@" > "$STUB_DIR/argv"
echo "stub stderr $n" >&2
if [ -n "$STUB_PEEK" ]; then
  mode=$(printf '%s\\n' "${@: -1}" | sed -n '1s/^mode: //p')
  pwd > "$STUB_DIR/cwd-$mode"; git rev-parse HEAD > "$STUB_DIR/head-$mode"
  echo "secret-$mode"; sleep 1
  # Its own private dir, plus the shared run dir by the path that worked from the shared worktree.
  grep -rho 'secret-[a-z_]*' .. ../../runs/9 2>/dev/null | sort -u > "$STUB_DIR/seen-$mode"
  printf '```text\\nREVIEWER\\nverdict: approved\\n```\\n'; exit 0
fi
if [ "$n" -lt "$STUB_OK_AT" ] && [ -n "$STUB_UNFENCED" ]; then printf 'REVIEWER\\nverdict: approved\\nThanks!\\n'; exit 0; fi
if [ -n "${STUB_REPLY+x}" ]; then printf '%s' "$STUB_REPLY"; exit "${STUB_EXIT:-0}"; fi
if [ "$n" -lt "$STUB_OK_AT" ] && [ -n "$STUB_PROSE" ]; then printf '```text\nREVIEWER\nverdict: approved\n```\nDone!\n'; fi
if [ "$n" -ge "$STUB_OK_AT" ]; then printf '```text\\nREVIEWER\\nverdict: approved\\n```\\n'; fi
"""
GH = """#!/bin/bash
if [ "$1" = issue ]; then cat "$STUB_DIR/issue.json"; exit; fi
echo "{\\"url\\":\\"$3\\",\\"body\\":\\"Closes #9\\",\\"statusCheckRollup\\":[{\\"conclusion\\":\\"SUCCESS\\"}]}"
"""


class RunRole(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        shutil.copy(sm.ROOT / "factory.yaml", self.root)
        shutil.copytree(sm.ROOT / "workflows", self.root / "workflows")
        self.run_dir = self.root / ".factory/runs/9"
        self.wt = self.root / ".factory/worktrees/9"
        self.run_dir.mkdir(parents=True)
        self.wt.mkdir(parents=True)
        shutil.copytree(sm.ROOT / "roles", self.root / "roles")
        for cmd in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                                     "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *cmd], cwd=self.wt, check=True, capture_output=True)
        stub = self.root / "stub"
        stub.mkdir()
        for name, text in (("agent", AGENT), ("gh", GH)):
            (stub / name).write_text(text)
            (stub / name).chmod(0o755)
        self.stub = stub
        self.issue(["factory:in-dev"])
        os.environ.update(FACTORY_AGENT=str(stub / "agent"), FACTORY_GH=str(stub / "gh"), STUB_DIR=str(stub))
        for k in ("STUB_PROSE", "STUB_REPLY", "STUB_EXIT", "STUB_UNFENCED", "STUB_PEEK"):
            os.environ.pop(k, None)

    def go(self, action, ok_at, risk="low", pr_urls=(), write_input=True):
        os.environ["STUB_OK_AT"] = str(ok_at)
        (self.run_dir / "sm.json").write_text(json.dumps({"vars": {"risk": risk, "pr_urls": list(pr_urls)}}))
        if write_input:
            (self.run_dir / f"{action}.in.md").write_text("mode: test\n")
        return run_role.run(self.root, 9, action)

    def issue(self, labels, comments=()):
        (self.stub / "issue.json").write_text(json.dumps(
            {"title": "T", "body": "B", "labels": [{"name": x} for x in labels],
             "comments": [{"body": c} for c in comments]}))

    def calls(self):
        return int((self.stub / "calls").read_text())

    def model(self, key):
        return sm.yaml.safe_load((self.root / "factory.yaml").read_text())["models"][key]

    def argv_model(self):
        argv = (self.stub / "argv").read_text().splitlines()
        return argv[argv.index("--model") + 1]

    def test_empty_output_is_logged_and_runs_once(self):
        self.assertEqual(self.go("implement", ok_at=2), 3)  # no fallback: the engine's retry reruns it
        self.assertEqual(self.calls(), 1)
        log = (self.run_dir / "implement.log").read_text()
        self.assertIn(f"factory: empty output from implement on {self.model('mid')}", log)
        self.assertIn("stub stderr 1", log)
        self.assertEqual(self.go("implement", ok_at=2), 0)
        self.assertIn("stub stderr 1", (self.run_dir / "implement.log").read_text())  # the log keeps both calls
        self.assertNotIn("fallback_from", json.loads((self.run_dir / "implement.handoff.json").read_text()))

    def test_empty_output_runs_the_fallback_once(self):
        self.assertEqual(self.go("review_behaviour", ok_at=2), 0)
        self.assertEqual((self.calls(), self.argv_model()), (2, self.model("cheap")))
        self.assertIn(f"falls back from {self.model('review')} to {self.model('cheap')}",
                      (self.run_dir / "review_behaviour.log").read_text())
        h = json.loads((self.run_dir / "review_behaviour.handoff.json").read_text())
        self.assertEqual(h.pop("reviewed_sha"), subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.wt,
                                                               capture_output=True, text=True).stdout.strip())
        self.assertEqual(h, {"handoff": "REVIEWER", "verdict": "approved", "fallback_from": self.model("review"),
                          "model": self.model("cheap")})
        self.assertEqual(self.go("review_structure_security", ok_at=99), 3)
        self.assertEqual(self.calls(), 4)  # 1 on the model, 1 on the fallback

    def test_each_reviewer_focus_gets_its_model(self):
        for action, key in [("review_full", "review"), ("review_behaviour", "review"),
                            ("review_structure_security", "review_strong")]:
            with self.subTest(action):
                self.go(action, ok_at=1)
                self.assertEqual(self.argv_model(), self.model(key))
                argv = (self.stub / "argv").read_text()
                self.assertIn("1-role.md", argv)  # a private copy of roles/reviewer/role.md
                self.assertNotIn(str(self.root), argv)

    def test_handoff_followed_by_prose_is_rejected(self):
        os.environ["STUB_PROSE"] = "1"
        self.assertEqual(self.go("implement", ok_at=2), 3)
        self.assertEqual(self.calls(), 1)  # no fallback for the developer
        self.assertIn("factory: text after the last fenced block from implement",
                      (self.run_dir / "implement.log").read_text())
        self.assertEqual(self.go("implement", ok_at=2), 0)

    def test_malformed_handoff_runs_the_fallback_once(self):
        for env in ("STUB_PROSE", "STUB_UNFENCED"):
            with self.subTest(env):
                (self.stub / "calls").unlink(missing_ok=True)
                os.environ.pop("STUB_PROSE", None)
                os.environ[env] = "1"
                self.assertEqual(self.go("review_full", ok_at=2), 0)
                self.assertEqual((self.calls(), self.argv_model()), (2, self.model("cheap")))
                h = json.loads((self.run_dir / "review_full.handoff.json").read_text())
                self.assertEqual((h["fallback_from"], h["model"]), (self.model("review"), self.model("cheap")))
                self.assertIn(f"falls back from {self.model('review')} to {self.model('cheap')}",
                              (self.run_dir / "review_full.log").read_text())

    def test_parallel_reviewers_see_only_their_own_run_dir(self):
        os.environ["STUB_PEEK"] = "1"
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.wt, capture_output=True, text=True).stdout.strip()
        actions = ("review_behaviour", "review_structure_security")
        for a in actions:
            (self.run_dir / f"{a}.in.md").write_text(f"mode: {a}\n")
        codes = {}
        threads = [threading.Thread(target=lambda a=a: codes.__setitem__(a, self.go(a, 1, write_input=False)))
                   for a in actions]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(codes, dict.fromkeys(actions, 0))
        for a, other in (actions, actions[::-1]):
            seen = (self.stub / f"seen-{a}").read_text()
            self.assertIn(f"secret-{a}", seen)  # it can read its own output file
            self.assertNotIn(f"secret-{other}", seen)  # but not the other reviewer's, written meanwhile
            self.assertEqual((self.stub / f"head-{a}").read_text().strip(), sha)  # detached checkout at HEAD
            cwd = Path((self.stub / f"cwd-{a}").read_text().strip())
            self.assertFalse(cwd.is_relative_to(self.root) or cwd.exists())  # outside the root, removed after
            self.assertEqual(json.loads((self.run_dir / f"{a}.handoff.json").read_text())["reviewed_sha"], sha)
            self.assertIn(f"secret-{a}", (self.run_dir / f"{a}.out.md").read_text())  # copied back
        worktrees = subprocess.run(["git", "worktree", "list"], cwd=self.wt, capture_output=True, text=True).stdout
        self.assertEqual(len(worktrees.splitlines()), 1)

    def test_role_input_rereads_the_ticket_after_a_stage_change(self):
        self.go("implement", ok_at=1)
        self.assertIn('"name": "factory:in-dev"', (self.stub / "argv").read_text())
        self.issue(["factory:in-review"], ["[factory] Moved to #30: old scope", "keep me"])
        self.go("review_full", ok_at=1)
        argv = (self.stub / "argv").read_text()
        self.assertIn('"name": "factory:in-review"', argv)
        self.assertNotIn("factory:in-dev", argv)
        self.assertIn("keep me", argv)
        self.assertNotIn("old scope", argv)  # moved to a split slice

    def test_parse_handoff(self):
        h = run_role.parse_handoff("prose\n```text\nDEVELOPER\nstatus: done\ncommits:\n  - abc #9 x\n"
                                   "checks:\n  - a: pass\n    b\nblocked_reason: none\n```\n\n")
        self.assertEqual(h, {"handoff": "DEVELOPER", "status": "done", "commits": "- abc #9 x",
                             "checks": "- a: pass\n  b", "blocked_reason": "none"})
        self.assertEqual(run_role.parse_handoff("Done.\n\nREVIEWER\nverdict: approved\nnits:\n  - a\n"),
                         {"handoff": "REVIEWER", "verdict": "approved", "nits": "- a"})
        h = run_role.parse_handoff("REVIEWER\nverdict: changes-requested\nrisk: high\n  OK\nrisk: low")
        self.assertEqual((h["handoff"], h["verdict"]), ("REVIEWER", "changes-requested"))
        for text, msg in [("", "empty"), ("REVIEWER\nverdict: changes-requested\nOK\nrisk: low", "malformed")]:
            with self.subTest(text), self.assertRaisesRegex(run_role.HandoffError, msg):
                run_role.parse_handoff(text)
        for text, msg in [("", "empty"), ("no block", "no handoff"), ("REVIEWER\nverdict: ok\nThanks!", "malformed"),
                          ("```text\nREVIEWER\nx: y\n```\nDEVELOPER", "text after"), ("```\nlower: x\n```", "handoff name"),
                          ("```text\nREVIEWER\n```\nok", "text after"), ("```text\nX\n!!\n```", "malformed")]:
            with self.subTest(text), self.assertRaisesRegex(run_role.HandoffError, msg):
                run_role.parse_handoff(text)

    def test_preflight_reply(self):
        for reply, code, ok in [("OK.", 0, True), ("ok\n", 0, True), ("", 0, False), ("  \n", 0, False),
                                ("Error: 401 invalid api key", 0, False), ("OK", 1, False)]:
            with self.subTest(reply=reply, code=code):
                os.environ.update(STUB_REPLY=reply, STUB_EXIT=str(code))
                self.assertEqual(preflight.check("m")[0], ok)
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(preflight.main(["m1", "m2"]), 1)
            os.environ.update(STUB_REPLY="OK", STUB_EXIT="0")
            self.assertEqual(preflight.main(["m1", "m2"]), 0)
        self.assertIn("FAIL m2: 'OK'", printed.getvalue())
        argv = (self.stub / "argv").read_text().splitlines()
        self.assertEqual(argv[argv.index("--model") + 1], "m2")
        config = sm.yaml.safe_load((self.root / "factory.yaml").read_text())
        for key in ("review", "review_strong", "cheap"):
            self.assertIn(config["models"][key], preflight.run_models(config))

    def test_worktree_cwd_absolute_prompts_and_model(self):
        self.go("implement", ok_at=1, risk="high")
        self.assertEqual(Path((self.stub / "cwd").read_text().strip()).resolve(), self.wt)
        argv = (self.stub / "argv").read_text().splitlines()
        self.assertIn(str(self.root / "roles/_common.md"), argv)
        self.assertIn(str(self.root / "roles/developer/role.md"), argv)
        self.assertEqual(argv[argv.index("--model") + 1], sm.yaml.safe_load(
            (self.root / "factory.yaml").read_text())["models"]["strong"])
        self.assertIn("factory-9-developer", argv)

    def test_intake_runs_in_root_and_summary_gets_pr_data(self):
        self.go("intake", ok_at=1)
        self.assertEqual(Path((self.stub / "cwd").read_text().strip()).resolve(), self.root)
        self.go("summary", ok_at=1, pr_urls=["https://x/pull/1"])
        self.assertEqual(Path((self.stub / "cwd").read_text().strip()).resolve(), self.wt)
        argv = (self.stub / "argv").read_text()
        self.assertIn('"url":"https://x/pull/1"', argv)
        self.assertIn('"conclusion":"SUCCESS"', argv)


if __name__ == "__main__":
    unittest.main()

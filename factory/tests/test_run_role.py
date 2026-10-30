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
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import model_access  # noqa: E402
import preflight  # noqa: E402
import run_role  # noqa: E402
import sm_v1 as sm  # noqa: E402

# Before the call count reaches $STUB_OK_AT it prints nothing, or with STUB_PROSE=1 a handoff
# followed by prose, with STUB_UNFENCED=1 an unfenced handoff followed by prose; records argv and cwd per call.
# STUB_PEEK=1: a reviewer prints secret-<mode>, waits, then greps its dirs for secrets.
# preflight.py calls get $STUB_REPLY.
AGENT = """#!/bin/bash
n=$(($(cat "$STUB_DIR/calls" 2>/dev/null || echo 0) + 1))
echo "$n" > "$STUB_DIR/calls"
pwd > "$STUB_DIR/cwd"
printf '%s\\n' "$@" > "$STUB_DIR/argv"
echo "${PI_CODING_AGENT_DIR:-none} ${ANTHROPIC_API_KEY:-none}" >> "$STUB_DIR/env"
echo "stub stderr $n" >&2
if [ -n "${STUB_FILE+x}" ] && [ -n "$FACTORY_HANDOFF" ]; then printf '%s' "$STUB_FILE" > "$FACTORY_HANDOFF"; fi
if [ -n "$STUB_PEEK" ]; then
  mode=$(printf '%s\\n' "${@: -1}" | sed -n '1s/^mode: //p')
  pwd > "$STUB_DIR/cwd-$mode"; git rev-parse HEAD > "$STUB_DIR/head-$mode"
  echo "secret-$mode"; sleep 1
  # Its own private dir, plus the shared run dir by the path that worked from the shared worktree.
  grep -rho 'secret-[a-z_]*' .. ../../runs/9 2>/dev/null | sort -u > "$STUB_DIR/seen-$mode"
  printf '%s\\n' '{"handoff":"REVIEWER","verdict":"approved","risk":"low","docs_impact":"no","findings":[]}'; exit 0
fi
if [ "$n" -lt "$STUB_OK_AT" ] && [ -n "$STUB_UNFENCED" ]; then printf 'REVIEWER\\nverdict: approved\\nThanks!\\n'; exit 0; fi
if [ -n "${STUB_REPLY+x}" ]; then printf '%s' "$STUB_REPLY"; exit "${STUB_EXIT:-0}"; fi
if [ "$n" -lt "$STUB_OK_AT" ] && [ -n "$STUB_PROSE" ]; then printf '```text\nREVIEWER\nverdict: approved\n```\nDone!\n'; fi
mode=$(printf '%s\\n' "${@: -1}" | sed -n '1s/^mode: //p')
case "$mode" in full|behaviour|structure-security) ok='{"handoff":"REVIEWER","verdict":"approved","risk":"low","docs_impact":"no","findings":[]}' ;; *) ok=$(printf '```text\\nREVIEWER\\nverdict: approved\\n```\\n') ;; esac
if [ "$n" -ge "$STUB_OK_AT" ]; then printf '%s\\n' "$ok"; fi
"""
# `jbcentral status` reports the proxy running once $STUB_DIR/proxy-up exists; `proxy start` creates
# it unless $STUB_DIR/proxy-broken exists. Every call is appended to $STUB_DIR/jbcentral.calls.
JBCENTRAL = """#!/bin/bash
echo "$*" >> "$STUB_DIR/jbcentral.calls"
if [ "$1" = status ]; then
  if [ -f "$STUB_DIR/proxy-up" ]; then printf '\\033[1mProxy     \\033[m running on port 1\\n'; else echo "Proxy     stopped"; fi
elif [ "$1 $2" = "proxy start" ] && [ ! -f "$STUB_DIR/proxy-broken" ]; then touch "$STUB_DIR/proxy-up"; fi
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
        for name, text in (("agent", AGENT), ("gh", GH), ("jbcentral", JBCENTRAL)):
            (stub / name).write_text(text)
            (stub / name).chmod(0o755)
        self.stub = stub
        config = sm.yaml.safe_load((self.root / "factory.yaml").read_text())
        config["targets"]["local"]["model_access"] = "env"  # Central tests opt in
        (self.root / "factory.yaml").write_text(sm.yaml.safe_dump(config))
        self.issue(["factory:in-dev"])
        os.environ.update(FACTORY_AGENT=str(stub / "agent"), FACTORY_GH=str(stub / "gh"), STUB_DIR=str(stub),
                          FACTORY_JBCENTRAL=str(stub / "jbcentral"))
        for k in ("STUB_PROSE", "STUB_REPLY", "STUB_EXIT", "STUB_UNFENCED", "STUB_PEEK", "STUB_FILE"):
            os.environ.pop(k, None)

    def go(self, action, ok_at, risk="low", pr_urls=(), write_input=True):
        os.environ["STUB_OK_AT"] = str(ok_at)
        (self.run_dir / "sm.json").write_text(json.dumps({"vars": {"risk": risk, "pr_urls": list(pr_urls)}}))
        if write_input:
            (self.run_dir / f"{action}.in.md").write_text(f"mode: {action.removeprefix('review_').replace('_', '-')}\n")
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
        self.assertEqual(self.go("intake", ok_at=4), 3)  # one retry, no fallback: the engine's retry reruns it
        self.assertEqual(self.calls(), 2)
        log = (self.run_dir / "intake.log").read_text()
        self.assertIn(f"factory: empty output from intake on {self.model('cheap')}", log)
        self.assertIn("stub stderr 1", log)
        self.assertEqual(self.go("intake", ok_at=3), 0)
        self.assertIn("stub stderr 1", (self.run_dir / "intake.log").read_text())  # the log keeps both calls
        self.assertNotIn("fallback_from", json.loads((self.run_dir / "intake.handoff.json").read_text()))

    def test_developer_falls_back_in_its_session(self):
        self.assertEqual(self.go("implement", ok_at=3), 0)  # model, its retry, then the fallback
        h = json.loads((self.run_dir / "implement.handoff.json").read_text())
        self.assertEqual((self.calls(), h["fallback_from"], h["model"]), (3, self.model("mid"), self.model("mid")))

    def test_empty_output_runs_the_fallback_once(self):
        self.assertEqual(self.go("review_behaviour", ok_at=3), 0)
        self.assertEqual((self.calls(), self.argv_model()), (3, self.model("cheap")))
        ev = [json.loads(x) for x in (self.run_dir / "events.log").read_text().splitlines()]
        fb = next(e for e in ev if e["event"] == "fallback")
        self.assertEqual((fb["from"], fb["to"]), (self.model("review"), self.model("cheap")))
        self.assertEqual([e["outcome"] for e in ev if e["event"] == "step.end"][:3], ["empty", "empty", "ok"])
        self.assertTrue(all(e["ts"] for e in ev))
        self.assertIn(f"falls back from {self.model('review')} to {self.model('cheap')}",
                      (self.run_dir / "review_behaviour.log").read_text())
        h = json.loads((self.run_dir / "review_behaviour.handoff.json").read_text())
        self.assertEqual(h.pop("reviewed_sha"), subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.wt,
                                                               capture_output=True, text=True).stdout.strip())
        self.assertEqual(h, {"handoff": "REVIEWER", "verdict": "approved", "risk": "low", "docs_impact": "no",
                          "findings": [], "fallback_from": self.model("review"),
                          "model": self.model("cheap")})
        self.assertEqual(self.go("review_structure_security", ok_at=99), 3)
        self.assertEqual(self.calls(), 7)  # 2 on the model, 2 on the fallback

    def test_each_reviewer_focus_gets_its_model(self):
        for action, key in [("review_full", "review_strong"), ("review_behaviour", "review"),
                            ("review_structure_security", "review_strong")]:
            with self.subTest(action):
                self.go(action, ok_at=1)
                self.assertEqual(self.argv_model(), self.model(key))
                argv = (self.stub / "argv").read_text()
                self.assertIn("1-role.md", argv)  # a private copy of roles/reviewer/role.md
                self.assertNotIn(str(self.root), argv)

    def test_handoff_followed_by_prose_is_rejected(self):
        os.environ["STUB_PROSE"] = "1"
        self.assertEqual(self.go("intake", ok_at=4), 3)
        self.assertEqual(self.calls(), 2)  # one retry, no fallback for the router
        self.assertIn("factory: text after the last fenced block from intake",
                      (self.run_dir / "intake.log").read_text())
        self.assertEqual(self.go("intake", ok_at=3), 0)

    DEV = {"handoff": "DEVELOPER", "status": "done", "branch": "b", "commits": ["abc #9 x"], "files": "f",
           "checks": [], "acceptance": [{"x": "met"}], "blocked_reason": "none"}

    def test_json_handoff_file_is_valid_and_prose_is_ignored(self):
        os.environ["STUB_FILE"] = "Here it is:\n" + json.dumps(self.DEV) + "\nbye"
        os.environ["STUB_REPLY"] = "All done, see the file."
        self.assertEqual(self.go("implement", ok_at=1), 0)
        self.assertEqual(self.calls(), 1)
        h = json.loads((self.run_dir / "implement.handoff.json").read_text())
        self.assertEqual((h["handoff"], h["commits"], h["acceptance"]), ("DEVELOPER", "- abc #9 x", "- x: met"))
        self.assertIn("handoff-draft.json", (self.stub / "argv").read_text())

    def test_invalid_json_field_is_one_retry_with_the_error_then_fallback(self):
        os.environ["STUB_FILE"] = json.dumps({**self.DEV, "status": "great"})
        os.environ["STUB_REPLY"] = "done"
        self.assertEqual(self.go("implement", ok_at=1), 3)
        self.assertEqual(self.calls(), 4)  # model: 1 + 1 retry, fallback: 1 + 1 retry
        argv = (self.stub / "argv").read_text()
        self.assertIn("field 'status' must be one of done, blocked", argv)
        self.assertIn("rejected", argv)
        self.assertIn("invalid JSON handoff", (self.run_dir / "implement.log").read_text())

    def test_invalid_json_retry_names_the_missing_field(self):
        os.environ["STUB_FILE"] = json.dumps({"handoff": "DEVELOPER"})
        os.environ["STUB_REPLY"] = "done"
        self.assertEqual(self.go("implement", ok_at=1), 3)
        self.assertIn("missing field 'branch'", (self.stub / "argv").read_text())

    REV = {"handoff": "REVIEWER", "verdict": "approved", "risk": "low", "docs_impact": "no", "findings": []}
    BLOCK = {"id": "B1", "file": "a:1", "finding": "x", "case": "empty input", "severity": "blocking"}

    def test_reviewer_has_no_write_tool_and_its_last_json_object_is_accepted(self):
        minor = {"id": "M1", "file": "a:1", "finding": "x", "case": "", "severity": "minor"}
        rev = {**self.REV, "findings": [minor]}
        os.environ["STUB_REPLY"] = 'Looks fine {not json}\n```json\n' + json.dumps(rev) + "\n```\nThanks"
        self.assertEqual(self.go("review_full", ok_at=1), 0)
        argv = (self.stub / "argv").read_text().splitlines()
        self.assertNotIn("write", argv[argv.index("--tools") + 1].split(","))
        h = json.loads((self.run_dir / "review_full.handoff.json").read_text())
        self.assertEqual((h["verdict"], h["findings"]), ("approved", [minor]))

    def test_findings_stay_typed_and_the_last_object_wins(self):
        b2 = {**self.BLOCK, "id": "B2", "file": "b:2"}
        rev = {**self.REV, "verdict": "changes-requested", "findings": [self.BLOCK, b2]}
        os.environ["STUB_REPLY"] = "For example: " + json.dumps({"handoff": "REVIEWER"}) + " is the shape.\n" + json.dumps(rev)
        self.assertEqual(self.go("review_full", ok_at=1), 0)
        h = json.loads((self.run_dir / "review_full.handoff.json").read_text())
        self.assertEqual([f["id"] for f in h["findings"]], ["B1", "B2"])

    def test_reviewer_schema_rejections(self):
        schema = json.loads((sm.ROOT / "roles/reviewer/handoff.schema.json").read_text())["REVIEWER"]
        no_case = {k: v for k, v in self.BLOCK.items() if k != "case"}
        sec = {**self.BLOCK, "severity": "minor", "finding": "unfiltered comment author is a security hole"}
        for name, obj, msg in [
                ("string finding", {**self.REV, "findings": ["a:1 — x"]}, "must be an object"),
                ("blocking without case", {**self.REV, "verdict": "changes-requested", "findings": [no_case]}, "needs a non-empty 'case'"),
                ("empty case", {**self.REV, "verdict": "changes-requested", "findings": [{**self.BLOCK, "case": " "}]}, "needs a non-empty 'case'"),
                ("approved with blocking", {**self.REV, "findings": [self.BLOCK]}, "must be 'changes-requested'"),
                ("changes without blocking", {**self.REV, "verdict": "changes-requested"}, "must be 'approved'"),
                ("bad severity", {**self.REV, "findings": [{**self.BLOCK, "severity": "nit"}]}, "must be one of blocking, minor"),
                ("old blocking/nits", {k: v for k, v in self.REV.items() if k != "findings"}, "missing field 'findings'")]:
            with self.subTest(name):
                self.assertTrue(any(msg in e for e in run_role.validate(obj, schema)), run_role.validate(obj, schema))
        self.assertEqual(run_role.validate({**self.REV, "verdict": "changes-requested", "findings": [self.BLOCK]}, schema), [])
        # replay of the #94 behaviour handoff: the security hole filed as a nit is rejected
        replay = {**self.REV, "findings": [sec]}
        self.assertTrue(any("names security or data loss" in e for e in run_role.validate(replay, schema)))

    def test_reviewer_retry_gets_the_parser_error(self):
        sec = {**self.BLOCK, "severity": "minor", "finding": "comment author is not filtered: security"}
        os.environ["STUB_REPLY"] = json.dumps({**self.REV, "findings": [sec]})
        self.assertEqual(self.go("review_full", ok_at=1), 3)
        argv = (self.stub / "argv").read_text()
        self.assertIn("rejected", argv)
        self.assertIn("findings[0]. is minor but names security or data loss".replace("[0].", "[0]"), argv)

    def test_malformed_handoff_runs_the_fallback_once(self):
        for env in ("STUB_PROSE", "STUB_UNFENCED"):
            with self.subTest(env):
                (self.stub / "calls").unlink(missing_ok=True)
                os.environ.pop("STUB_PROSE", None)
                os.environ[env] = "1"
                self.assertEqual(self.go("review_full", ok_at=3), 0)
                self.assertEqual((self.calls(), self.argv_model()), (3, self.model("cheap")))
                h = json.loads((self.run_dir / "review_full.handoff.json").read_text())
                self.assertEqual((h["fallback_from"], h["model"]), (self.model("review_strong"), self.model("cheap")))
                self.assertIn(f"falls back from {self.model('review_strong')} to {self.model('cheap')}",
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
            self.assertNotIn(f"secret-{other}", seen)  # the other reviewer's output is never on disk meanwhile
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

    def test_lean_flags_and_role_skills(self):
        skills = self.root / "skills/vendor"
        (self.root / ".agents/skills/ponytail-review").mkdir(parents=True)
        (self.root / ".agents/skills/ponytail-review/SKILL.md").write_text("installed")
        for n in ("ponytail-review", "other"):
            (skills / n).mkdir(parents=True)
            (skills / n / "SKILL.md").write_text("x")
        os.environ["STUB_OK_AT"] = "1"
        self.addCleanup(os.environ.pop, "STUB_OK_AT", None)
        (self.run_dir / "review.in.md").write_text("mode: behaviour\n")
        run_role.run(self.root, "9", "review_full", "review")
        argv = (self.stub / "argv").read_text().split("\n")
        for flag in ("--no-skills", "--no-extensions", "--no-prompt-templates"):
            self.assertIn(flag, argv)
        self.assertEqual([a for a in argv if a.endswith("/ponytail-review")], [str(skills / "ponytail-review")])
        self.assertNotIn(str(skills / "other"), argv)

    def test_other_harness_runs_and_logs_unknown_usage(self):
        config = sm.yaml.safe_load((self.root / "factory.yaml").read_text())
        for r in config["roles"].values():
            r.pop("harness", None)
        config["targets"]["local"]["harness"] = "claude-code"
        (self.root / "factory.yaml").write_text(sm.yaml.safe_dump(config))
        os.environ.update(STUB_REPLY=json.dumps({"type": "result", "session_id": "s", "usage": {"input_tokens": 4},
                                                 "result": json.dumps(self.REV)}))
        self.assertEqual(self.go("review_full", ok_at=1), 0)
        argv = (self.stub / "argv").read_text().splitlines()
        self.assertEqual((argv[0], argv[argv.index("--model") + 1]), ("-p", self.model("review_strong")))
        ev = next(e for e in map(json.loads, (self.run_dir / "events.log").read_text().splitlines()) if e["event"] == "usage")
        self.assertEqual((ev["input"], ev["output"], ev["cost"]), (4, None, None))
        os.environ["STUB_REPLY"] = json.dumps({"type": "result", "session_id": "s", "result": json.dumps(self.DEV)})
        self.assertEqual(self.go("implement", ok_at=1), 0)
        self.assertIn("--session-id", (self.stub / "argv").read_text())

    ROUTE_URL = "http://127.0.0.1:4242/wire/abc/claude-code/anthropic"

    def central(self):
        """Switch the copied factory.yaml to central, with a fake HOME holding the Central agent config
        and a user models.json that has another provider."""
        home = self.root / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude/settings.json").write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": self.ROUTE_URL}}))
        (home / ".pi/agent").mkdir(parents=True)
        (home / ".pi/agent/models.json").write_text(json.dumps(
            {"providers": {"hetzner": {"baseUrl": "https://q", "apiKey": "k", "models": [{"id": "Q"}]}}}))
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        self.addCleanup(os.environ.__setitem__, "HOME", old)
        config = sm.yaml.safe_load((self.root / "factory.yaml").read_text())
        config["targets"]["local"]["model_access"] = "central"
        (self.root / "factory.yaml").write_text(sm.yaml.safe_dump(config))
        return config

    def test_central_agent_dir_and_every_role_call_gets_it(self):
        config = self.central()
        (self.stub / "proxy-up").touch()
        self.go("intake", ok_at=1)
        self.go("review_behaviour", ok_at=1)  # a reviewer, in its private dir
        os.environ.update(STUB_REPLY="OK", STUB_EXIT="0")
        self.assertTrue(preflight.check("m", env=model_access.pi_env(config, self.root))[0])
        agent = self.root / ".factory/pi-agent"
        self.assertEqual(agent.stat().st_mode & 0o777, 0o700)
        self.assertEqual((agent / "models.json").stat().st_mode & 0o777, 0o600)
        providers = json.loads((agent / "models.json").read_text())["providers"]
        self.assertEqual(providers["anthropic"], {"baseUrl": self.ROUTE_URL})
        self.assertEqual(providers["hetzner"]["apiKey"], "k")
        self.assertEqual((self.stub / "env").read_text().splitlines(),
                         [f"{agent} {model_access.PLACEHOLDER_KEY}"] * 3)
        self.assertNotIn("proxy start", (self.stub / "jbcentral.calls").read_text())

    def test_central_preflight_starts_the_proxy_once(self):
        config = self.central()
        self.assertIn("PI_CODING_AGENT_DIR", model_access.pi_env(config, self.root))
        calls = self.stub / "jbcentral.calls"
        self.assertEqual(calls.read_text().splitlines(), ["status", "proxy start", "status"])
        (self.stub / "proxy-up").unlink()
        (self.stub / "proxy-broken").touch()
        calls.unlink()
        with self.assertRaisesRegex(model_access.ModelAccessError, r"\AJetBrains Central proxy is not running[^\n]*\Z"):
            model_access.pi_env(config, self.root)
        self.assertEqual(calls.read_text().splitlines(), ["status", "proxy start", "status"])
        with self.assertRaises(SystemExit):  # a role call never reaches the model without the proxy
            self.go("intake", ok_at=1)
        self.assertFalse((self.stub / "env").exists())

    def test_central_without_route_blocks(self):
        config = self.central()
        (self.stub / "proxy-up").touch()
        (self.root / "home/.claude/settings.json").write_text("{}")
        with self.assertRaisesRegex(model_access.ModelAccessError, "no JetBrains Central claude-code route"):
            model_access.pi_env(config, self.root)

    def test_env_access_passes_nothing(self):
        if "PI_CODING_AGENT_DIR" in os.environ:
            self.addCleanup(os.environ.__setitem__, "PI_CODING_AGENT_DIR", os.environ.pop("PI_CODING_AGENT_DIR"))
        self.go("intake", ok_at=1)
        self.assertEqual((self.stub / "env").read_text().split()[0], "none")
        self.assertFalse((self.stub / "jbcentral.calls").exists())

if __name__ == "__main__":
    unittest.main()

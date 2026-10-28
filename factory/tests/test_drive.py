#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""End-to-end tests for `drive.py run` on the local-markdown tracker: stub `pi` and `gh`, a
temporary factory root and a local bare repository as `origin`. Run: uv run factory/tests/test_drive.py"""
import contextlib
import io
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
import drive  # noqa: E402
import preflight  # noqa: E402
import sm  # noqa: E402
from local_tracker import ClaimError, LocalTracker  # noqa: E402

# Replies with $STUB_DIR/reply-<mode>-<call> or reply-<mode> (mode = the input's first line); a
# missing file is empty output.
# The developer commits a file first. Counts calls per mode in $STUB_DIR/calls-<mode>, keeps the prompt.
AGENT = """#!/bin/bash
for a; do last=$a; done
if [ "$last" = "Reply with OK." ]; then
  echo x >> "$STUB_DIR/calls-preflight"
  if [ -f "$STUB_DIR/preflight-empty-once" ]; then rm "$STUB_DIR/preflight-empty-once"; exit 0; fi
  echo OK; exit 0
fi
mode=$(printf '%s\\n' "$last" | sed -n '1s/^mode: //p')
# Reviewers of the high-risk fork: record a start, wait (at most 10s) for the other one to start.
if [ "$mode" = behaviour ] || [ "$mode" = structure-security ]; then
  touch "$STUB_DIR/started-$mode"
  for _ in $(seq 100); do
    if [ -f "$STUB_DIR/started-behaviour" ] && [ -f "$STUB_DIR/started-structure-security" ]; then
      echo "$mode" >> "$STUB_DIR/overlap"; break
    fi
    sleep 0.1
  done
fi
echo x >> "$STUB_DIR/calls-$mode"
printf '%s' "$last" > "$STUB_DIR/prompt-$mode"
if [ "$mode" = implement ]; then echo hi > stub.txt; git add stub.txt; git commit -qm "#01 stub"; fi
if [ "$mode" = fix ]; then echo fix >> stub.txt; git commit -qam "#01 fix"; fi
if [ "$mode" = update ]; then echo doc > doc.md; git add doc.md; git commit -qm "#01 doc"; fi
n=$(wc -l < "$STUB_DIR/calls-$mode" | tr -d ' ')
f=
if [ -f "$STUB_DIR/reply-$mode-$n" ]; then f="$STUB_DIR/reply-$mode-$n"
elif [ -f "$STUB_DIR/reply-$mode" ]; then f="$STUB_DIR/reply-$mode"; fi
# pi --mode json: one assistant message_end with usage
[ -n "$f" ] && python3 -c 'import json,sys; print(json.dumps({"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":open(sys.argv[1]).read()}],"usage":{"input":10,"output":5,"cacheRead":0,"cacheWrite":7,"cost":{"total":0.25}}}}))' "$f"
"""
GH = """#!/bin/bash
printf '%s\\n' "$@" > "$STUB_DIR/gh-$1-$2"
case "$1 $2" in
  "pr list") echo '[]' ;;
  "pr create") echo "https://example.invalid/pull/7" ;;
  "pr view") echo '{"url":"https://example.invalid/pull/7","statusCheckRollup":[]}' ;;
  "pr comment"|"pr edit") ;;
  "pr checks")
    echo x >> "$STUB_DIR/calls-checks"
    n=$(wc -l < "$STUB_DIR/calls-checks" | tr -d ' ')
    if [ -f "$STUB_DIR/checks-register-at" ] && [ "$n" -lt "$(cat "$STUB_DIR/checks-register-at")" ]; then
      echo "no checks reported on the 'ez/01' branch" >&2; exit 1
    fi
    if [ -f "$STUB_DIR/ci-fail-until" ] && [ "$n" -le "$(cat "$STUB_DIR/ci-fail-until")" ]; then
      printf 'build\tfail\t1m\thttps://example.invalid/actions/runs/42/job/9\t\n'; exit 1
    fi
    echo "build pass" ;;
  "run view") echo "SC1083 in roles/x.sh line 3" ;;
  *) echo "stub gh: unexpected $*" >&2; exit 1 ;;
esac
"""
# Every soft question decided `yes`.
DECIDE = """#!/bin/bash
echo '[{"questions": [{"id": "risk-low", "resolution": "decided", "answer": "yes"}]}]'
"""
REPLIES = {
    "intake": "```text\nROUTER-INTAKE\nready: yes\nrisk: low\nnext: delivery\n```\n",
    "implement": "```text\nDEVELOPER\nstatus: done\ncommits:\n  - abc #01 stub\nblocked_reason: none\n```\n",
    "full": "```text\nREVIEWER\nverdict: approved\nreviewed_sha: x\nrisk: low\ndocs_impact: no\nblocking:\nnits:\n```\n",
    "behaviour": "```text\nREVIEWER\nverdict: approved\nrisk: high\ndocs_impact: no\n```\n",
    "structure-security": "```text\nREVIEWER\nverdict: approved\nrisk: high\ndocs_impact: no\n```\n",
    "summary": "```text\nROUTER-SUMMARY\ncomplete: yes\ncomment: |\n  [factory] Summary line.\n  Factory-Run: r1\n```\n",
}
CHECKS = [{"name": "has-file", "stage": "pre-review", "mode": "blocking", "run": "test -f stub.txt"},
          {"name": "tool", "stage": "pre-review", "mode": "advisory", "requires": "no-such-tool-31", "run": "exit 1"}]


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class DriveRun(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        self.root, self.stub = tmp / "factory", tmp / "stub"
        self.root.mkdir()
        self.stub.mkdir()
        config = yaml.safe_load((sm.ROOT / "factory.yaml").read_text())
        config["checks"] = CHECKS
        (self.root / "factory.yaml").write_text(yaml.safe_dump(config))
        shutil.copytree(sm.ROOT / "workflows", self.root / "workflows")
        shutil.copytree(sm.ROOT / "roles", self.root / "roles")
        for d in ("policies", "judgments"):
            shutil.copytree(sm.ROOT / d, self.root / d)
        shutil.copy(sm.ROOT / ".gitignore", self.root)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "STUB_DIR": str(self.stub),
               "FACTORY_AGENT": str(self.stub / "agent"), "FACTORY_GH": str(self.stub / "gh"),
               "FACTORY_DECIDE": str(self.stub / "decide")}
        for k, v in env.items():
            self.addCleanup(os.environ.pop, k, None)
        os.environ.update(env)
        git("init", "-q", "--bare", "-b", "main", str(tmp / "origin.git"), cwd=tmp)
        git("init", "-q", "-b", "main", cwd=self.root)
        git("add", ".", cwd=self.root)
        git("commit", "-qm", "init", cwd=self.root)
        git("remote", "add", "origin", str(tmp / "origin.git"), cwd=self.root)
        git("push", "-q", "origin", "main", cwd=self.root)
        for name, text in (("agent", AGENT), ("gh", GH), ("decide", DECIDE)):
            (self.stub / name).write_text(text)
            (self.stub / name).chmod(0o755)
        for mode, text in REPLIES.items():
            (self.stub / f"reply-{mode}").write_text(text)
        self.tr = LocalTracker(Path(shutil.copytree(HERE / "fixtures/effort", tmp / "effort")))
        self.stages, self.labels = [], 0
        labels = self.tr.set_review_labels
        self.tr.set_review_labels = lambda *a: (setattr(self, "labels", self.labels + 1), labels(*a))
        set_state = self.tr.set_state
        self.tr.set_state = lambda nn, s: (self.stages.append(s) if self.stages[-1:] != [s] else None,
                                           set_state(nn, s))

    def go(self):
        t, _ = drive.run_ticket(self.root, "01", self.tr, "executor-a", run_id="r1")
        self.assertNotIn("claimed_by: executor-a", self.tr.path("01").read_text())  # released
        self.tr.claim("01", "executor-b")  # another executor can take it now
        return t

    def calls(self, mode):
        p = self.stub / f"calls-{mode}"
        return len(p.read_text().split()) if p.exists() else 0

    def test_records_cost_per_role(self):
        self.go()
        log = [json.loads(x) for x in (self.root / ".factory/runs/01/events.log").read_text().splitlines()]
        self.assertEqual({r["role"] for r in log}, {"developer", "reviewer", "router"})
        self.assertTrue(all(r["event"] == "usage" and r["cacheWrite"] == 7 for r in log))
        table = drive.cost_table(self.root / ".factory/runs/01")
        self.assertIn("implement", table)
        self.assertIn(f"{0.25 * len(log):.3f}", table.splitlines()[-1])

    def test_delivers_ready_to_approved(self):
        t = self.go()
        self.assertEqual(self.stages, ["ready", "in-dev", "in-review", "approved"])
        self.assertEqual(LocalTracker(self.tr.issues.parent).get_state("01"), "approved")
        self.assertEqual(self.tr.read_vars("01"), sm.persisted(t))
        branch = "ez/01-driven-ticket"
        self.assertEqual(t["vars"]["branch"], branch)
        self.assertEqual(t["vars"]["pr_urls"], ["https://example.invalid/pull/7"])
        wt = self.root / ".factory/worktrees/01"
        head = subprocess.run(["git", "rev-parse", branch], cwd=self.root, capture_output=True, text=True).stdout.strip()
        self.assertEqual(t["vars"]["reviewed_sha"], head)
        remote = subprocess.run(["git", "ls-remote", "--heads", "origin", branch], cwd=self.root,
                                capture_output=True, text=True).stdout
        self.assertIn(head, remote)  # pushed
        pr = (self.stub / "gh-pr-create").read_text()
        self.assertIn("Refs #01", pr)
        self.assertIn("Factory-Run: r1", pr)
        self.assertIn("- tool (advisory): skipped", pr)
        self.assertEqual((t["out"]["ci"]["result"], t["out"]["ci"]["ci"]), ("passed", "passed"))
        self.assertIn("https://example.invalid/pull/7", (self.stub / "gh-pr-checks").read_text())
        self.assertFalse((self.stub / "gh-pr-comment").exists())  # no nits
        # The approver (dry-run) runs before publish: its verdict is in the PR body and on the ticket.
        verdict = "would-auto-approve" if shutil.which("conftest") else "needs-human"
        self.assertEqual(t["vars"]["approval"], verdict)
        self.assertIn(f"## Approver (dry-run): {verdict}", pr)
        self.assertIn(f"[factory] Approver (dry-run): {verdict}", self.tr.path("01").read_text())
        self.assertEqual(json.loads((self.root / ".factory/runs/01/approve-0.json").read_text())["paths"], ["stub.txt"])
        edit = (self.stub / "gh-pr-edit").read_text()  # after the summary: complete yes
        self.assertIn("Closes #01\n\nFactory-Run: r1", edit)
        self.assertNotIn("Refs #01", edit)
        self.assertFalse(wt.exists())  # worktree removed, run dir kept
        self.assertTrue((self.root / ".factory/runs/01/summary.out.md").exists())
        ticket = self.tr.path("01").read_text()
        self.assertIn("[factory] Summary line.", ticket.split("## Comments", 1)[1])
        self.assertIn("Risk: low", ticket)
        prompt = (self.stub / "prompt-implement").read_text()
        self.assertIn("mode: implement", prompt)
        self.assertIn("# Driven ticket", prompt)  # the local ticket file, not `gh issue view`

    def test_docs_impact_runs_the_doc_writer_before_the_approver(self):
        (self.stub / "reply-full").write_text(REPLIES["full"].replace("docs_impact: no", "docs_impact: yes"))
        (self.stub / "reply-update").write_text("```text\nDOC-WRITER\nstatus: done\nfiles: doc.md\n```\n")
        t = self.go()
        self.assertEqual((t["state"], self.calls("update"), t["out"]["document"]["files"]), ("approved", 1, "doc.md"))
        self.assertIn("docs_impact", (self.stub / "prompt-update").read_text())  # the reviewers' handoffs
        self.assertEqual(json.loads((self.root / ".factory/runs/01/approve-0.json").read_text())["paths"],
                         ["doc.md", "stub.txt"])  # the approver sees the doc commit

    def test_empty_first_preflight_reply_passes_on_retry(self):
        (self.stub / "preflight-empty-once").touch()
        self.assertEqual(self.go()["state"], "approved")
        config = yaml.safe_load((self.root / "factory.yaml").read_text())
        self.assertEqual(self.calls("preflight"), len(preflight.run_models(config)) + 1)

    def test_ci_without_registered_checks_is_pending_until_they_appear(self):
        self.patch(drive, "CI_POLL", 0.01)
        (self.stub / "checks-register-at").write_text("3")  # two reads see no checks
        t = self.go()
        self.assertEqual((t["out"]["ci"]["result"], self.calls("checks")), ("passed", 5))  # 2 + watch + final
        self.tr.release("01", "executor-b")
        d = drive.Delivery(self.root, "01", self.tr, "r1")
        d.wt.mkdir()
        (self.stub / "calls-checks").unlink()
        (self.stub / "checks-register-at").write_text("999")
        self.patch(drive, "CI_WAIT", 0.3)
        self.assertEqual(d.read_ci("https://example.invalid/pull/7"), ("pending", "pending after 0.3s"))

    def test_summary_runs_after_approved_with_stage_ci_and_events(self):
        self.go()
        prompt = (self.stub / "prompt-summary").read_text()
        self.assertIn("stage: approved", prompt)
        self.assertIn("ci: passed", prompt)
        self.assertIn("publish.done", prompt.split("## events", 1)[1])
        self.assertEqual(self.stages[-1], "approved")

    def test_failed_ci_goes_to_fix_then_rereview_then_approved(self):
        (self.stub / "reply-intake").write_text(REPLIES["intake"].replace("risk: low", "risk: high"))
        (self.stub / "reply-fix").write_text(REPLIES["implement"])
        (self.stub / "ci-fail-until").write_text("3")  # the first read, --watch and final read fail
        wt = self.root / ".factory/worktrees/01"
        finish = drive.Delivery.finish
        seen = []
        self.patch(drive.Delivery, "finish", lambda d, t, s: (seen.append(wt.is_dir()), finish(d, t, s)))
        t = self.go()
        self.assertEqual((t["state"], t["vars"]["fix_round"]), ("approved", 1))
        self.assertEqual(self.stages, ["ready", "in-dev", "in-review", "changes-requested", "in-review", "approved"])
        fix = (self.stub / "prompt-fix").read_text()
        self.assertIn("## checks", fix)
        self.assertIn("SC1083 in roles/x.sh line 3", fix)  # the failing job log
        self.assertEqual((self.calls("behaviour"), self.calls("structure-security")), (2, 2))  # re-review
        edit = (self.stub / "gh-pr-edit").read_text()  # last edit: the summary's Closes body
        self.assertIn("#01 stub\n", edit.split("## What changed", 1)[1])
        self.assertIn("#01 fix", edit)
        self.assertEqual(self.labels, 2)  # set_review_labels on both publishes
        self.assertIn("- Fix rounds: 1", edit)
        self.assertIn("#01 fix", (self.stub / "prompt-summary").read_text().split("## commits", 1)[1])
        self.assertEqual(seen, [True])
        self.assertFalse(wt.exists())  # removed once CI is green
        self.assertEqual(json.loads((self.root / ".factory/runs/01/sm.json").read_text())["state"], "approved")

    def test_pending_ci_keeps_the_worktree(self):
        self.patch(drive.Delivery, "read_ci", lambda d, url: ("pending", "pending"))
        self.assertEqual(self.go()["state"], "approved")
        self.assertTrue((self.root / ".factory/worktrees/01").is_dir())

    def test_merge_with_base_runs_blocking_checks(self):
        d = drive.Delivery(self.root, "01", self.tr, "r1")
        d.run_dir.mkdir(parents=True)
        d.start({})
        (d.wt / "stub.txt").write_text("branch\n")
        git("add", "stub.txt", cwd=d.wt)
        git("commit", "-qm", "#01 stub", cwd=d.wt)
        status, report = d.merged_checks("main")
        self.assertEqual(status, "passed", report)
        self.assertIn("has-file (blocking): pass", report)
        self.assertNotIn("tool (advisory)", report)
        (self.root / "stub.txt").write_text("main\n")  # another PR merged a conflicting change
        git("add", "stub.txt", cwd=self.root)
        git("commit", "-qm", "other", cwd=self.root)
        git("push", "-q", "origin", "main", cwd=self.root)
        status, report = d.merged_checks("main")
        self.assertEqual(status, "failed")
        self.assertIn("does not merge with origin/main", report)
        self.assertFalse(d.wt.with_name("01-merge").exists())

    def test_high_risk_reviewers_run_in_parallel(self):
        (self.stub / "reply-intake").write_text(REPLIES["intake"].replace("risk: low", "risk: high"))
        t = self.go()
        self.assertEqual(t["state"], "approved")
        self.assertEqual(sorted((self.stub / "overlap").read_text().split()), ["behaviour", "structure-security"])
        self.assertIn("review_structure_security", (self.stub / "prompt-summary").read_text())

    def test_claimed_ticket_exits_2_with_one_line(self):
        self.tr.claim("01", "executor-b")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = drive.main(["run", "01", "--tracker", f"local:{self.tr.issues.parent}", "--executor", "executor-a"])
        self.assertEqual((code, err.getvalue().count("\n")), (2, 1))
        self.assertIn("claimed by executor-b", err.getvalue())
        self.tr.release("01", "executor-b")
        self.tr.set_state = lambda nn, st: (_ for _ in ()).throw(ValueError("unrelated"))
        self.patch(drive, "LocalTracker", lambda _: self.tr)
        with self.assertRaisesRegex(ValueError, "unrelated"):  # not mapped to exit 2
            drive.main(["run", "01", "--tracker", f"local:{self.tr.issues.parent}", "--executor", "executor-a"])

    def patch(self, obj, name, value):
        self.addCleanup(setattr, obj, name, getattr(obj, name))
        setattr(obj, name, value)

    def test_missing_tool_check_is_skipped(self):
        t = self.go()
        self.assertEqual(t["out"]["checks"]["checks"], [
            {"name": "has-file", "mode": "blocking", "status": "pass"},
            {"name": "tool", "mode": "advisory", "status": "skipped"}])
        self.assertIn("## tool (advisory): skipped", (self.root / ".factory/runs/01/checks-0.md").read_text())

    def test_empty_agent_output_runs_at_most_twice(self):
        (self.stub / "reply-implement").unlink()
        t = self.go()
        self.assertEqual((t["state"], self.calls("implement")), ("failed", 4))  # 2 runs, each model + fallback
        self.assertEqual(self.stages[-1], "failed")

    def test_resume_restarts_a_failed_ticket_at_the_failed_step(self):
        self.tr.claim("01", "executor-b")
        before = self.tr.path("01").read_text()
        self.assertEqual(drive.main(["run", "01", "--tracker", f"local:{self.tr.issues.parent}", "--executor",
                                     "executor-a", "--resume"]), 2)  # held elsewhere: stop, no writes
        self.assertEqual(self.tr.path("01").read_text(), before)
        self.tr.release("01", "executor-b")
        with self.assertRaises(drive.ResumeError):  # not failed: nothing to resume
            drive.run_ticket(self.root, "01", self.tr, "executor-a", resume=True)
        (self.stub / "reply-implement").unlink()
        self.assertEqual(self.go()["state"], "failed")
        self.tr.release("01", "executor-b")
        self.assertEqual(self.tr.read_vars("01")["_resume"]["state"], "in-dev")
        (self.stub / "reply-implement").write_text(REPLIES["implement"])
        t, _ = drive.run_ticket(self.root, "01", self.tr, "executor-a", run_id="r1", resume=True)
        self.assertEqual((t["state"], self.calls("implement"), self.calls("intake")), ("approved", 5, 1))
        self.assertEqual(self.stages, ["ready", "in-dev", "failed", "in-dev", "in-review", "approved"])
        self.assertNotIn("_resume", self.tr.read_vars("01"))

    def test_resume_point_reruns_unfinished_fork_branches(self):
        t = {"state": "in-review", "pending": ["b"], "fork": {"name": "review", "join": "all",
             "results": {"a": "done", "b": None, "c": "error"}}}
        p = drive.resume_point(t)
        self.assertEqual((p["state"], p["run"]["pending"]), ("in-review", ["b", "c"]))
        self.assertEqual(p["run"]["fork"]["results"], {"a": "done", "b": None, "c": None})

    def test_unfenced_review_is_accepted(self):
        (self.stub / "reply-full").write_text("Looks fine.\nREVIEWER\nverdict: approved\nrisk: low\n")
        t = self.go()
        self.assertEqual((t["state"], self.calls("full")), ("approved", 1))

    def test_prose_after_review_falls_back_and_approves(self):
        (self.stub / "reply-full-1").write_text(REPLIES["full"] + "Thanks!\n")
        t = self.go()
        self.assertEqual((t["state"], self.calls("full")), ("approved", 2))  # one call per model
        config = yaml.safe_load((self.root / "factory.yaml").read_text())
        r = t["out"]["review_full"]
        self.assertEqual((r["result"], r["fallback_from"], r["model"]),
                         ("done", config["models"]["review"], config["models"]["cheap"]))
        self.assertIn("fell back from", (self.stub / "gh-pr-create").read_text())
        run = self.root / ".factory/runs/01"
        self.assertIn("Thanks!", (run / "review_full-attempt1.out.md").read_text())  # each attempt kept
        self.assertTrue((run / "review_full-attempt2.out.md").exists())

    def test_nits_posted_once_as_one_pr_comment(self):
        (self.stub / "reply-full").write_text(REPLIES["full"].replace("nits:\n", "nits:\n  - a.py:1 — rename x\n"))
        self.go()
        body = (self.stub / "gh-pr-comment").read_text()
        self.assertIn("a.py:1 — rename x", body)
        self.assertTrue(body.startswith("pr\ncomment\nhttps://example.invalid/pull/7\n--body\n[factory]"))
        self.assertTrue(body.rstrip().endswith("Factory-Run: r1"))

    def test_incomplete_summary_keeps_refs(self):
        (self.stub / "reply-summary").write_text("```text\nROUTER-SUMMARY\ncomplete: no\n```\n")
        t = self.go()
        self.assertEqual(t["state"], "approved")
        self.assertFalse((self.stub / "gh-pr-edit").exists())
        self.assertIn("Refs #01", (self.stub / "gh-pr-create").read_text())
        self.assertFalse((self.root / ".factory/worktrees/01").exists())

    def test_review_raising_risk_relabels_the_ticket(self):
        (self.stub / "reply-full").write_text(REPLIES["full"].replace("risk: low", "risk: high")
                                              .replace("docs_impact: no", "docs_impact: yes"))
        (self.stub / "reply-update").write_text("```text\nDOC-WRITER\nstatus: done\nfiles: none\n```\n")
        t = self.go()
        self.assertEqual((t["state"], t["vars"]["risk"]), ("approved", "high"))
        ticket = self.tr.path("01").read_text()
        self.assertIn("Risk: high", ticket)
        self.assertIn("Docs impact: yes", ticket)
        self.assertIn("- Risk: raised from low to high by the review", (self.stub / "gh-pr-create").read_text())

    def test_claim_released_at_blocked(self):
        (self.stub / "reply-implement").write_text("```text\nDEVELOPER\nstatus: blocked\nblocked_reason: x\n```\n")
        self.assertEqual(self.go()["state"], "blocked")

    def test_unsafe_base_fails_before_any_git_call(self):
        path = self.tr.path("01")
        original = path.read_text()
        for base in ("--upload-pack=touch /tmp/pwned31", "a/../b", "-x"):
            with self.subTest(base):
                path.write_text(original.replace("## Comments", f"base: {base}\n\n## Comments", 1))
                t = self.go()
                self.assertEqual(t["state"], "blocked")  # preflight failed
                self.assertIn(repr(base.split()[0]), json.dumps(t["out"]["preflight"]))
                self.assertFalse((self.root / ".factory/worktrees/01").exists())
                self.assertEqual(self.calls("intake"), 0)
                self.tr.release("01", "executor-b")
                self.tr.set_state("01", "ready")
                self.tr.write_vars("01", {})

    def test_exception_in_an_action_releases_the_claim(self):
        (self.stub / "gh").write_text("#!/bin/bash\nexit 1\n")  # preflight's `gh pr list` fails: RuntimeError
        self.assertEqual(self.go()["state"], "blocked")
        self.tr.release("01", "executor-b")
        self.tr.set_state("01", "ready")
        self.tr.write_vars("01", {})
        self.tr.set_state = lambda nn, st: (_ for _ in ()).throw(KeyError("boom"))  # raised inside the loop
        with self.assertRaises(KeyError):
            drive.run_ticket(self.root, "01", self.tr, "executor-a")
        self.assertNotIn("claimed_by: executor-a", self.tr.path("01").read_text())

    def test_claim_held_elsewhere_stops_without_running(self):
        self.tr.claim("01", "executor-b")
        with self.assertRaises(ClaimError):
            drive.run_ticket(self.root, "01", self.tr, "executor-a")
        self.assertEqual((self.stages, self.calls("intake")), ([], 0))


if __name__ == "__main__":
    unittest.main()

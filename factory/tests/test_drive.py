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
import re
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
import sm_v1 as sm  # noqa: E402
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
if [ "$mode" = merge ]; then mkdir -p knowledge/_shared; echo "- lesson" >> knowledge/_shared/lessons.md
  if [ -f "$STUB_DIR/merge-outside" ]; then echo x > outside.txt; fi; fi
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
  "pr view")
    if [ "$4" = --json ] && [ "$5" = state,mergedAt ]; then
      if grep -qxF "$3" "$STUB_DIR/merged" 2>/dev/null; then echo '{"state":"MERGED","mergedAt":"2026-01-01T00:00:00Z"}'
      else echo '{"state":"OPEN","mergedAt":null}'; fi
    else echo '{"url":"https://example.invalid/pull/7","statusCheckRollup":[]}'; fi ;;
  "pr comment"|"pr edit") ;;
  "pr merge") echo "$@" >> "$STUB_DIR/gh-merges" ;;
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
def review(risk, docs="no", findings=()):
    """A reviewer's JSON handoff reply."""
    v = "changes-requested" if any(f["severity"] == "blocking" for f in findings) else "approved"
    return json.dumps({"handoff": "REVIEWER", "verdict": v, "summary": "s", "reviewed_sha": "x", "risk": risk, "docs_impact": docs,
                       "findings": list(findings)}) + "\n"


REPLIES = {
    "intake": "```text\nROUTER-INTAKE\nready: yes\nrisk: low\nnext: delivery\n```\n",
    "implement": "```text\nDEVELOPER\nstatus: done\ncommits:\n  - abc #01 stub\nblocked_reason: none\n```\n",
    "full": review("low"),
    "behaviour": review("high"),
    "structure-security": review("high"),
    "record": "```text\nRETRO\nmanual_steps: |\n  - none\nlessons: |\n  - navigation: point to x — y\n```\n",
    "merge": "```text\nDISTILL\nstatus: done\nfiles: knowledge/_shared/lessons.md\nsummary: |\n  - lesson (2 entries)\n```\n",
    "summary": "```text\nROUTER-SUMMARY\ncomplete: yes\ncomment: |\n  [factory] Summary line.\n  Factory-Run: r1\n```\n",
}
CHECKS = [{"name": "has-file", "stage": "pre-review", "mode": "blocking", "run": "test -f stub.txt"},
          {"name": "tool", "stage": "pre-review", "mode": "advisory", "requires": "no-such-tool-31", "run": "exit 1"}]


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def strip_log(text):
    """loop output without the `<timestamp> cycle=<id> ` prefix of each line."""
    return re.sub(r"(?m)^\S+ cycle=\S+ ", "", text)


class DriveRun(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, tmp)
        self.root, self.stub = tmp / "factory", tmp / "stub"
        self.root.mkdir()
        self.stub.mkdir()
        config = yaml.safe_load((sm.ROOT / "factory.yaml").read_text())
        config["checks"] = CHECKS
        config["targets"]["local"]["model_access"] = "env"  # central is tested in test_run_role.py
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
        kinds = {r["event"] for r in log}
        self.assertTrue({"usage", "step.start", "step.end", "transition"} <= kinds)
        self.assertTrue(all(r["ts"] and r["run"] for r in log))
        log = [r for r in log if r["event"] == "usage"]
        self.assertEqual({r["role"] for r in log}, {"developer", "reviewer", "router"})
        self.assertTrue(all(r["event"] == "usage" and r["cacheWrite"] == 7 for r in log))
        table = drive.cost_table(self.root / ".factory/runs/01")
        self.assertIn("implement", table)
        self.assertIn(f"{0.25 * len(log):.3f}", table.splitlines()[-1])

    def test_retry_wait_and_human_events(self):
        d = drive.Delivery(self.root, "01", self.tr, "r1")
        d.run_dir.mkdir(parents=True)
        log = d.run_dir / "events.log"
        log.write_text(json.dumps({"event": "usage", "step": "x", "role": "developer", "model": "m", "cost": 1}) + "\n")
        d.log_merge()  # an old usage-only log must not break the merge path
        self.assertEqual([json.loads(x)["event"] for x in log.read_text().splitlines()], ["usage", "human"])
        d.emit("transition", **{"from": "a", "to": "b"})
        d.log_merge()
        d.on_step({"retries": {}}, {"retries": {"implement": 1}}, {"from": "a", "event": "e", "to": "a", "note": None})
        d.ci = lambda t: ("passed", {})
        d.run({"vars": {}}, "ci")
        ev = [json.loads(x) for x in log.read_text().splitlines()]
        self.assertTrue({"usage", "human", "wait", "retry", "transition"} <= {e["event"] for e in ev})
        self.assertEqual({e["what"] for e in ev if e["event"] == "wait"}, {"merge", "ci"})
        self.assertTrue(all(e["run"] == "r1" and e["ts"] for e in ev[1:]))

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
        self.assertIn("Closes #01", pr)
        self.assertIn("## Acceptance\nSee the ticket.", pr)
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
        (self.stub / "reply-full").write_text(review("low", "yes"))
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

    def auto_merge(self, labels=""):
        config = yaml.safe_load((self.root / "factory.yaml").read_text())
        config["roles"]["approver"].update(dry_run=False, auto_merge={"enabled": True, "max_risk": "low",
                                                                      "require": "hard-rules"})
        (self.root / "factory.yaml").write_text(yaml.safe_dump(config))
        if labels:
            self.tr.path("01").write_text(self.tr.path("01").read_text().replace("\n\n", f"\n\nlabels: {labels}\n", 1))

    @unittest.skipUnless(shutil.which("conftest"), "conftest is not installed")
    def test_auto_merge_merges_then_retro_then_done(self):
        self.auto_merge()
        t = self.go()
        self.assertEqual((t["state"], t["vars"]["approval"], t["out"]["ci"]["merge"]), ("done", "auto-approve", "yes"))
        self.assertIn(f"--match-head-commit {t['vars']['reviewed_sha']}", (self.stub / "gh-merges").read_text())
        self.assertEqual(self.stages[-2:], ["merged", "done"])

    @unittest.skipUnless(shutil.which("conftest"), "conftest is not installed")
    def test_hold_label_and_pending_ci_go_to_the_human_gate(self):
        self.auto_merge("factory:hold")
        self.assertEqual(self.go()["state"], "approved")
        self.assertFalse((self.stub / "gh-merges").exists())

    @unittest.skipUnless(shutil.which("conftest"), "conftest is not installed")
    def test_pending_ci_does_not_auto_merge(self):
        self.auto_merge()
        self.patch(drive.Delivery, "read_ci", lambda d, url: ("pending", "pending"))
        self.assertEqual(self.go()["state"], "approved")
        self.assertFalse((self.stub / "gh-merges").exists())

    def test_merge_action_refuses_a_moved_head_or_base(self):
        d = drive.Delivery(self.root, "01", self.tr, "r1")
        d.run_dir.mkdir(parents=True)
        d.start({})
        (d.wt / "stub.txt").write_text("branch\n")
        git("add", "stub.txt", cwd=d.wt)
        git("commit", "-qm", "#01 stub", cwd=d.wt)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d.wt, capture_output=True, text=True).stdout.strip()
        t = {"vars": {"reviewed_sha": "0" * 40, "pr_urls": ["https://example.invalid/pull/7"]}}
        self.assertEqual(d.merge(t)[0], "moved")  # head is not the reviewed one
        t["vars"]["reviewed_sha"] = head
        self.assertEqual(d.merge(t), ("done", {"merged": ["https://example.invalid/pull/7"]}))
        self.assertIn(f"https://example.invalid/pull/7 --merge --match-head-commit {head}",
                      (self.stub / "gh-merges").read_text())
        moved = d.merged_checks
        def base_moves(base):
            r = moved(base)
            (self.root / "other.txt").write_text("x\n")
            git("add", "other.txt", cwd=self.root)
            git("commit", "-qm", "other", cwd=self.root)
            git("push", "-q", "origin", "main", cwd=self.root)
            return r
        d.merged_checks = base_moves
        (self.stub / "gh-merges").unlink()
        self.assertEqual(d.merge(t)[0], "moved")  # base moved during the checks
        self.assertFalse((self.stub / "gh-merges").exists())
        d.merged_checks = moved
        issue = self.tr.issue
        self.tr.issue = lambda nn: {**issue(nn), "labels": ["factory:hold"]}  # held while the checks ran
        self.assertEqual(d.merge(t), ("failed", {"error": "the ticket is on hold"}))
        self.assertFalse((self.stub / "gh-merges").exists())

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

    def red_checks(self, on_base):
        """One blocking check: fails on the base too (`on_base`), or only on the branch (no stub.txt yet)."""
        config = yaml.safe_load((self.root / "factory.yaml").read_text())
        config["checks"] = [{"name": "gate", "stage": "pre-review", "mode": "blocking",
                             "run": "exit 1" if on_base else "test ! -f stub.txt"}]
        (self.root / "factory.yaml").write_text(yaml.safe_dump(config))

    def test_red_on_base_parks_the_ticket_on_one_owner_ticket(self):
        self.red_checks(True)
        self.only("01")
        t = self.go()
        self.assertEqual(t["state"], "waiting-main")
        self.assertEqual(t["out"]["checks"]["main_red"], "main_red: gate")
        owner = t["out"]["checks"]["owner"]
        self.assertEqual(self.tr.issue(owner)["title"], "Main is red: gate")
        self.assertEqual(self.tr.get_state(owner), "ready")
        self.assertIn("Risk: low", self.tr.path(owner).read_text())
        self.assertIn("gate (blocking): fail", self.tr.path(owner).read_text())
        self.assertIn(owner, self.tr._field("01", "Blocked by"))
        self.assertEqual(self.calls("diagnose") + self.calls("fix"), 0)
        self.assertFalse((self.root / ".factory/runs/01/notify.log").exists())
        # a second waiting ticket reuses the owner; the frontier holds everything but the owner
        self.assertEqual(self.tr.find_open("Main is red: gate"), owner)
        self.assertEqual(self.tr.frontier(), [owner])

    def test_red_on_branch_only_is_a_fix_round(self):
        self.red_checks(False)
        self.only("01")
        t = self.go()
        self.assertNotEqual(t["state"], "waiting-main")
        self.assertGreaterEqual(self.calls("fix"), 1)
        self.assertIsNone(self.tr.find_open("Main is red: gate"))

    def test_existing_owner_is_reused_and_waiting_ticket_resumes(self):
        self.red_checks(True)
        self.only("01")
        owner = self.tr.create_issue("Main is red: gate", "x", stage="approved")
        self.assertEqual(self.go()["out"]["checks"]["owner"], owner)
        self.assertEqual(len(list(self.tr.issues.glob("*.md"))), 2)
        # the owner is still open: the detector does not fire; once it is done, the sweep resumes 01
        self.tr.release("01", "executor-b")
        self.patch(drive, "detect", lambda *a: None)
        self.assertEqual(self.loop(), "")
        self.assertEqual(self.tr.get_state("01"), "waiting-main")
        self.patch(drive, "detect", lambda *a: {})
        self.tr.set_state(owner, "done")
        self.assertIn("ticket 01:", self.loop())
        log = (self.root / ".factory/runs/01/events.log").read_text()
        self.assertRegex(log, r'"from": "waiting-main".*"on": "main.green".*"to": "ready"')  # resumed (red again: parked again)

    def test_start_after_main_red_merges_the_fixed_base(self):
        d = drive.Delivery(self.root, "01", self.tr, "r1")
        d.run_dir.mkdir(parents=True)
        d.start({"out": {}})
        (self.root / "fix.txt").write_text("fixed\n")
        git("add", "fix.txt", cwd=self.root)
        git("commit", "-qm", "fix main", cwd=self.root)
        git("push", "-q", "origin", "main", cwd=self.root)
        d.start({"out": {}})
        self.assertFalse((d.wt / "fix.txt").exists())  # a plain restart keeps the branch as is
        d.start({"out": {"checks": {"main_red": "main_red: gate"}}})
        self.assertTrue((d.wt / "fix.txt").exists())

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
        self.assertEqual((t["state"], self.calls("implement")), ("failed", 8))  # 2 runs, each model + fallback, each with one handoff retry
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
        self.assertEqual((t["state"], self.calls("implement"), self.calls("intake")), ("approved", 9, 1))
        self.assertEqual(self.stages, ["ready", "in-dev", "failed", "in-dev", "in-review", "approved"])
        self.assertNotIn("_resume", self.tr.read_vars("01"))

    def test_resume_point_reruns_unfinished_fork_branches(self):
        t = {"state": "in-review", "pending": ["b"], "fork": {"name": "review", "join": "all",
             "results": {"a": "done", "b": None, "c": "error"}}}
        p = drive.resume_point(t)
        self.assertEqual((p["state"], p["run"]["pending"]), ("in-review", ["b", "c"]))
        self.assertEqual(p["run"]["fork"]["results"], {"a": "done", "b": None, "c": None})

    def test_text_review_is_rejected(self):
        (self.stub / "reply-full").write_text("Looks fine.\nREVIEWER\nverdict: approved\nrisk: low\n")
        t = self.go()
        self.assertNotEqual(self.calls("full"), 1)  # no valid handoff: retry, fallback

    def test_text_review_falls_back_and_approves(self):
        for n in (1, 2):  # the model and its handoff retry
            (self.stub / f"reply-full-{n}").write_text("REVIEWER\nverdict: approved\nThanks!\n")
        t = self.go()
        self.assertEqual((t["state"], self.calls("full")), ("approved", 3))  # model, its retry, fallback
        config = yaml.safe_load((self.root / "factory.yaml").read_text())
        r = t["out"]["review_full"]
        self.assertEqual((r["result"], r["fallback_from"], r["model"]),
                         ("done", config["models"]["review_strong"], config["models"]["cheap"]))
        self.assertIn("fell back from", (self.stub / "gh-pr-create").read_text())
        run = self.root / ".factory/runs/01"
        self.assertIn("Thanks!", (run / "review_full-attempt1.out.md").read_text())  # each attempt kept
        self.assertTrue((run / "review_full-attempt3.out.md").exists())

    def test_acceptance_lists_the_ticket_checks(self):
        body = "Scope.\n\nAcceptance checks:\n- a works\n- b works\n\nAffected repositories: x.\n"
        self.assertEqual(drive.Delivery.acceptance({"body": body}), ["- a works", "- b works"])

    def test_checks_report_notes_skipped_tests(self):
        d = drive.Delivery(self.root, "01", self.tr, "r1")
        d.config["checks"] = [{"name": "t", "stage": "pre-review", "mode": "blocking", "run": "echo 'OK (skipped=2)'"}]
        failed, rows, report = d.run_checks(self.root, "main")
        self.assertFalse(failed)
        self.assertIn("NOTE: 1 skip report(s)", report)

    def test_intake_split_blocks_with_the_proposal(self):
        (self.stub / "reply-intake").write_text(REPLIES["intake"].replace(
            "next: delivery", "size_estimate: ~850\nsplit: |\n  - Slice A — scope (~400)\n  - Slice B — scope (~450)\nnext: delivery"))
        t = self.go()
        self.assertEqual((t["state"], self.calls("implement")), ("blocked", 0))
        self.assertEqual(self.stages[-1], "blocked")
        text = self.tr.path("01").read_text()
        self.assertIn("Slice A", text)
        self.assertIn("Slice B", text)

    def test_nits_posted_once_as_one_pr_comment(self):
        (self.stub / "reply-full").write_text(review("low", findings=[{"id": "M1", "file": "a.py:1", "finding": "rename x", "case": "", "severity": "minor"}]))
        self.go()
        body = (self.stub / "gh-pr-comment").read_text()
        self.assertIn("a.py:1 — rename x", body)
        self.assertTrue(body.startswith("pr\ncomment\nhttps://example.invalid/pull/7\n--body\n[factory]"))
        self.assertTrue(body.rstrip().endswith("Factory-Run: r1"))

    def test_incomplete_summary_keeps_refs(self):
        (self.stub / "reply-summary").write_text("```text\nROUTER-SUMMARY\ncomplete: no\n```\n")
        t = self.go()
        self.assertEqual(t["state"], "approved")
        self.assertIn("Refs #01", (self.stub / "gh-pr-edit").read_text())
        self.assertIn("Closes #01", (self.stub / "gh-pr-create").read_text())
        self.assertFalse((self.root / ".factory/worktrees/01").exists())

    def test_review_raising_risk_relabels_the_ticket(self):
        (self.stub / "reply-full").write_text(review("high", "yes"))
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

    def approved(self, nn, *urls):
        self.tr.set_state(nn, "approved")
        self.tr.write_vars(nn, {"pr_urls": list(urls), "run_id": "r1"})

    def test_unmerged_pr_waits_without_writes(self):
        self.approved("01", "https://example.invalid/pull/7", "https://example.invalid/pull/8")
        (self.stub / "merged").write_text("https://example.invalid/pull/7\n")  # one of two merged
        before = self.tr.path("01").read_text()
        self.patch(drive, "LocalTracker", lambda _: self.tr)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = drive.main(["run", "01", "--tracker", f"local:{self.tr.issues.parent}", "--executor", "executor-a"])
        self.assertEqual((code, out.getvalue()), (0, "waiting for pr.merged\n"))
        self.assertEqual(self.tr.path("01").read_text(), before)
        self.assertIn("state,mergedAt", (self.stub / "gh-pr-view").read_text())

    def test_merged_pr_moves_the_ticket_to_done(self):
        self.approved("01", "https://example.invalid/pull/7")
        (self.stub / "merged").write_text("https://example.invalid/pull/7\n")
        t, trace = drive.poll(self.root, "01", self.tr, "executor-a")
        self.assertEqual([(r["event"], r["to"]) for r in trace], [("pr.merged", "merged"), ("retro.done", "done")])
        self.assertEqual((self.tr.get_state("01"), self.stages[-2:]), ("done", ["merged", "done"]))
        self.assertNotIn("claimed_by: executor-a", self.tr.path("01").read_text())  # released by the driver

    def test_retro_entry_pushed_to_knowledge_inbox_after_the_merge(self):
        self.go()
        self.tr.release("01", "executor-b")
        self.assertFalse((self.stub / "calls-record").exists())  # not before the merge
        (self.stub / "merged").write_text("https://example.invalid/pull/7\n")
        t, _ = drive.poll(self.root, "01", self.tr, "executor-a")
        k = yaml.safe_load((self.root / "factory.yaml").read_text())["knowledge"]
        rel = "knowledge/offline-eval-factory/retro-01-r1.md"
        self.assertEqual((t["state"], t["out"]["retro"]["result"], t["out"]["retro"]["entry"]), ("done", "done", rel))
        git("fetch", "-q", "origin", cwd=self.root)
        entry = subprocess.run(["git", "show", f"origin/{k['branch']}:{rel}"], cwd=self.root, capture_output=True,
                               text=True, check=True).stdout
        for line in ("- cost: $", "- wall time: ", "- fix rounds: 0", "- blocking findings: 0",
                     "- reviewer agreement: n/a", "- approver (dry-run): ", "- ci: passed", "navigation: point to x"):
            self.assertIn(line, entry)
        self.assertNotIn("knowledge/", subprocess.run(["git", "ls-tree", "-r", "--name-only", "origin/main"],
                                                      cwd=self.root, capture_output=True, text=True).stdout)
        self.assertIn("## numbers", (self.stub / "prompt-record").read_text())

    def test_blocking_ids_name_id_and_file_of_blocking_findings(self):
        f = [{"id": "B1", "file": "a.py:1", "finding": "x", "case": "c", "severity": "blocking"},
             {"id": "M1", "file": "b.py:2", "finding": "y", "case": "", "severity": "minor"}]
        self.assertEqual(drive.blocking_ids({"findings": f}), ["B1 a.py:1"])

    def test_retro_counts_blocking_findings_per_reviewer_and_round(self):
        d = drive.Delivery(self.root, "01", self.tr, "r1")
        d.run_dir.mkdir(parents=True)
        block = {"verdict": "changes-requested", "findings": [{"id": "B1", "file": "x", "finding": "f", "case": "c", "severity": "blocking"}]}
        for name in ("review_behaviour", "review_structure_security", "review_behaviour-1"):
            (d.run_dir / f"{name}.handoff.json").write_text(json.dumps(block))
        numbers = d.run_numbers(sm.new_ticket(d.m))
        self.assertIn("- blocking findings: 3 (", numbers)
        self.assertIn("- reviewer agreement: 1/1 rounds", numbers)

    def test_old_format_handoff_reads_next_to_a_new_one(self):
        d = drive.Delivery(self.root, "01", self.tr, "r1")
        d.run_dir.mkdir(parents=True)
        old = {"verdict": "changes-requested", "blocking": ["bad"], "nits": ["meh"]}
        (d.run_dir / "review_behaviour.handoff.json").write_text(json.dumps(old))
        (d.run_dir / "review_structure_security.handoff.json").write_text(json.dumps(
            {"findings": [{"id": "B1", "file": "x", "finding": "f", "case": "c", "severity": "blocking"}]}))
        self.assertIn("- blocking findings: 2 (", d.run_numbers(sm.new_ticket(d.m)))
        self.assertEqual([(x["severity"], x["id"]) for x in drive.findings(old)], [("blocking", ""), ("minor", "")])
        self.assertEqual(drive.blocking_ids(old), [" "])

    def test_raising_agent_step_is_an_error_outcome(self):
        d = drive.Delivery(self.root, "01", self.tr, "r1")
        d.role_input = lambda *a: 1 / 0
        d.run_dir.mkdir(parents=True)
        res, out = d.run(sm.new_ticket(d.m), "implement")
        self.assertEqual(res, "error")
        self.assertIn("ZeroDivisionError", out["error"])

    def inbox(self):
        """Push the fixture retro entries to the knowledge branch, as the retro does."""
        k = yaml.safe_load((self.root / "factory.yaml").read_text())["knowledge"]
        git("checkout", "-qb", "inbox", cwd=self.root)
        (self.root / "knowledge/offline-eval-factory").mkdir(parents=True)
        for f in (HERE / "fixtures/inbox").iterdir():
            shutil.copy(f, self.root / "knowledge/offline-eval-factory" / f.name)
        git("add", "knowledge", cwd=self.root)
        git("commit", "-qm", "entries", cwd=self.root)
        git("push", "-q", "origin", f"HEAD:refs/heads/{k['branch']}", cwd=self.root)
        git("checkout", "-q", "main", cwd=self.root)
        return k

    def remote(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True, text=True).stdout

    def test_distill_opens_one_pr_from_inbox_entries(self):
        self.assertIsNone(drive.distill(self.root))  # no inbox branch yet
        k = self.inbox()
        url = drive.distill(self.root, run_id="d1")
        self.assertEqual((url, self.calls("merge")), ("https://example.invalid/pull/7", 1))
        pr = (self.stub / "gh-pr-create").read_text().split("\n")
        branch = pr[pr.index("--head") + 1]
        self.assertTrue(branch.startswith(k["distill_branch"] + "-"))
        self.assertEqual(pr[pr.index("--base") + 1], "main")
        body = pr[pr.index("--body") + 1:]
        self.assertTrue(body[0].startswith("[factory] Distill"))
        self.assertIn("- knowledge/offline-eval-factory/retro-02-r2.md", body)
        self.assertIn("Factory-Run: d1", body)
        self.assertIn("SC2086", (self.stub / "prompt-merge").read_text())  # the entries are the input
        self.assertNotIn("## Ticket", (self.stub / "prompt-merge").read_text())
        git("fetch", "-q", "origin", cwd=self.root)
        self.assertEqual(self.remote("show", f"origin/{branch}:knowledge/distilled.txt").split(),
                         [f"knowledge/offline-eval-factory/retro-0{n}-r{n}.md" for n in (1, 2)])
        self.assertIn("lesson", self.remote("show", f"origin/{branch}:knowledge/_shared/lessons.md"))
        self.assertNotIn("retro-01", self.remote("ls-tree", "-r", "--name-only", f"origin/{branch}"))
        self.assertNotIn("knowledge/", self.remote("ls-tree", "-r", "--name-only", "origin/main"))  # main untouched
        self.assertFalse((self.root / ".factory/worktrees/distill").exists())
        git("merge", "-q", f"origin/{branch}", cwd=self.root)  # the human merges the PR
        git("push", "-q", "origin", "main", cwd=self.root)
        self.assertIsNone(drive.distill(self.root))  # every entry is merged: no second PR
        self.assertEqual(self.calls("merge"), 1)

    def test_distill_rejects_edits_outside_the_knowledge_dir(self):
        self.inbox()
        (self.stub / "merge-outside").touch()
        with self.assertRaisesRegex(ValueError, "outside knowledge/: outside.txt"):
            drive.distill(self.root)
        self.assertFalse((self.stub / "gh-pr-create").exists())

    def test_loop_runs_distill_once_per_interval(self):
        config = yaml.safe_load((self.root / "factory.yaml").read_text())
        self.assertEqual([drive.distill_due(self.root, config) for _ in range(2)], [True, False])
        config["knowledge"]["distill_every_days"] = 0
        self.assertFalse(drive.distill_due(self.root, config))

    def only(self, *keep):
        """Drop the fixture tickets other than `keep`, so the loop sees only those."""
        for f in self.tr.issues.glob("*.md"):
            if f.name.split("-", 1)[0] not in keep:
                f.unlink()

    def loop(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(drive.loop(self.root, self.tr, "executor-a"), 0)
        return strip_log("".join(x for x in out.getvalue().splitlines(True) if " cycle=" in x))  # no notify lines

    def test_loop_once_moves_every_merged_approved_ticket(self):
        self.only("01", "05", "06")
        self.approved("01", "https://example.invalid/pull/7")
        self.approved("06", "https://example.invalid/pull/9")
        (self.stub / "merged").write_text("https://example.invalid/pull/7\nhttps://example.invalid/pull/9\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(drive.loop(self.root, self.tr, "executor-a", once=True), 0)
        self.assertEqual(strip_log(out.getvalue()), "ticket 01: final: done\nticket 06: final: done\n")
        self.assertEqual([self.tr.get_state(n) for n in ("01", "05", "06")], ["done", "done", "done"])

    def test_loop_leaves_an_unmerged_ticket_unchanged(self):
        self.only("01", "05")
        self.approved("01", "https://example.invalid/pull/7")
        before = [f.read_text() for f in sorted(self.tr.issues.glob("*.md"))]
        self.assertEqual(self.loop(), "")
        self.assertEqual([f.read_text() for f in sorted(self.tr.issues.glob("*.md"))], before)

    def test_loop_drives_a_custom_human_state_and_event(self):
        """A machine with a `qa-review` human state and a `qa.passed` event: no driver change needed."""
        config = yaml.safe_load((self.root / "factory.yaml").read_text())
        config["tracker"]["stages"]["qa-review"] = "factory:qa-review"
        config["events"]["qa.passed"] = {"run": 'test -f "$STUB_DIR/qa-ok-$FACTORY_TICKET"'}
        (self.root / "factory.yaml").write_text(yaml.safe_dump(config))
        path = self.root / "workflows/delivery.v1.sm.yaml"
        machine = yaml.safe_load(path.read_text())
        machine["states"]["qa-review"] = {"actor": "human"}
        machine["events"].append("qa.passed")
        for tr in machine["transitions"]:
            if tr.get(True) in ("ci.passed", "ci.pending") and tr.get("to") == "approved":
                tr["to"] = "qa-review"
        machine["transitions"].append({"from": "qa-review", "on": "qa.passed", "to": "approved"})
        path.write_text(yaml.safe_dump(machine, sort_keys=False))
        self.only("01")
        self.assertEqual(self.loop(), "ticket 01: final: qa-review\n")  # ran once, then waits for QA
        self.assertEqual(self.loop(), "")
        (self.stub / "qa-ok-01").touch()
        self.assertEqual(self.loop(), "ticket 01: final: approved\n")
        (self.stub / "merged").write_text("https://example.invalid/pull/7\n")
        self.assertEqual(self.loop(), "ticket 01: final: done\n")
        self.assertEqual(self.stages, ["ready", "in-dev", "in-review", "qa-review", "approved", "merged", "done"])

    def test_entering_blocked_runs_diagnose_and_notify_once(self):
        (self.stub / "reply-implement").write_text("```text\nDEVELOPER\nstatus: blocked\nblocked_reason: x\n```\n")
        (self.stub / "reply-diagnose").write_text(
            "```text\nROUTER-DIAGNOSE\nstep: implement\ncomment: |\n  [factory] Blocked: x.\n  Factory-Run: r1\n```\n")
        self.only("01")
        out = self.loop()
        self.assertIn("ticket 01: final: blocked", out)
        self.assertEqual(self.loop(), "")  # the next cycles poll nothing and run nothing
        self.assertEqual(self.calls("diagnose"), 1)
        self.assertIn("stage: blocked", (self.stub / "prompt-diagnose").read_text())
        self.assertEqual(self.tr.path("01").read_text().count("[factory] Blocked: x."), 1)
        notes = (self.root / ".factory/runs/01/notify.log").read_text().splitlines()
        self.assertEqual(len(notes), 1)
        self.assertIn("ticket 01: blocked", notes[0])
        self.assertEqual(self.tr.read_vars("01")["_run"]["hooks"], [])

    def blocked_ticket(self):
        self.only("01")
        self.tr.set_state("01", "blocked")
        self.tr.write_vars("01", {"checked_comment": 0})

    def comment_loop(self, text, verdict):
        (self.stub / "reply-recheck").write_text(f"```text\nROUTER-RECHECK\nactionable: {verdict}\nreason: r\n```\n")
        self.tr.comment("01", text)
        return self.loop()

    def test_actionable_comment_resumes_a_blocked_ticket(self):
        self.blocked_ticket()
        out = self.comment_loop("The answer is 42.", "yes")
        self.assertIn("ticket 01: final: approved", out)
        self.assertEqual(self.calls("recheck"), 1)
        self.assertEqual(self.calls("intake"), 1)
        self.assertEqual(self.tr.read_vars("01")["resumes"], 1)
        self.assertIn("The answer is 42.", (self.stub / "prompt-recheck").read_text())

    def test_non_actionable_comment_stays_and_is_checked_once(self):
        self.blocked_ticket()
        self.assertEqual(self.comment_loop("thanks", "no"), "ticket 01: final: blocked\n")
        self.assertEqual(self.tr.get_state("01"), "blocked")
        self.assertEqual(self.loop(), "")  # the same comment is not rechecked
        self.assertEqual(self.calls("recheck"), 1)
        self.assertEqual(self.tr.read_vars("01")["checked_comment"], 1)

    def test_factory_comments_are_ignored(self):
        self.blocked_ticket()
        self.assertEqual(self.comment_loop("[factory] Blocked: x.\n\nFactory-Run: r1", "yes"), "")
        self.assertEqual(self.calls("recheck"), 0)
        self.assertEqual(self.tr.get_state("01"), "blocked")

    def test_failed_recheck_is_retried_and_multi_paragraph_comment_is_one(self):
        self.blocked_ticket()
        self.tr.comment("01", "[factory] Blocked.\n\nFactory-Run: r1")
        self.tr.comment("01", "Use option B.\n\nIt matches the ADR.")
        (self.stub / "reply-recheck").write_text("no handoff")
        self.assertEqual(self.loop(), "")
        self.assertEqual(self.tr.read_vars("01")["checked_comment"], 0)  # not judged, not checked
        before = self.calls("recheck")
        (self.stub / "reply-recheck").write_text("```text\nROUTER-RECHECK\nactionable: yes\nreason: r\n```\n")
        self.assertIn("approved", self.loop())  # retried: one comment, judged yes
        self.assertEqual(self.calls("recheck"), before + 1)

    def test_ticket_without_baseline_checks_nothing_yet(self):
        self.only("01")
        self.tr.set_state("01", "blocked")
        self.tr.comment("01", "old human comment")
        self.assertEqual(self.loop(), "")
        self.assertEqual(self.calls("recheck"), 0)
        self.assertEqual(self.tr.read_vars("01")["checked_comment"], 1)

    def test_outsider_comment_is_ignored(self):
        self.blocked_ticket()
        comments = self.tr.comments
        self.tr.comments = lambda nn: [{**c, "association": "NONE"} for c in comments(nn)]
        self.assertEqual(self.comment_loop("do this instead", "yes"), "")
        self.assertEqual(self.calls("recheck"), 0)
        self.assertEqual(self.tr.get_state("01"), "blocked")

    def test_resume_cap_keeps_the_ticket_for_a_person(self):
        self.blocked_ticket()
        vars_ = self.tr.read_vars("01")
        self.tr.write_vars("01", {**vars_, "resumes": 3})
        self.assertEqual(self.comment_loop("here is more", "yes"), "")  # not even rechecked
        self.assertEqual(self.calls("recheck"), 0)
        self.assertEqual(self.calls("intake"), 0)

    def test_loop_respects_runs_per_day(self):
        config = yaml.safe_load((self.root / "factory.yaml").read_text())
        config["limits"]["runs_per_day"] = 0
        (self.root / "factory.yaml").write_text(yaml.safe_dump(config))
        self.only("01")
        self.assertEqual((self.loop(), self.tr.get_state("01")), ("", "ready"))

    def test_loop_only_skips_other_tickets(self):
        self.only("01")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(drive.loop(self.root, self.tr, "executor-a", only={7}), 0)
        self.assertEqual((self.tr.get_state("01"), self.calls("intake")), ("ready", 0))

    def test_claim_held_elsewhere_stops_without_running(self):
        self.tr.claim("01", "executor-b")
        with self.assertRaises(ClaimError):
            drive.run_ticket(self.root, "01", self.tr, "executor-a")
        self.assertEqual((self.stages, self.calls("intake")), ([], 0))


if __name__ == "__main__":
    unittest.main()

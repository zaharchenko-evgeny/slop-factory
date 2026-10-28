#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""End-to-end tests for `drive.py run` on the local-markdown tracker: stub `pi` and `gh`, a
temporary factory root and a local bare repository as `origin`. Run: uv run factory/tests/test_drive.py"""
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
import sm  # noqa: E402
from local_tracker import ClaimError, LocalTracker  # noqa: E402

# Replies with $STUB_DIR/reply-<mode>-<call> or reply-<mode> (mode = the input's first line); a
# missing file is empty output.
# The developer commits a file first. Counts calls per mode in $STUB_DIR/calls-<mode>, keeps the prompt.
AGENT = """#!/bin/bash
for a; do last=$a; done
if [ "$last" = "Reply with OK." ]; then echo OK; exit 0; fi
mode=$(printf '%s\\n' "$last" | sed -n '1s/^mode: //p')
echo x >> "$STUB_DIR/calls-$mode"
printf '%s' "$last" > "$STUB_DIR/prompt-$mode"
if [ "$mode" = implement ]; then echo hi > stub.txt; git add stub.txt; git commit -qm "#01 stub"; fi
n=$(wc -l < "$STUB_DIR/calls-$mode" | tr -d ' ')
if [ -f "$STUB_DIR/reply-$mode-$n" ]; then cat "$STUB_DIR/reply-$mode-$n"
elif [ -f "$STUB_DIR/reply-$mode" ]; then cat "$STUB_DIR/reply-$mode"; fi
"""
GH = """#!/bin/bash
printf '%s\\n' "$@" > "$STUB_DIR/gh-$1-$2"
case "$1 $2" in
  "pr list") echo '[]' ;;
  "pr create") echo "https://example.invalid/pull/7" ;;
  "pr view") echo '{"url":"https://example.invalid/pull/7","statusCheckRollup":[]}' ;;
  *) echo "stub gh: unexpected $*" >&2; exit 1 ;;
esac
"""
REPLIES = {
    "intake": "```text\nROUTER-INTAKE\nready: yes\nrisk: low\nnext: delivery\n```\n",
    "implement": "```text\nDEVELOPER\nstatus: done\ncommits:\n  - abc #01 stub\nblocked_reason: none\n```\n",
    "full": "```text\nREVIEWER\nverdict: approved\nreviewed_sha: x\nrisk: low\ndocs_impact: no\nblocking:\nnits:\n```\n",
    "summary": "```text\nROUTER-SUMMARY\ncomplete: yes\n```\n",
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
        shutil.copy(sm.ROOT / ".gitignore", self.root)
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t", "STUB_DIR": str(self.stub),
               "FACTORY_AGENT": str(self.stub / "agent"), "FACTORY_GH": str(self.stub / "gh")}
        for k, v in env.items():
            self.addCleanup(os.environ.pop, k, None)
        os.environ.update(env)
        git("init", "-q", "--bare", "-b", "main", str(tmp / "origin.git"), cwd=tmp)
        git("init", "-q", "-b", "main", cwd=self.root)
        git("add", ".", cwd=self.root)
        git("commit", "-qm", "init", cwd=self.root)
        git("remote", "add", "origin", str(tmp / "origin.git"), cwd=self.root)
        git("push", "-q", "origin", "main", cwd=self.root)
        for name, text in (("agent", AGENT), ("gh", GH)):
            (self.stub / name).write_text(text)
            (self.stub / name).chmod(0o755)
        for mode, text in REPLIES.items():
            (self.stub / f"reply-{mode}").write_text(text)
        self.tr = LocalTracker(Path(shutil.copytree(HERE / "fixtures/effort", tmp / "effort")))
        self.stages = []
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

    def test_delivers_ready_to_approved(self):
        t = self.go()
        self.assertEqual(self.stages, ["ready", "in-dev", "in-review", "approved"])
        self.assertEqual(LocalTracker(self.tr.issues.parent).get_state("01"), "approved")
        self.assertEqual(self.tr.read_vars("01"), sm.persisted(t))
        branch = "ez/01-driven-ticket"
        self.assertEqual(t["vars"]["branch"], branch)
        self.assertEqual(t["vars"]["pr_urls"], ["https://example.invalid/pull/7"])
        wt = self.root / ".factory/worktrees/01"
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True).stdout.strip()
        self.assertEqual(t["vars"]["reviewed_sha"], head)
        remote = subprocess.run(["git", "ls-remote", "--heads", "origin", branch], cwd=self.root,
                                capture_output=True, text=True).stdout
        self.assertIn(head, remote)  # pushed
        pr = (self.stub / "gh-pr-create").read_text()
        self.assertIn("Refs #01", pr)
        self.assertIn("Factory-Run: r1", pr)
        self.assertIn("- tool (advisory): skipped", pr)
        prompt = (self.stub / "prompt-implement").read_text()
        self.assertIn("mode: implement", prompt)
        self.assertIn("# Driven ticket", prompt)  # the local ticket file, not `gh issue view`

    def test_missing_tool_check_is_skipped(self):
        t = self.go()
        self.assertEqual(t["out"]["checks"]["checks"], [
            {"name": "has-file", "mode": "blocking", "status": "pass"},
            {"name": "tool", "mode": "advisory", "status": "skipped"}])
        self.assertIn("## tool (advisory): skipped", (self.root / ".factory/runs/01/checks-0.md").read_text())

    def test_empty_agent_output_runs_at_most_twice(self):
        (self.stub / "reply-implement").unlink()
        t = self.go()
        self.assertEqual((t["state"], self.calls("implement")), ("failed", 2))
        self.assertEqual(self.stages[-1], "failed")

    def test_unfenced_review_falls_back_and_approves(self):
        (self.stub / "reply-full-1").write_text("REVIEWER\nverdict: approved\n")  # no fence
        t = self.go()
        self.assertEqual((t["state"], self.calls("full")), ("approved", 2))  # one call per model
        config = yaml.safe_load((self.root / "factory.yaml").read_text())
        r = t["out"]["review_full"]
        self.assertEqual((r["result"], r["fallback_from"], r["model"]),
                         ("done", config["models"]["review"], config["models"]["cheap"]))
        self.assertIn("fell back from", (self.stub / "gh-pr-create").read_text())

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

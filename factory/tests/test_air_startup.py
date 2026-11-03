#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for .air/cloud/startup.sh with a temporary HOME and stubbed curl, git, uv.
Run: uv run factory/tests/test_air_startup.py"""
import gzip
import hashlib
import io
import os
import re
import sys
import tarfile
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config as cfg  # noqa: E402
cfg.set_team(Path(__file__).resolve().parent / "fixtures/team")

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".air/cloud/startup.sh"

STUBS = {
    "curl": 'echo curl >> "$LOG"\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\ncat "$STUB_TAR" > "$out"\n',
    "gh": "",
    "git": 'echo git "$@" >> "$LOG"\ncase "$*" in\n  "fetch --depth 1 origin "*) [ "${GIT_FAIL:-}" != fetch ] ;;\n  "clone --depth 1 "*) mkdir -p "$5"; echo "engine: ${TEAM_ENGINE:-v0.1.0}" > "$5/factory.descriptor.yaml" ;;\n  "worktree add --detach "*) [ "${GIT_FAIL:-}" != worktree-add ] ;;\n  *) exit 0 ;;\nesac\n',
    "uv": 'echo uv "$@" >> "$LOG"\nprintf "#!/bin/sh\\n" > "$HOME/.local/bin/decide"; chmod +x "$HOME/.local/bin/decide"\n',
}


class StartupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.home, self.repo, self.log = t / "home", t / "repo", t / "log"
        stubs = t / "stubs"
        for d in (self.home, self.repo, stubs):
            d.mkdir()
        self.log.touch()
        for name, body in STUBS.items():
            p = stubs / name
            p.write_text("#!/bin/sh\n" + body)
            p.chmod(0o755)
        # The script pins real SHA-256s; the stub serves its own file, so the pin is rewritten to match it.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo("conftest")
            info.mode, info.size = 0o755, 10
            tf.addfile(info, io.BytesIO(b"#!/bin/sh\n\n"[:10]))
        data = gzip.compress(buf.getvalue(), mtime=0)
        (t / "stub.tgz").write_bytes(data)
        stub_tar = hashlib.sha256(data).hexdigest()
        self.script = t / "startup.sh"
        self.script.write_text(re.sub(r"^CONFTEST_SHA=.*$", f"CONFTEST_SHA={stub_tar}", SCRIPT.read_text(), flags=re.M))
        self.env = {"HOME": str(self.home), "LOG": str(self.log), "STUB_TAR": str(t / "stub.tgz"), "PATH": f"{stubs}:/usr/bin:/bin"}

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self):
        return subprocess.run(["sh", str(self.script)], cwd=self.repo, env=self.env, capture_output=True, text=True)

    def test_idempotent(self):
        (self.repo.parent / "jbeval/.git").mkdir(parents=True)
        self.assertEqual(self.run_script().returncode, 0)
        first = self.log.read_text()
        self.assertIn("curl", first)
        self.assertIn("uv tool install", first)
        self.assertEqual(self.run_script().returncode, 0)
        self.assertGreater(self.log.read_text().count("uv tool install --force"), first.count("uv tool install --force"))
        self.assertEqual((self.home / ".bashrc").read_text().count(".local/bin"), 1)

    def test_installs_factory_from_main_worktree(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        log = self.log.read_text()
        self.assertIn('git fetch --depth 1 origin main', log)
        self.assertIn('git worktree add --detach ' + str(self.home / "factory-main") + ' FETCH_HEAD', log)
        self.assertIn('uv tool install --force ' + str(self.home / "factory-main"), log)

    def test_team_repository_is_cloned_and_names_the_engine_version(self):
        self.env["FACTORY_TEAM_REPOSITORY"] = "https://github.com/o/team"
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        team = self.home / "factory-team"
        log = self.log.read_text()
        self.assertIn(f"git clone --depth 1 https://github.com/o/team {team}", log)
        self.assertIn("git fetch --depth 1 origin refs/tags/v0.1.0", log)
        self.assertIn(f'export FACTORY_TEAM="{team}"', (self.home / ".bashrc").read_text())

    def test_non_v_engine_tag_is_used(self):
        self.env.update(FACTORY_TEAM_REPOSITORY="https://github.com/o/team", TEAM_ENGINE="release-2026.11")
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("git fetch --depth 1 origin refs/tags/release-2026.11", self.log.read_text())

    def test_additional_team_repository_is_used_without_clone(self):
        self.env["FACTORY_TEAM_REPOSITORY"] = "https://github.com/o/team"
        (self.repo.parent / "team/.git").mkdir(parents=True)
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("git clone", self.log.read_text())
        self.assertEqual((self.home / "factory-team").resolve(), (self.repo.parent / "team").resolve())

    def test_factory_install_failure_is_reported(self):
        uv = Path(self.env["PATH"].split(":", 1)[0]) / "uv"
        uv.write_text("#!/bin/sh\nexit 1\n")
        uv.chmod(0o755)
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("startup: factory install failed", r.stderr)

    def test_factory_fetch_failure_is_reported(self):
        self.env["GIT_FAIL"] = "fetch"
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("startup: factory install failed", r.stderr)
        self.assertNotIn("worktree add", self.log.read_text())

    def test_factory_worktree_add_failure_is_reported(self):
        self.env["GIT_FAIL"] = "worktree-add"
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("startup: factory install failed", r.stderr)
        self.assertNotIn("uv tool install --force", self.log.read_text())

    def test_checksum_mismatch_fails_closed(self):
        self.script.write_text(SCRIPT.read_text())  # the real pins: the stub's file does not match
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("checksum mismatch", r.stderr)
        self.assertFalse((self.home / ".local/bin/conftest").exists())

    def test_every_download_is_pinned(self):
        text = SCRIPT.read_text()
        self.assertEqual(len(re.findall(r"^\w+_SHA=[0-9a-f]{64}$", text, re.M)), 3)
        self.assertNotRegex(text, r"curl[^\n]*\|\s*(env [^\n]*)?sh")

    def test_no_jbeval(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("uv tool install --force", self.log.read_text())

    def test_light_env_skips_approver_tools(self):
        self.env["FACTORY_ENV"] = "light"
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("FACTORY_ENV=light", r.stdout)
        self.assertIn("uv tool install --force", self.log.read_text())

    def dry_run(self, env):
        r = subprocess.run(["sh", str(SCRIPT)], cwd=ROOT, capture_output=True, text=True,
                           env={"PATH": "/usr/bin:/bin", "HOME": str(self.home), "FACTORY_DRY": "1", "FACTORY_ENV": env})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_dry_run_read_installs_reviewer_skills_only(self):
        out = self.dry_run("read")
        self.assertIn("would install skills: ponytail-review to-tickets", out)
        for developer in ("tdd", "diagnosing-bugs", "shellcheck", "jactionlint"):
            self.assertNotIn(developer, out)
        self.assertFalse((self.home / ".claude").exists())

    def test_dry_run_write_installs_developer_tools(self):
        out = self.dry_run("write")
        self.assertIn("tdd", out)
        self.assertIn("shellcheck-py==0.10.0.1 jactionlint==2.3.0", out)
        self.assertNotIn("to-tickets", out)

    def test_every_node_role_is_installed_by_its_environment(self):
        graph = yaml.safe_load((ROOT / "workflows/nodes.yaml").read_text())["nodes"]
        for env in ("read", "write", "ops"):
            out = self.dry_run(env).splitlines()[0]
            for n in graph.values():
                if n.get("env") == env and n["role"] not in ("script", "human"):
                    self.assertIn(n["role"], out.split("roles=")[1].split(), (env, n["role"]))

    def test_path_like_skill_name_is_rejected_and_files_survive(self):
        self.env["FACTORY_ENV"] = "write"
        (self.repo / "roles/developer").mkdir(parents=True)
        (self.repo / "roles/developer/skills.txt").write_text("../victim\n")
        keep = self.home / ".agents/victim/keep"
        keep.parent.mkdir(parents=True)
        keep.write_text("x")
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("invalid skill name", r.stderr)
        self.assertTrue(keep.exists())

    def test_air_models_in_config(self):
        m = cfg.load()["targets"]["air"]["models"]
        self.assertEqual(set(m), {"cheap", "mid", "strong", "review", "review_strong"})
        self.assertNotEqual(m["review"].split("/")[0], m["mid"].split("/")[0])


if __name__ == "__main__":
    unittest.main()

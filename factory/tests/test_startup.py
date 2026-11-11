"""The team startup script run from a project checkout (a project is the environment's main repository)."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "skills/factory-setup/template/.air/cloud/startup.sh"
ENGINE = "git+https://example.com/engine@v1"


def executable(path: Path, body: str):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class StartupFromProjectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home, self.work, self.bin = self.tmp / "home", self.tmp / "workspaces", self.tmp / "bin"
        for d in (self.home, self.bin, self.work / "app", self.work / "team"):
            d.mkdir(parents=True)
        (self.work / "team/factory.yaml").write_text(f'name: T\nengine: "{ENGINE}"   # pinned\n')
        (self.work / "team/workflow.yaml").write_text("nodes: {}\n")
        self.log = self.tmp / "calls.log"
        executable(self.bin / "uv", f'echo "uv $*" >> {self.log}\n'
                   f'printf \'#!/bin/sh\\necho "factory $*" >> {self.log}\\n\' > "$HOME/.local/bin/factory"\n'
                   'chmod +x "$HOME/.local/bin/factory"\n')

    def run_script(self, cwd, **env):
        e = {"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), "FACTORY_ENV": "write",
             "FACTORY_WORKSPACES": str(self.work), **env}
        return subprocess.run(["sh", str(SCRIPT)], cwd=cwd, env=e, capture_output=True, text=True)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_installs_from_the_team_checkout_beside_the_project(self):
        run = self.run_script(self.work / "app")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(self.calls(), [f"uv tool install --quiet --force {ENGINE}", "factory setup --environment write"])
        self.assertEqual(os.path.realpath(self.home / "factory-team"), os.path.realpath(self.work / "team"))

    def test_no_team_checkout_fails_by_name(self):
        (self.work / "team/workflow.yaml").unlink()
        run = self.run_script(self.work / "app")
        self.assertEqual(run.returncode, 1)
        self.assertIn("no checkout with factory.yaml and workflow.yaml", run.stderr)
        self.assertEqual(self.calls(), [])

    def test_skip_flag_runs_once_per_pinned_engine(self):
        self.assertEqual(self.run_script(self.work / "app", FACTORY_SKIP_IF_INSTALLED="1").returncode, 0)
        first = self.calls()
        self.assertEqual(len(first), 2)
        run = self.run_script(self.work / "app", FACTORY_SKIP_IF_INSTALLED="1")
        self.assertEqual((run.returncode, self.calls()), (0, first))
        self.assertIn("skipped", run.stderr)
        # a new pin or a missing ~/factory-team installs again; no flag always installs
        (self.work / "team/factory.yaml").write_text('engine: "git+https://example.com/engine@v2"\n')
        self.run_script(self.work / "app", FACTORY_SKIP_IF_INSTALLED="1")
        self.assertEqual(len(self.calls()), 4)
        self.run_script(self.work / "app")
        self.assertEqual(len(self.calls()), 6)
        (self.home / "factory-team").unlink()
        self.run_script(self.work / "app", FACTORY_SKIP_IF_INSTALLED="1")
        self.assertEqual(len(self.calls()), 8)

    def test_failed_setup_leaves_no_skip_marker(self):
        executable(self.bin / "uv", f'echo "uv $*" >> {self.log}\n'
                   'printf \'#!/bin/sh\\nexit 1\\n\' > "$HOME/.local/bin/factory"\nchmod +x "$HOME/.local/bin/factory"\n')
        self.assertNotEqual(self.run_script(self.work / "app", FACTORY_SKIP_IF_INSTALLED="1").returncode, 0)
        self.run_script(self.work / "app", FACTORY_SKIP_IF_INSTALLED="1")
        self.assertEqual(len([c for c in self.calls() if c.startswith("uv")]), 2)


if __name__ == "__main__":
    unittest.main()

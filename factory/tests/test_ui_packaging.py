#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Check that wheel and source builds contain the progress UI without bundling node_modules."""
import shutil
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import cli  # noqa: E402


class ProgressUICLITest(unittest.TestCase):
    def test_ui_dispatches_remaining_args_after_team_selection(self):
        cli.load_engine(cli.HERE)
        server = types.ModuleType("factory_ui.server")
        server.main = mock.Mock(return_value=0)
        with mock.patch.dict(sys.modules, {"factory_ui.server": server}), \
                mock.patch.object(cli.config, "set_team") as set_team:
            result = cli.main(["ui", "--team", "/tmp/team", "--no-open", "--port", "8765"])

        self.assertEqual(result, 0)
        set_team.assert_called_once_with("/tmp/team")
        server.main.assert_called_once_with(["--no-open", "--port", "8765"])


@unittest.skipUnless(shutil.which("uv"), "uv builds the wheel and source distribution")
class ProgressUIPackagingTest(unittest.TestCase):
    def build(self, kind, output):
        subprocess.run(["uv", "build", kind, "--out-dir", str(output), str(ROOT)],
                       check=True, capture_output=True, text=True)

    def test_wheel_contains_runtime_package_and_built_browser(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            self.build("--wheel", output)
            wheel, = output.glob("*.whl")
            with zipfile.ZipFile(wheel) as archive:
                names = set(archive.namelist())

        self.assertIn("factory_ui/__init__.py", names)
        self.assertIn("factory_ui/static/index.html", names)
        self.assertTrue(any(name.endswith("share/factory-engine/docs/progress-ui.md") for name in names))
        self.assertTrue(any(name.startswith("factory_ui/static/assets/") and name.endswith(".js")
                            for name in names), "wheel must contain the built JavaScript bundle")

    def test_source_distribution_has_ui_sources_and_static_files_without_node_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            self.build("--sdist", output)
            sdist, = output.glob("*.tar.gz")
            with tarfile.open(sdist, "r:gz") as archive:
                names = {member.name for member in archive.getmembers()}

        self.assertTrue(any(name.endswith("/ui/package.json") for name in names))
        self.assertTrue(any(name.endswith("/ui/package-lock.json") for name in names))
        self.assertTrue(any(name.endswith("/factory/factory_ui/static/index.html") for name in names))
        self.assertTrue(any(name.endswith("/docs/progress-ui.md") for name in names))
        self.assertFalse(any("/ui/node_modules/" in f"/{name}/" for name in names))


if __name__ == "__main__":
    unittest.main()

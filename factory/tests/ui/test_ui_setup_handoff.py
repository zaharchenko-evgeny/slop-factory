"""`ui_setup_handoff`: the setup skill hands the operator the launch command; no setup script starts the server."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKILL = ROOT / "skills/factory-setup/SKILL.md"
SETUP_ROOTS = [ROOT / "skills/factory-setup/template", ROOT / "engine/template-v1", ROOT / "factory/tests/fixtures"]
LAUNCH = re.compile(r"\bfactory\s+ui\b")


def setup_scripts():
    """Every shell script of the local and Air setup: role and environment `setup.sh`, the Air `startup.sh`, helpers."""
    return sorted(p for root in SETUP_ROOTS for p in root.rglob("*.sh"))


class SetupHandoff(unittest.TestCase):
    def test_skill_hands_off_the_launch_command(self):
        text = SKILL.read_text()
        self.assertIn("factory ui --team", text)
        self.assertIn("Setup never starts the server", text)
        self.assertIn("127.0.0.1", text)
        self.assertIn("no Node", text)

    def test_handoff_is_a_step_of_the_setup_flow(self):
        text = SKILL.read_text()
        self.assertLess(text.index("## 4. Verify"), text.index("## 5. Hand off the progress UI"))

    def test_scripts_are_found(self):
        names = {p.name for p in setup_scripts()}
        self.assertTrue({"setup.sh", "startup.sh"} <= names, names)

    def test_no_setup_script_calls_factory_ui(self):
        for script in setup_scripts():
            with self.subTest(script=str(script.relative_to(ROOT))):
                self.assertIsNone(LAUNCH.search(script.read_text()))

    def test_setup_code_never_starts_the_server(self):
        for path in (ROOT / "factory/team_setup.py", ROOT / "factory/deploy.py", ROOT / "factory/factory_plugins/local/surface.py"):
            with self.subTest(path=path.name):
                text = path.read_text()
                self.assertNotIn("ui.server", text)
                self.assertNotRegex(text, r"""["']ui["']\s*[,\]]""")


if __name__ == "__main__":
    unittest.main()

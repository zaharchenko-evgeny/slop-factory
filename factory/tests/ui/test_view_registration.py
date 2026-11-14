"""A third view registers without edits to the built-in views or the tracker adapters (both sides of the UI)."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import snapshot_support as s  # noqa: E402
import ui_support as u  # noqa: E402
from ui import snapshot, views  # noqa: E402


class ThirdView(unittest.TestCase):
    def test_python_registry_serves_a_third_view_over_a_snapshot(self):
        registry = views.builtin_registry()
        registry.register(views.ViewSpec("by-stage", "By stage", lambda snap: {t["key"]: t["stage"] for t in snap["tickets"]}))
        tracker = s.FakeTracker({1: {"facts": s.facts(), "events": []}})
        with s.team("slop") as (config, nodes):
            snap = snapshot.build(config, nodes, [s.source(tracker)], s.NOW)
        self.assertEqual(registry.ids(), ["workflow", "columns", "by-stage"])
        self.assertEqual(registry.project("by-stage", snap), {"github-issues|o/r#1": "ready"})

    def test_ui_views_import_only_the_snapshot_types_and_the_registry(self):
        allowed = {"react", "@/snapshot", "./registry", "./WorkflowFlow", "@/components/ui/badge"}  # no fetch, no tracker adapter
        for path in sorted((u.UI / "src" / "views").glob("*.ts*")):
            imports = set(re.findall(r'^import .* from "([^"]+)"', path.read_text(), re.M))
            with self.subTest(path.name):
                self.assertLessEqual(imports, allowed, "a view reads the snapshot only")

    @unittest.skipUnless(u.NPM, "npm is required to run the UI component tests")
    def test_ui_registry_accepts_a_third_view(self):
        run = u.npm("test", "--", "ui_builtin_views_and_registration")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("1 passed", run.stdout)


if __name__ == "__main__":
    unittest.main()

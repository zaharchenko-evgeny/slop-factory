"""View registration: a third view registers through the public interface alone."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import snapshot_support as s  # noqa: E402
from ui import snapshot, views  # noqa: E402


class Registration(unittest.TestCase):
    def test_a_third_view_needs_no_edit_of_the_built_ins_or_adapters(self):
        registry = views.builtin_registry()
        self.assertEqual((registry.ids(), registry.default()), (["workflow", "columns"], "workflow"))
        registry.register(views.ViewSpec("by-repo", "By repository", lambda snap: {
            r: [t["key"] for t in snap["tickets"] if t["repository"] == r] for r in {t["repository"] for t in snap["tickets"]}}))
        tracker = s.FakeTracker({1: {"facts": s.facts(), "events": []}})
        with s.team("slop") as (config, nodes):
            snap = snapshot.build(config, nodes, [s.source(tracker)], s.NOW)
        self.assertEqual(registry.project("by-repo", snap), {"o/r": ["github-issues|o/r#1"]})
        self.assertEqual([v["id"] for v in registry.describe()], ["workflow", "columns", "by-repo"])

    def test_bad_registrations_are_refused(self):
        registry = views.builtin_registry()
        project = lambda snap: {}  # noqa: E731
        for spec in (views.ViewSpec("workflow", "dup", project), views.ViewSpec("Bad Id", "x", project),
                     views.ViewSpec("second", "x", project, default=True), views.ViewSpec("nocall", "x", None)):
            with self.subTest(spec.id), self.assertRaises(ValueError):
                registry.register(spec)
        with self.assertRaises(KeyError):
            registry.project("missing", {})


if __name__ == "__main__":
    unittest.main()

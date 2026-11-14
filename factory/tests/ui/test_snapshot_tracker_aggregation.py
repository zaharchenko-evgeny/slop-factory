"""`ui_tracker_aggregation`: one card per (tracker instance, ticket) across overlapping environments."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import snapshot_support as s  # noqa: E402
from ui import snapshot, sources, views  # noqa: E402

GH, GH2 = ("github-issues", "o/r"), ("github-issues", "o/other")
YT, YT2 = ("youtrack", "https://yt.one", "PROJ"), ("youtrack", "https://yt.two", "PROJ")


def tracker(*ids, stage="ready", **kw):
    return s.FakeTracker({i: {"facts": s.facts(stage=stage, title=f"t{i}"), "events": []} for i in ids}, **kw)


class Aggregation(unittest.TestCase):
    def build(self, srcs):
        with s.team("slop") as (config, nodes):
            return snapshot.build(config, nodes, srcs, s.NOW)

    def cards(self, snap):
        return {t["key"]: t for t in snap["tickets"]}

    def test_overlapping_environments_give_one_card_with_every_environment(self):
        a, b = tracker(1, 2), tracker(2, 3)
        snap = self.build([s.source(a, GH, ("engine",)), s.source(b, GH, ("team", "intake"))])
        cards = self.cards(snap)
        self.assertEqual(sorted(t["id"] for t in snap["tickets"]), ["1", "2", "3"])
        self.assertEqual(cards["github-issues|o/r#2"]["environments"], ["engine", "intake", "team"])
        self.assertEqual(cards["github-issues|o/r#1"]["environments"], ["engine"])

    def test_equal_short_ids_on_different_tracker_instances_stay_apart(self):
        srcs = [s.source(tracker(7), GH, ("a",), "o/r"), s.source(tracker(7), GH2, ("b",), "o/other"),
                s.source(tracker("PROJ-7"), YT, ("a",), None), s.source(tracker("PROJ-7"), YT2, ("b",), None)]
        snap = self.build(srcs)
        self.assertEqual(len(snap["tickets"]), 4)
        self.assertEqual(len({t["key"] for t in snap["tickets"]}), 4)
        self.assertEqual(sorted((t["tracker"], t["short_id"]) for t in snap["tickets"]),
                         sorted([("github-issues|o/r", 7), ("github-issues|o/other", 7),
                                 ("youtrack|https://yt.one|PROJ", "PROJ-7"), ("youtrack|https://yt.two|PROJ", "PROJ-7")]))

    def test_github_and_youtrack_tickets_carry_repository_and_links(self):
        snap = self.build([s.source(tracker(5), GH, ("a",), "o/r"), s.source(tracker("PROJ-1"), YT, ("a",), None)])
        gh, yt = sorted(snap["tickets"], key=lambda t: t["tracker"])
        self.assertEqual((gh["repository"], gh["links"]["ticket"]), ("o/r", "https://github.com/o/r/issues/5"))
        self.assertEqual(yt["links"]["ticket"], None)
        self.assertIn("repository", yt["missing"])  # a missing field is named, not hidden

    def test_merge_keeps_the_first_source_and_unions_environments(self):
        one, two = s.source(tracker(), GH, ("x",)), s.source(tracker(), GH, ("y",))
        merged = sources.merge([(one, [1, 2]), (two, [2])])
        self.assertEqual([(src.environments, t, envs) for src, t, envs in merged], [(["x"], 1, ["x"]), (["x"], 2, ["x", "y"])])

    def test_a_failing_source_leaves_the_others_and_is_reported(self):
        snap = self.build([s.source(tracker(1, fail="boom"), GH, ("a",)), s.source(tracker(9), GH2, ("b",), "o/other")])
        self.assertEqual([t["id"] for t in snap["tickets"]], ["9"])
        self.assertEqual([(e["source"], "boom" in e["message"]) for e in snap["errors"]], [("github-issues|o/r", True)])
        self.assertEqual([(x["id"], x["ok"]) for x in snap["sources"]], [("github-issues|o/r", False), ("github-issues|o/other", True)])

    def test_a_facts_failure_keeps_the_card_with_unknown_fields(self):
        t = tracker(4)
        t.facts = lambda ticket: (_ for _ in ()).throw(RuntimeError("api down"))
        snap = self.build([s.source(t, GH, ("a",))])
        self.assertEqual(snap["tickets"][0]["facts_error"], "facts unavailable")
        self.assertEqual(snap["tickets"][0]["nodes"], [])
        self.assertTrue(any("api down" in e["message"] for e in snap["errors"]))

    def test_views_read_the_one_snapshot(self):
        snap = self.build([s.source(tracker(1, 2), GH, ("a",))])
        registry = views.builtin_registry()
        flow, columns = registry.project("workflow", snap), registry.project("columns", snap)
        keys = sorted(k for ks in flow["tickets_by_stage"].values() for k in ks)
        self.assertEqual(keys, sorted(k for c in columns["columns"] for k in c["tickets"]))
        self.assertEqual(keys, sorted(t["key"] for t in snap["tickets"]))


if __name__ == "__main__":
    unittest.main()

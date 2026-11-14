"""`ui_stage_age_boundaries`: warn only after MORE than one hour in an active stage."""
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import snapshot_support as s  # noqa: E402
from ui import age, snapshot  # noqa: E402

STAGES = {"ready": "f:ready", "in-dev": "f:in-dev", "blocked": "f:blocked", "done": "f:done"}


def ago(**kw):
    return (s.NOW - timedelta(**kw)).isoformat()


class StageAge(unittest.TestCase):
    def age(self, kind, entered):
        return age.stage_age("x", kind, entered, s.NOW)

    def test_boundary_table(self):
        rows = [("active", ago(minutes=59, seconds=59), "ok", False), ("active", ago(hours=1), "ok", False),
                ("active", ago(hours=1, seconds=1), "warn", True), ("active", ago(days=3), "warn", True),
                ("active", ago(seconds=-30), "ok", False),  # a clock-skewed future entry is age 0
                ("waiting", ago(days=3), "exempt", False), ("final", ago(days=30), "exempt", False),
                ("active", None, "unknown", False), ("active", "", "unknown", False), ("active", "not a time", "unknown", False),
                (None, ago(days=1), "unknown", False), ("waiting", None, "exempt", False)]
        for kind, entered, state, warn in rows:
            with self.subTest(kind=kind, entered=entered):
                got = self.age(kind, entered)
                self.assertEqual((got["state"], got["warn"]), (state, warn))
        self.assertEqual(self.age("active", ago(hours=1))["seconds"], 3600)
        self.assertEqual(self.age("active", ago(hours=2).replace("+00:00", "Z"))["state"], "warn")  # a Z suffix parses

    def test_entry_time_is_the_latest_stage_label_not_comments_or_updated_at(self):
        events = [s.labeled("f:in-dev", ago(hours=5)), {"kind": "commented", "value": None, "at": ago(minutes=1)}]
        self.assertEqual(age.entered_at(events, STAGES, "in-dev"), ago(hours=5))
        events += [{"kind": "unlabeled", "value": "f:in-dev", "at": ago(hours=4)}, s.labeled("f:in-dev", ago(minutes=10))]
        self.assertEqual(age.entered_at(events, STAGES, "in-dev"), ago(minutes=10))  # re-entry resets
        self.assertIsNone(age.entered_at([s.labeled("f:in-dev", ago(hours=5)), {"kind": "unlabeled", "value": "f:in-dev", "at": ago(hours=1)}], STAGES, "in-dev"))
        self.assertIsNone(age.entered_at([], STAGES, "in-dev"))
        self.assertIsNone(age.entered_at([s.labeled("f:in-dev", None)], STAGES, "in-dev"))
        self.assertEqual([e["stage"] for e in age.stage_entries(events, STAGES)], ["in-dev", "in-dev"])

    def card(self, stage, events, updated_at=None):
        tracker = s.FakeTracker({1: {"facts": s.facts(stage=stage, updated_at=updated_at), "events": events}})
        with s.team("slop") as (config, nodes):
            self.stages = config["tracker"]["stages"]
            return snapshot.build(config, nodes, [s.source(tracker)], s.NOW)["tickets"][0]

    def test_snapshot_cards_use_the_stage_kind_of_the_factory(self):
        with s.team("slop") as (config, _):
            stages, kinds = config["tracker"]["stages"], config["tracker"]["stage_kinds"]
        active = next(k for k, v in kinds.items() if v == "active")
        waiting, final = (next(k for k, v in kinds.items() if v == x) for x in ("waiting", "final"))
        old = [s.labeled(stages[active], ago(hours=2))]
        warm = self.card(active, old, updated_at=ago(minutes=1))  # a fresh comment does not reset the age
        self.assertEqual((warm["stage_age"]["state"], [w["kind"] for w in warm["warnings"]]), ("warn", ["stage-age"]))
        self.assertEqual(warm["warnings"][0]["level"], "amber")
        fresh = self.card(active, [s.labeled(stages[active], ago(minutes=30))])
        self.assertEqual((fresh["stage_age"]["state"], fresh["warnings"]), ("ok", []))
        self.assertEqual(self.card(active, [])["stage_age"]["state"], "unknown")
        self.assertIn("stage_entered_at", self.card(active, [])["missing"])
        for stage in (waiting, final):
            c = self.card(stage, [s.labeled(stages[stage], ago(days=9))])
            self.assertEqual((c["stage_age"]["state"], c["warnings"]), ("exempt", []))
        self.assertEqual(self.card(active, old + [{"kind": "unlabeled", "value": stages[active], "at": ago(hours=1)},
                                                  s.labeled(stages[active], ago(minutes=5))])["stage_age"]["state"], "ok")
        self.assertEqual([h["stage"] for h in self.card(active, old)["history"]], [active])


if __name__ == "__main__":
    unittest.main()

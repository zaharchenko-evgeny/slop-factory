"""`ui_human_review_attention`, snapshot side: review evidence versus the generic marker, reason, reviewer, source."""
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import snapshot_support as s  # noqa: E402
from ui import review, snapshot  # noqa: E402

MARKER = "factory:needs-human"
APPROVED = dict(stage="approved", pr=9, head="h", pr_state="open", ci="green", round_complete=True,
                round_verdict="approve", round_head="h", risk="high")


def config(auto_merge=None, **kw):
    return {"tracker": {"needs_human_label": MARKER}, "gates": {"auto_merge": auto_merge} if auto_merge is not None else {}, **kw}


class Attention(unittest.TestCase):
    def test_table(self):
        agent_review = {"agent": True, "verdict": "approve", "commit_id": "h", "author": "bot"}
        rows = [  # name, facts, config, kind, label, reason, reviewer, source kinds
            ("requested reviewer, no marker, any stage", s.facts(stage="in-dev", pr=3, requested_reviewers=["ann"]), config(), "review", "Needs human review", "review requested", "ann", ["requested-review"]),
            ("requested reviewer and marker", s.facts(requested_reviewers=["ann", "bo"], attrs=[MARKER]), config(), "review", "Needs human review", "review requested", "ann, bo", ["requested-review", "marker"]),
            ("bot reviewer is not a person", s.facts(requested_reviewers=["copilot[bot]"]), config(), None, None, None, None, []),
            ("manual gate unresolved: auto-merge off", s.facts(**APPROVED), config({"enabled": False, "max_risk": "high"}, owner="boss"), "review", "Needs human review", "manual approval gate", "boss", ["manual-gate"]),
            ("manual gate unresolved: risk above limit", s.facts(**APPROVED), config({"enabled": True, "max_risk": "low"}), "review", "Needs human review", "manual approval gate", None, ["manual-gate"]),
            ("auto-merge resolves the gate", s.facts(**APPROVED), config({"enabled": True, "max_risk": "high"}), None, None, None, None, []),
            ("marker alone: question", s.facts(stage="needs-answers", attrs=[MARKER]), config(), "attention", "Needs human attention", "question", None, ["marker"]),
            ("marker alone: unblocking", s.facts(stage="blocked", pr_labels=[MARKER]), config(), "attention", "Needs human attention", "unblocking", None, ["marker"]),
            ("marker alone: unknown reason", s.facts(stage="in-dev", attrs=[MARKER]), config(), "attention", "Needs human attention", "unknown", None, ["marker"]),
            ("agent review and stage alone", s.facts(stage="in-review", reviews=[agent_review], pr=1, head="h"), config(), None, None, None, None, []),
            ("a custom marker name", s.facts(stage="in-dev", attrs=["ops:attend"]), {"tracker": {"needs_human_label": "ops:attend"}}, "attention", "Needs human attention", "unknown", None, ["marker"]),
            ("no marker configured", s.facts(attrs=[MARKER]), {"tracker": {}}, None, None, None, None, []),
            ("empty and missing facts", s.facts(requested_reviewers=[]), config(), None, None, None, None, []),
        ]
        for name, facts, cfg, kind, label, reason, reviewer, kinds in rows:
            with self.subTest(name):
                got = review.attention(facts, cfg)
                self.assertEqual((got["kind"], got["label"], got["reason"], got["reviewer"], [x["kind"] for x in got["sources"]]),
                                 (kind, label, reason, reviewer, kinds))

    def test_missing_values_are_listed(self):
        got = review.attention(s.facts(stage="in-dev", attrs=[MARKER]), config())
        self.assertEqual(sorted(got["unknown"]), ["reason", "reviewer"])
        self.assertEqual(review.attention(s.facts(requested_reviewers=["ann"]), config())["unknown"], [])
        self.assertEqual(review.attention(s.facts(stage="blocked", attrs=[MARKER]), config())["unknown"], ["reviewer"])
        self.assertTrue(all(x["evidence"] for x in got["sources"]))

    def test_an_incomplete_fact_set_does_not_raise(self):
        self.assertIsNone(review.attention({"stage": "approved"}, config({"enabled": False}))["kind"])


class InSnapshot(unittest.TestCase):
    def test_review_badge_and_age_warning_are_independent(self):
        with s.team("slop") as (cfg_, nodes):
            stages, kinds = cfg_["tracker"]["stages"], cfg_["tracker"]["stage_kinds"]
            active = next(k for k, v in kinds.items() if v == "active")
            old, fresh = (s.NOW - timedelta(hours=3)).isoformat(), (s.NOW - timedelta(minutes=5)).isoformat()
            tickets = {1: ("f1", s.facts(stage=active, pr=2, requested_reviewers=["ann"]), old),
                       2: ("f2", s.facts(stage=active, pr=3, requested_reviewers=["ann"]), fresh),
                       3: ("f3", s.facts(stage=active, attrs=[cfg_["tracker"]["needs_human_label"]]), old),
                       4: ("f4", s.facts(stage=active), old)}
            tracker = s.FakeTracker({k: {"facts": f, "events": [s.labeled(stages[active], at)]} for k, (_, f, at) in tickets.items()})
            snap = snapshot.build(cfg_, nodes, [s.source(tracker)], s.NOW)
        by_id = {t["id"]: t for t in snap["tickets"]}
        self.assertEqual({k: (v["human"]["kind"], bool(v["warnings"])) for k, v in by_id.items()},
                         {"1": ("review", True), "2": ("review", False), "3": ("attention", True), "4": (None, True)})
        self.assertEqual(snap["attention"], {"human_review": 2, "human_attention": 1})
        self.assertEqual(by_id["2"]["human"]["reviewer"], "ann")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/gate_space.py with a fake Space API. Run: uv run factory/tests/test_gate_space.py"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gate_port import PR_DEFAULTS  # noqa: E402
from gate_space import SpaceCodeHost  # noqa: E402


def mr(number, branch, **kw):
    return {"number": number, "title": f"t{number}", "state": "open", "branch": branch, "head": f"sha{number}",
            "ci": "none", "labels": [], "reviews": [], **kw}


class FakeSpace:
    def __init__(self, *mrs):
        self.mrs = list(mrs)

    def __call__(self, path):
        if path.endswith("/code-reviews"):
            return {"data": self.mrs}
        number = int(path.rsplit("number:", 1)[1])
        return next(m for m in self.mrs if m["number"] == number)


class SpaceFacts(unittest.TestCase):
    def host(self, *mrs):
        self.space = FakeSpace(*mrs)
        return SpaceCodeHost("IJ", self.space)

    def test_empty(self):
        h = self.host(mr(1, "ez/9-other"))
        self.assertIsNone(h.merge_request(5))
        self.assertEqual(h.facts(5), PR_DEFAULTS)

    def test_open_and_lookup(self):
        h = self.host(mr(1, "ez/9-other"), mr(2, "ez/5-thing", labels=["factory:ci-green"]))
        f = h.facts(5)
        self.assertEqual((f["pr"], f["head"], f["pr_state"], f["branch"], f["pr_labels"]), (2, "sha2", "open", "ez/5-thing", ["factory:ci-green"]))
        self.assertEqual(h.ticket_for_pr(2), 5)

    def test_ticket_from_title(self):
        h = self.host(mr(3, "feature", title="5 Do thing"))
        self.assertEqual((h.ticket_for_pr(3), h.facts(5)["pr"]), (5, 3))

    def test_merged_and_open_preferred(self):
        h = self.host(mr(1, "ez/5-a", state="merged"))
        self.assertEqual(h.facts(5)["pr_state"], "merged")
        self.space.mrs.append(mr(2, "ez/5-b", state="closed"))
        self.assertEqual(h.facts(5)["pr"], 2)  # latest closed beats older merged
        self.space.mrs.append(mr(0, "ez/5-c"))
        self.assertEqual((h.facts(5)["pr"], h.facts(5)["pr_state"]), (0, "open"))

    def test_changed_head(self):
        h = self.host(mr(2, "ez/5-a"))
        before = h.facts(5)["head"]
        self.space.mrs[0]["head"] = "newsha"
        self.assertEqual((before, h.facts(5)["head"]), ("sha2", "newsha"))

    def test_ci(self):
        got = [SpaceCodeHost("IJ", FakeSpace(mr(2, "ez/5-a", ci=c))).facts(5)["ci"] for c in ("success", "failed", "running", "none", "weird")]
        self.assertEqual(got, ["green", "red", "pending", "none", "none"])

    def test_reviews(self):
        r = {"author": "bob", "verdict": "changes", "commit_id": "sha2", "submitted_at": "2026-01-01T00:00:00Z", "extra": 1}
        f = self.host(mr(2, "ez/5-a", reviews=[r])).facts(5)
        self.assertEqual(f["reviews"], [{k: r[k] for k in ("author", "verdict", "commit_id", "submitted_at")}])


if __name__ == "__main__":
    unittest.main()

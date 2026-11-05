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
from gate_space import SpaceCodeHost, space_event  # noqa: E402
import gate  # noqa: E402


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


class SpaceEvents(unittest.TestCase):
    def payload(self, **mr):
        return {"eventType": "space.merge_request.opened", "data": {"action": "opened", "project": {"key": "IJ"},
                "merge_request": {"number": 7, "title": "t", "branch": "ez/5-thing", **mr}}}

    def test_event_resolves_ticket(self):
        self.assertEqual(space_event(self.payload()), {"number": 7, "action": "opened", "project": "IJ", "ticket": 5})
        self.assertEqual(gate.from_trigger(self.payload()),
                         {"id": "merge_request:5:opened", "ticket": 5, "event_name": "pull_request", "action": "opened"})

    def test_event_falls_back_to_title_then_host(self):
        self.assertEqual(gate.from_trigger(self.payload(branch="x", title="6 Fix"))["ticket"], 6)
        host = SpaceCodeHost("IJ", FakeSpace(mr(7, "ez/8-a")))
        self.assertEqual(gate.from_trigger(self.payload(branch="x"), host)["ticket"], 8)

    def test_bad_events(self):
        with self.assertRaises(gate.NoTicket):
            gate.from_trigger(self.payload(branch="x"))
        with self.assertRaises(ValueError):
            space_event({"eventType": "space.merge_request.opened", "data": {}})

    def test_dry_run_and_review_roles_in_facts(self):
        r = {"author": "bob", "verdict": "approve", "commit_id": "sha2", "role": "qa"}
        f = SpaceCodeHost("IJ", FakeSpace(mr(2, "ez/5-a", ci="success", dry_run={"state": "failed"}, reviews=[r]))).facts(5)
        self.assertEqual((f["ci"], f["dry_run"], f["reviews"][0]["role"]), ("red", "failed", "qa"))
        self.assertIsNone(SpaceCodeHost("IJ", FakeSpace(mr(2, "ez/5-a", ci="success"))).facts(5)["dry_run"])


class SpaceGateFacts(unittest.TestCase):
    def test_check_reads_space_ci_and_roles_but_keeps_tracker_facts(self):
        from gate_port import MemoryTracker
        r = {"author": "bob", "verdict": "approve", "commit_id": "sha5", "role": "qa"}
        host = SpaceCodeHost("IJ", FakeSpace(mr(5, "ez/5-a", ci="success", dry_run={"state": "failed"}, reviews=[r])))
        tracker = MemoryTracker({"5": {"stage": "changes-requested", "head": "gh", "ci": "green", "pr": 9}})
        _, out = gate.check("fix", {"id": "e", "ticket": 5}, tracker, gate.nodes_mod.load(), gate.config_mod.load(), host)
        c = out["context"]
        self.assertEqual((c["ci"], c["dry_run"], c["head"], c["pr"], c["stage"]), ("red", "failed", "sha5", 5, "changes-requested"))
        self.assertEqual(c["reviews"][0]["role"], "qa")


class SpaceWrites(unittest.TestCase):
    def setUp(self):
        self.calls, self.fail = [], None
        self.space = FakeSpace(mr(2, "ez/5-a", labels=["a", "x-1", "x-2"]))
        self.host = SpaceCodeHost("IJ", self.space, self.write)

    def write(self, method, path, body):
        if self.fail:
            raise OSError(self.fail)
        self.calls.append((method, path.split("number:2")[1], body))
        return {}

    def apply(self, kind, value=None, **kw):
        return self.host.apply({"ticket": 5, "kind": kind, "value": value, **kw})

    def test_review_once_per_head(self):
        e = dict(focus="behaviour", summary="s", findings=[{"id": "B1", "file": "f:1", "finding": "bad"}])
        self.assertTrue(self.apply("review", "changes", **e))
        method, path, body = self.calls[0]
        self.assertEqual((method, path, body["commitId"], body["verdict"]), ("POST", "/reviews", "sha2", "changes"))
        self.assertIn("**B1** f:1", body["body"])
        self.space.mrs[0]["reviews"] = [{"commit_id": "sha2", "body": body["body"]}]
        self.assertFalse(self.apply("review", "changes", **e))
        self.assertEqual(len(self.calls), 1)

    def test_labels(self):
        self.assertTrue(self.apply("label", "new"))
        self.assertFalse(self.apply("label", "a"))
        self.assertTrue(self.apply("unlabel", "x-*"))
        self.assertFalse(self.apply("unlabel", "absent"))
        self.assertEqual([(m, p) for m, p, _ in self.calls], [("POST", "/labels"), ("DELETE", "/labels/x-1"), ("DELETE", "/labels/x-2")])

    def test_update_and_merge_pin_head(self):
        self.assertTrue(self.apply("update-branch", head="sha2"))
        self.assertTrue(self.apply("merge", head="sha2"))
        self.assertEqual([(p, b["expectedHead"]) for _, p, b in self.calls], [("/update-branch", "sha2"), ("/merge", "sha2")])
        self.assertFalse(self.apply("merge", head="old"))  # head moved: nothing written
        self.assertEqual(len(self.calls), 2)

    def test_merged_is_noop_and_no_mr_fails(self):
        self.space.mrs[0]["state"] = "merged"
        self.assertFalse(self.apply("merge", head="sha2"))
        with self.assertRaisesRegex(RuntimeError, "label: no open merge request"):
            self.apply("label", "a")
        self.assertEqual(self.calls, [])

    def test_api_error(self):
        self.fail = "HTTP 500"
        for kind in ("label", "update-branch", "merge", "review"):
            with self.assertRaisesRegex(RuntimeError, f"{kind}: HTTP 500"):
                self.apply(kind, "x", focus="f")

    def test_unknown_effect(self):
        with self.assertRaises(ValueError):
            self.apply("explode")


if __name__ == "__main__":
    unittest.main()

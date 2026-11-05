#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/youtrack_tracker.py with a stubbed runner. Run: uv run factory/tests/test_youtrack_tracker.py"""
import copy
import json
import sys
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_gate_github as contract  # noqa: E402
import youtrack_tracker as yt  # noqa: E402

FIXTURES = HERE / "fixtures/youtrack"
ISSUE = json.loads((FIXTURES / "issue.json").read_text())
ACTIVITIES = json.loads((FIXTURES / "activities.json").read_text())


def ms(at):
    return int(datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp() * 1000)


class FakeYouTrack:
    """Enough of the REST API for the store, over one issue `T-1` (plus its blockers); records each request."""

    def __init__(self, issue=None, activities=(), comments=(), tags=("docs", "factory:approved", "factory:in-review", "risk:low")):
        self.calls, self.activities, self.comments = [], list(activities), list(comments)
        self.issue = copy.deepcopy(issue or ISSUE)
        self.tags = {t: f"id:{t}" for t in tags}

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        url, q = urlsplit(path), parse_qs(urlsplit(path).query)
        parts = url.path.split("/")[2:]
        if parts[0] == "tags" and len(parts) == 1:
            return [{"id": i, "name": t} for t, i in self.tags.items() if q["query"][0] in t]
        if parts[0] != "issues":
            raise AssertionError(path)
        if len(parts) == 1:
            return []
        if len(parts) == 2 and method == "GET":
            return copy.deepcopy(self.issue)
        if len(parts) == 2:  # custom field update
            for f in body["customFields"]:
                self.issue["customFields"] = [x for x in self.issue["customFields"] if x["name"] != f["name"]]
                self.issue["customFields"].append({"name": f["name"], "value": f["value"]})
                if f["name"] == "State" and f["value"] and f["value"]["name"] == "Done":
                    self.issue["resolved"] = 1
            return {}
        if parts[2] == "activities":
            return self.activities
        if parts[2] == "comments":
            if method == "POST":
                self.comments.append({"text": body["text"], "created": 2, "author": {"login": "factory"}})
                return {}
            return self.comments
        if parts[2] == "tags":
            if method == "POST":
                name = next(t for t, i in self.tags.items() if i == body["id"])
                self.issue["tags"].append({"id": body["id"], "name": name})
            else:
                self.issue["tags"] = [t for t in self.issue["tags"] if t["id"] != parts[3]]
            return {}
        raise AssertionError(path)


class StoreTest(unittest.TestCase):
    def test_ticket_store_contract(self):
        def make(attrs, body, assignees, comments, events):
            issue = {"idReadable": "T-1", "summary": "t", "description": body, "resolved": None, "updated": 1,
                     "reporter": {"login": "author"}, "tags": [{"id": f"id:{a}", "name": a} for a in attrs],
                     "customFields": [{"name": "Assignee", "value": {"login": assignees[0]}}],
                     "links": [{"direction": "INWARD", "linkType": {"name": "Depend"}, "issues": [
                         {"idReadable": "T-2", "resolved": None}, {"idReadable": "T-3", "resolved": 1}]}]}
            acts = [{"timestamp": ms(e["at"]), "author": {"login": e["actor"]},
                     "category": {"id": "IssueResolvedCategory" if e["kind"] == "closed" else "TagsCategory"},
                     **({"added": True} if e["kind"] == "closed" else {"added": [{"name": e["value"]}], "removed": []})}
                    for e in events]
            cs = [{"text": c["body"], "created": ms(c["at"]), "author": {"login": c["author"]}} for c in comments]
            fake = FakeYouTrack(issue, acts, cs)
            return yt.YouTrackTicketStore(fake, bots=["ci[bot]"], writers=["alice"]), "T-1"

        contract.ticket_store_contract(self, make)

    def test_events_map_tags_state_field_and_resolution_from_a_recorded_fixture(self):
        store = yt.YouTrackTicketStore(FakeYouTrack(activities=ACTIVITIES), state_field="State")
        got = [(e["kind"], e["value"], e["actor"]) for e in store.events("LME-1701")]
        self.assertEqual(got, [
            ("labeled", "factory:ready", "alice"), ("unlabeled", "factory:ready", "alice"),
            ("labeled", "factory:in-dev", "alice"), ("unlabeled", "Open", "bob"), ("labeled", "In Progress", "bob"),
            ("closed", None, "bob"), ("reopened", None, "alice")])
        self.assertEqual(store.events("LME-1701")[0]["at"], "2026-01-01T00:00:00Z")

    def test_links_read_from_a_recorded_fixture(self):
        store = yt.YouTrackTicketStore(FakeYouTrack())
        self.assertEqual(store.open_blockers("LME-1701"), ["LME-1700"])
        self.assertEqual(store.blocking("LME-1701"), [{"number": "LME-1702", "state": "open"}])
        self.assertEqual(store.parent("LME-1701"), "LME-1600")
        self.assertEqual(store.children("LME-1701"), [{"number": "LME-1703", "state": "closed", "not_planned": False,
                                                       "attrs": ["factory:done"]}])
        self.assertEqual(store.issue("LME-1701")["attrs"], ["factory:in-dev", "risk:low"])

    def test_request_shapes(self):
        fake = FakeYouTrack()
        store = yt.YouTrackTicketStore(fake, state_field="State", done_state="Done")
        calls = lambda: [(m, urlsplit(p).path, b) for m, p, b in fake.calls if m != "GET"]
        store.add_attr("LME-1701", "docs")
        store.remove_attr("LME-1701", "docs")
        store.comment("LME-1701", "hi")
        store.set_stage("LME-1701", "Review", ["In Progress"])
        store.close("LME-1701")
        self.assertEqual(calls(), [
            ("POST", "/api/issues/LME-1701/tags", {"id": "id:docs"}),
            ("DELETE", "/api/issues/LME-1701/tags/id:docs", None),
            ("POST", "/api/issues/LME-1701/comments", {"text": "hi"}),
            ("POST", "/api/issues/LME-1701", {"customFields": [{"name": "State", "$type": "StateIssueCustomField", "value": {"name": "Review"}}]}),
            ("POST", "/api/issues/LME-1701", {"customFields": [{"name": "State", "$type": "StateIssueCustomField", "value": {"name": "Done"}}]})])
        gets = [p for m, p, _ in fake.calls if m == "GET" and "/activities" not in p]
        self.assertTrue(all("fields=" in p for p in gets), gets)
        self.assertEqual(store.issue("LME-1701")["state"], "closed")

    def test_tag_stage_replaces_the_other_stage_tags_and_a_missing_tag_is_an_error(self):
        fake = FakeYouTrack()
        store = yt.YouTrackTicketStore(fake)
        self.assertTrue(store.set_stage("LME-1701", "factory:approved", ["factory:in-dev"]))
        self.assertEqual(sorted(t["name"] for t in fake.issue["tags"]), ["factory:approved", "risk:low"])
        self.assertFalse(store.set_stage("LME-1701", "factory:approved", ["factory:approved"]))
        with self.assertRaisesRegex(yt.MissingTag, "factory:nope"):
            store.add_attr("LME-1701", "factory:nope")

    def test_tickets_by_stage_queries_open_and_recently_resolved(self):
        fake = FakeYouTrack()
        yt.YouTrackTicketStore(fake).tickets_by_stage(["a:b", "c"])
        yt.YouTrackTicketStore(fake).tickets_by_stage(["c"], open_only=True)
        queries = [unquote(parse_qs(urlsplit(p).query)["query"][0]) for _, p, _ in fake.calls]
        self.assertEqual(queries, ["(tag: {a:b} or tag: {c}) and (#Unresolved or resolved date: {minus 48h} .. Now)",
                                   "(tag: {c}) and #Unresolved"])

    def test_token_comes_from_the_named_env_var_and_is_sent_as_a_bearer(self):
        import os
        from unittest.mock import patch, MagicMock
        sent = []
        resp = MagicMock()
        resp.__enter__.return_value.read.return_value = b"[]"
        with patch.dict(os.environ, {"MY_TOKEN": "secret"}), \
                patch("urllib.request.urlopen", side_effect=lambda req, timeout=None: (sent.append(req), resp)[1]):
            store = yt.YouTrackTicketStore.from_config({"url": "https://yt.example/", "token_env": "MY_TOKEN"})
            store.comments("T-1")
        self.assertEqual((sent[0].full_url.startswith("https://yt.example/api/issues/T-1/comments?fields="),
                          sent[0].get_header("Authorization")), (True, "Bearer secret"))


if __name__ == "__main__":
    unittest.main()

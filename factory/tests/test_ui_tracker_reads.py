#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Read-only tracker queries used by the progress UI, with local fake runners."""
import json
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))
import config  # noqa: E402
config.set_team(ROOT / "factory/tests/fixtures/team")
from factory_plugins.github.gate import GitHubGateTracker  # noqa: E402
from factory_plugins.youtrack.store import YouTrackTicketStore  # noqa: E402
from ports.system import Query  # noqa: E402


class ProgressUITrackerReadTest(unittest.TestCase):
    def test_stage_query_defaults_to_the_gate_48_hour_window(self):
        query = Query("stage", values=("ready",))
        self.assertEqual(query.closed_hours, 48)
        self.assertIsNone(query.updated_hours)

    def test_github_stage_reads_honor_closed_window_and_scope(self):
        calls = []

        def fake_gh(args):
            calls.append(args)
            return json.dumps({"data": {"search": {
                "nodes": [{"number": 17}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }}})

        tracker = GitHubGateTracker.for_repository("owner/repo", run=fake_gh)
        tracker.stages = {"ready": "factory:ready"}
        tracker.tickets.scope = {"label": "scope:alpha"}

        for closed_hours in (48, 168, None):
            with self.subTest(closed_hours=closed_hours):
                calls.clear()
                query = Query("stage", values=("ready",), closed_hours=closed_hours)
                self.assertEqual(tracker.find(query), [17])
                searches = [arg[i + 1][2:] for arg in calls for i, item in enumerate(arg)
                            if item == "-f" and arg[i + 1].startswith("q=")]
                self.assertEqual(len(searches), 2)
                self.assertTrue(all('label:"scope:alpha"' in search for search in searches), searches)
                self.assertTrue(all("label:\"factory:ready\"" in search for search in searches), searches)
                self.assertFalse("closed:" in searches[0])
                if closed_hours is None:
                    self.assertNotIn("closed:", searches[1])
                else:
                    self.assertIn("closed:>=", searches[1])

        calls.clear()
        self.assertEqual(tracker.find(Query("stage", values=("ready",), closed_hours=None, updated_hours=168)), [17])
        searches = [arg[i + 1][2:] for arg in calls for i, item in enumerate(arg)
                    if item == "-f" and arg[i + 1].startswith("q=")]
        self.assertEqual(len(searches), 2)
        self.assertTrue(all('label:"scope:alpha"' in search for search in searches), searches)
        self.assertFalse("updated:" in searches[0])
        self.assertRegex(searches[1], r"updated:>=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        self.assertNotIn("closed:", searches[1])

    def test_youtrack_stage_reads_honor_window_scope_and_repository_stage_tag(self):
        calls = []

        def fake_yt(method, path, body=None):
            calls.append((method, path, body))
            return []

        store = YouTrackTicketStore(
            fake_yt,
            repository_field="Repository",
            repo_stages=("factory:ready",),
            repositories=("owner/repo",),
            scope={"field": "Team", "value": "Alpha"},
        )

        for closed_hours in (48, 168, None):
            with self.subTest(closed_hours=closed_hours):
                calls.clear()
                store.tickets_by_stage(["factory:ready"], closed_hours=closed_hours)
                query = unquote(parse_qs(urlsplit(calls[-1][1]).query)["query"][0])
                self.assertIn("tag: {factory:ready-owner/repo}", query)
                self.assertIn("Team: {Alpha}", query)
                if closed_hours is None:
                    self.assertNotIn("resolved date:", query)
                else:
                    self.assertIn(f"resolved date: {{minus {closed_hours}h}} .. Now", query)

        calls.clear()
        store.tickets_by_stage(["factory:ready"], closed_hours=None, updated_hours=168)
        query = unquote(parse_qs(urlsplit(calls[-1][1]).query)["query"][0])
        self.assertIn("tag: {factory:ready-owner/repo}", query)
        self.assertIn("Team: {Alpha}", query)
        self.assertIn("(#Unresolved or updated: {minus 168h} .. Now)", query)
        self.assertNotIn("resolved date:", query)

    def test_youtrack_labeled_lists_only_requested_open_issue_labels(self):
        calls = []
        issue = {
            "idReadable": "LME-7", "summary": "Ready ticket", "description": "", "resolved": None,
            "updated": 1760000000000, "reporter": {"login": "alice"},
            "tags": [{"id": "1", "name": "factory:ready-owner/repo"}],
            "customFields": [
                {"name": "Repository", "value": {"name": "owner/repo"}},
                {"name": "Team", "value": {"name": "Alpha"}},
            ],
            "links": [],
        }
        activities = [
            {"timestamp": 1760000000000, "author": {"login": "alice"},
             "category": {"id": "TagsCategory"},
             "added": [{"name": "factory:ready-owner/repo"}], "removed": []},
            {"timestamp": 1760000001000, "author": {"login": "bob"},
             "category": {"id": "TagsCategory"},
             "added": [{"name": "factory:ready-owner/repo"}], "removed": []},
        ]

        def fake_yt(method, path, body=None):
            calls.append((method, path, body))
            parsed = urlsplit(path)
            if parsed.path == "/api/issues":
                query = unquote(parse_qs(parsed.query)["query"][0])
                if "#Unresolved" in query and "factory:ready-owner/repo" in query:
                    return [{"idReadable": "LME-7"}]
                return []
            if parsed.path == "/api/issues/LME-7":
                return deepcopy(issue)
            if parsed.path == "/api/issues/LME-7/activities":
                return deepcopy(activities)
            raise AssertionError(f"unexpected tracker request: {method} {path}")

        store = YouTrackTicketStore(
            fake_yt,
            repository_field="Repository",
            repo_stages=("factory:ready",),
            repositories=("owner/repo",),
            scope={"field": "Team", "value": "Alpha"},
        )
        result = store.labeled((
            ("issue", "factory:ready"),
            ("pr", "factory:pull-request-only"),
        ))

        self.assertEqual(result, [{
            "number": "LME-7", "pr": False, "branch": None,
            "label": "factory:ready", "event": "2025-10-09T08:53:21Z",
        }])
        search = unquote(parse_qs(urlsplit(next(path for method, path, _ in calls
                                                  if method == "GET" and urlsplit(path).path == "/api/issues")).query)["query"][0])
        self.assertIn("#Unresolved", search)
        self.assertIn("factory:ready-owner/repo", search)
        self.assertNotIn("factory:pull-request-only", search)
        self.assertIn("Team: {Alpha}", search)


if __name__ == "__main__":
    unittest.main()

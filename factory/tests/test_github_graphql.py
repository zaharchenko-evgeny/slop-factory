#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""github_graphql: batched GraphQL answers in the REST shapes the gate reads."""
import base64
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github_graphql as g  # noqa: E402

BOT = {"login": "jetbrains-air", "__typename": "Bot"}
PERSON = {"login": "ez", "__typename": "User"}


def node(n=7, labels=("factory:ready", "risk:low"), events=None, timeline_paged=False, comments_paged=False):
    events = [{"__typename": "LabeledEvent", "createdAt": "2026-11-05T10:00:00Z", "actor": BOT,
               "label": {"name": labels[0]}}] if events is None else events
    return {"number": n, "databaseId": 70, "id": "I_7", "state": "CLOSED", "stateReason": "NOT_PLANNED", "title": "t",
            "body": "b", "updatedAt": "2026-11-05T11:00:00Z", "closedAt": "2026-11-05T11:00:00Z", "author": PERSON,
            "assignees": {"nodes": [{"login": "ez"}]},
            "labels": {"pageInfo": {"hasNextPage": False}, "nodes": [{"name": x} for x in labels]},
            "blockedBy": {"pageInfo": {"hasNextPage": False}, "nodes": [{"number": 5, "databaseId": 50, "state": "OPEN"}]},
            "comments": {"pageInfo": {"hasPreviousPage": comments_paged},
                         "nodes": [{"databaseId": 9, "body": "[factory] x", "createdAt": "2026-11-05T10:01:00Z", "author": BOT}]},
            "timelineItems": {"pageInfo": {"hasNextPage": timeline_paged}, "nodes": events}}


def encoded(raw):
    """A GraphQL node id `LE_…` whose body is the base64url of `raw`, as GitHub writes it."""
    return "LE_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


class RestIdTest(unittest.TestCase):
    def test_the_sample_node_id_decodes_to_its_rest_id(self):
        self.assertEqual(g.rest_id("LE_lADOVagZ7s8AAAABcwaHgM8AAAAINa9FIQ"), 35260417313)

    def test_every_unsigned_int_format_decodes(self):
        self.assertEqual(g.rest_id(encoded(bytes([0x92, 0x01, 0x07]))), 7)
        self.assertEqual(g.rest_id(encoded(bytes([0x91, 0xcc, 0xff]))), 255)
        self.assertEqual(g.rest_id(encoded(bytes([0x91, 0xcd, 0x01, 0x02]))), 258)
        self.assertEqual(g.rest_id(encoded(bytes([0x91, 0xce, 0x00, 0x01, 0x00, 0x00]))), 65536)

    def test_garbage_gives_none(self):
        for bad in ("nope", "LE_", "LE_!!", encoded(bytes([0x91, 0xc0])), encoded(bytes([0x92, 0x01])),
                    encoded(bytes([0x91, 0x01, 0x02]))):
            self.assertIsNone(g.rest_id(bad), bad)


class ToRestTest(unittest.TestCase):
    def test_ticket_parts_have_the_rest_shapes(self):
        out = g.to_rest(node(events=[
            {"__typename": "LabeledEvent", "createdAt": "2026-11-05T10:00:00Z", "actor": BOT, "label": {"name": "factory:ready"}},
            {"__typename": "RenamedTitleEvent", "createdAt": "2026-11-05T10:02:00Z"}]), ["factory:ready"])
        issue = out["issues/7"]
        self.assertEqual((issue["state"], issue["state_reason"], issue["id"]), ("closed", "not_planned", 70))
        self.assertEqual(issue["labels"], [{"name": "factory:ready"}, {"name": "risk:low"}])
        self.assertEqual(issue["user"], {"login": "ez", "type": "User"})
        self.assertEqual(out["issues/7/events"], [
            {"event": "labeled", "created_at": "2026-11-05T10:00:00Z",
             "actor": {"login": "jetbrains-air[bot]", "type": "Bot"}, "label": {"name": "factory:ready"}},
            {"event": "renamed", "created_at": "2026-11-05T10:02:00Z"}])
        self.assertEqual(out["issues/7/comments"][0]["user"], {"login": "jetbrains-air[bot]", "type": "Bot"})
        self.assertEqual(out["issues/7/dependencies/blocked_by"], [{"number": 5, "id": 50, "state": "open"}])

    def test_paged_parts_are_left_to_rest(self):
        out = g.to_rest(node(timeline_paged=True, comments_paged=True), ["factory:ready"])
        self.assertNotIn("issues/7/events", out)
        self.assertNotIn("issues/7/comments", out)
        self.assertIn("issues/7", out)

    def test_empty_timeline_of_a_staged_ticket_is_left_to_rest(self):
        self.assertNotIn("issues/7/events", g.to_rest(node(events=[]), ["factory:ready"]))
        self.assertEqual(g.to_rest(node(labels=("risk:low",), events=[]), ["factory:ready"])["issues/7/events"], [])

    def test_event_names(self):
        self.assertEqual(g.rest_event("BlockedByAddedEvent"), "blocked_by_added")
        self.assertEqual(g.type_name("BLOCKED_BY_ADDED_EVENT"), "BlockedByAddedEvent")


class QueryTest(unittest.TestCase):
    def test_search_lists_open_and_recently_closed_tickets_with_any_stage_label(self):
        now = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
        open_q, closed_q = g.search_query("o/r", ["factory:ready", "factory:in-dev"], now)
        self.assertEqual(open_q, 'repo:o/r is:issue is:open label:"factory:ready","factory:in-dev"')
        self.assertEqual(closed_q, 'repo:o/r is:issue is:closed closed:>=2026-11-03T12:00:00Z label:"factory:ready","factory:in-dev"')

    def test_batch_query_names_every_ticket_and_no_project_event(self):
        q = g.batch_query("o", "r", [7, 8])
        self.assertIn("i7:issue(number:7)", q)
        self.assertIn("i8:issue(number:8)", q)
        self.assertNotIn("PROJECT", q)
        self.assertNotIn("CONVERTED_FROM_DRAFT", q)


if __name__ == "__main__":
    unittest.main()

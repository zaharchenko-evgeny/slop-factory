#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for factory/factory_plugins/youtrack/store.py with a stubbed runner. Run: uv run factory/tests/test_youtrack_tracker.py"""
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
from factory_plugins.youtrack import store as yt  # noqa: E402

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
            return yt.YouTrackTicketStore(fake, bots=[contract.BOT], writers=["alice"]), "T-1"

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
                patch("urllib.request.OpenerDirector.open", side_effect=lambda req, timeout=None: (sent.append(req), resp)[1]):
            store = yt.YouTrackTicketStore.from_config({"url": "https://yt.example/", "token_env": "MY_TOKEN"})
            store.comments("T-1")
        self.assertEqual((sent[0].full_url.startswith("https://yt.example/api/issues/T-1/comments?fields="),
                          sent[0].get_header("Authorization")), (True, "Bearer secret"))

    def test_redirect_does_not_forward_the_token(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200), self.end_headers(), self.wfile.write(b"[]")

            log_message = lambda *a: None

        target = HTTPServer(("127.0.0.1", 0), Target)

        class Redirect(Target):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/api/issues")
                self.end_headers()

        origin = HTTPServer(("127.0.0.1", 0), Redirect)
        for s in (origin, target):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        try:
            run = yt.urllib_runner(f"http://127.0.0.1:{origin.server_port}", "secret")
            with self.assertRaises(Exception):
                run("GET", "/api/issues")
        finally:
            origin.shutdown(), target.shutdown(), origin.server_close(), target.server_close()
        self.assertEqual(seen, [])

    def test_field_stage_child_gets_ready_in_the_state_field(self):
        fake = FakeYouTrack()
        store = yt.YouTrackTicketStore(fake, state_field="State")
        store._create = lambda title, body: "T-1"
        store._command = lambda *a: None
        store.open_blockers_all = lambda n: ["T-1"]
        store.create_children("T-9", [{"title": "c"}], "factory:ready")
        writes = [(m, b) for m, p, b in fake.calls if m == "POST"]
        self.assertEqual(writes, [("POST", {"customFields": [{"name": "State", "$type": "StateIssueCustomField",
                                                              "value": {"name": "factory:ready"}}]})])


class TrackerFromConfigTest(unittest.TestCase):
    """#1069: the youtrack plugin's tracker is the vcs's gate tracker over the YouTrack store, built from a tracker
    config block."""

    def descriptor(self, **tracker):
        import shutil
        import tempfile
        import yaml
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        desc = {**contract.CONFIG, "code_host": {"repository": "o/code"},
                "tracker": {**contract.CONFIG["tracker"], "adapter": "youtrack", "url": "https://yt.example",
                            "project": "LME", **tracker}}
        path = Path(d, "factory.descriptor.yaml")
        path.write_text(yaml.safe_dump(desc))
        return path, desc

    def test_tickets_in_youtrack_and_pull_requests_on_the_code_host(self):
        from factory_plugins.youtrack.gate import YouTrackTracker
        path, _ = self.descriptor(bot_logins=["factory[bot]"])
        t = YouTrackTracker.from_config(path, run=lambda *a, **k: "", yt_run=FakeYouTrack())
        self.assertIsInstance(t, contract.GitHubGateTracker)
        self.assertIsInstance(t.tickets, yt.YouTrackTicketStore)
        self.assertEqual((t.repo, t.pr_repo, t.tickets.project, t.tickets.bots), ("o/code", "o/code", "LME", {"factory[bot]"}))
        self.assertEqual((t.state_kinds, t.command_kinds), (("field", "tag"), ("tag", "pr_label")))

    def test_a_tracker_block_names_the_youtrack_project(self):
        from factory_plugins.youtrack.gate import YouTrackTracker
        path, desc = self.descriptor()
        block = {**desc["tracker"], "project": "OPS", "done_state": "Closed"}
        t = YouTrackTracker.from_config(path, run=lambda *a, **k: "", tracker=block, yt_run=FakeYouTrack())
        self.assertEqual((t.tickets.project, t.tickets.done_state), ("OPS", "Closed"))

    def test_a_code_host_repository_is_required(self):
        import yaml
        from factory_plugins.youtrack.gate import YouTrackTracker
        path, desc = self.descriptor()
        path.write_text(yaml.safe_dump({k: v for k, v in desc.items() if k != "code_host"}))
        with self.assertRaisesRegex(ValueError, "code_host.repository is required with tracker.adapter youtrack"):
            YouTrackTracker.from_config(path, run=lambda *a, **k: "", yt_run=FakeYouTrack())


class RepositoryTagsTest(unittest.TestCase):
    """#896: the stages that wake a per-repository node are tags `<tag>-<repo>`, the repository is a ticket field."""

    def store(self, repo="api", **kw):
        issue = copy.deepcopy(ISSUE)
        issue["tags"] = []
        issue["customFields"] = [{"name": "Repository", "value": {"name": repo}}] if repo else []
        fake = FakeYouTrack(issue, tags=("factory:ready", "factory:ready-api", "factory:ready-web"))
        return fake, yt.YouTrackTicketStore(fake, repository_field="Repository", repo_stages=["factory:ready"],
                                            repositories=["api", "web"], **kw)

    def test_set_stage_writes_the_repository_tag_and_reads_it_back_as_the_stage(self):
        fake, store = self.store()
        self.assertTrue(store.set_stage("LME-1701", "factory:ready", []))
        self.assertEqual([t["name"] for t in fake.issue["tags"]], ["factory:ready-api"])
        self.assertEqual(store.issue("LME-1701")["attrs"], ["factory:ready"])
        self.assertFalse(store.set_stage("LME-1701", "factory:ready", ["factory:ready"]))

    def test_a_later_stage_removes_the_repository_tag(self):
        fake, store = self.store()
        store.set_stage("LME-1701", "factory:ready", [])
        fake.tags["factory:in-dev"] = "id:factory:in-dev"
        self.assertTrue(store.set_stage("LME-1701", "factory:in-dev", ["factory:ready"]))
        self.assertEqual([t["name"] for t in fake.issue["tags"]], ["factory:in-dev"])

    def test_a_ticket_without_the_field_or_with_an_unknown_value_is_blocked_by_name(self):
        for repo, want in ((None, "is empty"), ("other", "holds 'other'")):
            fake, store = self.store(repo)
            self.assertRegex(store.repository("LME-1701")[1], f"'Repository' {want}")
            with self.assertRaisesRegex(yt.MissingRepository, "'Repository'"):
                store.set_stage("LME-1701", "factory:ready", [])
            self.assertEqual(fake.issue["tags"], [])

    def test_tickets_by_stage_queries_every_repository_tag(self):
        fake, store = self.store()
        store.tickets_by_stage(["factory:ready"], open_only=True)
        self.assertEqual(unquote(parse_qs(urlsplit(fake.calls[-1][1]).query)["query"][0]),
                         "(tag: {factory:ready-api} or tag: {factory:ready-web}) and #Unresolved")

    def test_create_children_sets_the_repository_field_and_links_by_after(self):
        fake, store = self.store()
        ids, cmds = iter(["T-2", "T-3"]), []
        store._create = lambda title, body, tags=(), repo=None: (cmds.append(("create", title, repo)), next(ids))[1]
        store._command = lambda n, q: cmds.append((n, q))
        store.open_blockers_all = lambda n: []
        store.set_stage = lambda *a: True
        store.create_children("T-1", [{"title": "w", "repo": "web", "after": ["api"]}, {"title": "a", "repo": "api"}],
                              "factory:ready")
        self.assertEqual(cmds, [("create", "a", "api"), ("T-2", "subtask of T-1"), ("T-1", "depends on T-2"),
                                ("create", "w", "web"), ("T-3", "subtask of T-1"), ("T-3", "depends on T-2"),
                                ("T-1", "depends on T-3")])

    def test_create_children_links_by_one_based_position_in_after(self):
        fake, store = self.store()
        ids, cmds = iter(["T-2", "T-3"]), []
        store._create = lambda title, body, tags=(), repo=None: (cmds.append(("create", title, repo)), next(ids))[1]
        store._command = lambda n, q: cmds.append((n, q))
        store.open_blockers_all = lambda n: []
        store.set_stage = lambda *a: True
        store.create_children("T-1", [{"title": "a", "repo": "api"}, {"title": "w", "repo": "web", "after": [1]}],
                              "factory:ready")
        self.assertIn(("T-3", "depends on T-2"), cmds)


class TagInventoryTest(unittest.TestCase):
    def test_lists_creates_and_deletes_factory_tags_but_preserves_others(self):
        calls = []

        def run(method, path, body=None):
            calls.append((method, path, body))
            return [{"id": "1", "name": "factory:old"}, {"id": "2", "name": "bug"}] if method == "GET" else {}
        store = yt.YouTrackTicketStore(run)
        self.assertEqual(store.list_tags(), {"factory:old": "1", "bug": "2"})
        store.create_tag("factory:ready")
        store.delete_tag("factory:old", ["factory:"])
        with self.assertRaises(ValueError):
            store.delete_tag("bug", ["factory:"])
        self.assertEqual([c for c in calls if c[0] != "GET"],
                         [("POST", "/api/tags?fields=id,name", {"name": "factory:ready"}), ("DELETE", "/api/tags/1", None)])

    def test_used_outside_project_queries_other_projects_and_is_conservative_without_a_project(self):
        calls = []

        def run(method, path, body=None):
            calls.append(path)
            return [{"idReadable": "X-1"}]
        self.assertTrue(yt.YouTrackTicketStore(run, project="LME").used_outside_project("factory:old"))
        self.assertIn("project%3A%20-%7BLME%7D", calls[0])
        self.assertTrue(yt.YouTrackTicketStore(run).used_outside_project("factory:old"))
        self.assertEqual(len(calls), 1)
        self.assertFalse(yt.YouTrackTicketStore(lambda *a, **k: [], project="LME").used_outside_project("factory:old"))


class FieldInventoryTest(unittest.TestCase):
    BASE = "/api/admin/customFieldSettings/bundles/state/b1/values"

    def store(self, field="State"):
        calls = []

        def run(method, path, body=None):
            calls.append((method, path, body))
            if path.startswith("/api/admin/projects?"):
                return [{"id": "0-1", "shortName": "LME"}]
            if method == "GET":
                return [{"field": {"name": field}, "bundle": {"id": "b1", "values": [
                    {"id": "1", "name": "factory:old", "isResolved": False}, {"id": "2", "name": "Open"}]}}]
            return {}
        return yt.YouTrackTicketStore(run, project="LME"), calls

    def test_lists_creates_updates_and_deletes_factory_values_but_preserves_others(self):
        store, calls = self.store()
        self.assertEqual(store.list_field_values("State"), {"factory:old": {"id": "1", "resolved": False}, "Open": {"id": "2", "resolved": None}})
        store.create_field_value("State", "factory:ready", False)
        store.update_field_value("State", "factory:old", True)
        store.delete_field_value("State", "factory:old", ["factory:"])
        with self.assertRaises(ValueError):
            store.delete_field_value("State", "Open", ["factory:"])
        self.assertEqual([c for c in calls if c[0] != "GET"], [
            ("POST", self.BASE + "?fields=id,name", {"name": "factory:ready", "isResolved": False}),
            ("POST", self.BASE + "/1?fields=id,name", {"isResolved": True}),
            ("DELETE", self.BASE + "/1", None)])

    def test_missing_field_is_an_error_and_no_field_is_created(self):
        store, calls = self.store(field="Other")
        with self.assertRaises(yt.MissingField):
            store.list_field_values("State")
        with self.assertRaises(yt.MissingField):
            store.create_field_value("State", "factory:ready")
        self.assertEqual([c for c in calls if c[0] != "GET"], [])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The system contract (factory/tests/system_contract.py) for the in-memory systems, for GitHub over a stubbed gh that
keeps what was written, for YouTrack tickets (a stubbed REST runner that keeps them) with GitHub pull requests, and
for Space merge requests (a stubbed HTTP runner that keeps them) beside in-memory tickets.
Run: uv run factory/tests/test_system_contract.py"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_gate_github as G  # noqa: E402  (sets the team the GitHub tracker reads)
from test_gate_github import BOT, M, REPO, STAGES, FakeGh, GitHubGateTracker, review  # noqa: E402
from system_contract import SystemContract  # noqa: E402
import derive  # noqa: E402
import effects  # noqa: E402
from factory_plugins.github import gate as gate_github  # noqa: E402
from factory_plugins.youtrack.store import YouTrackTicketStore  # noqa: E402
from factory_plugins.space.gate import SpaceCodeHost  # noqa: E402
from gate_port import MemorySystem, TagTracker  # noqa: E402
from gate_tracker import OnVcs  # noqa: E402

LABELS = ["factory:in-review", "risk:low", "factory:fix-mid"]


def relate_in_memory(tracker):
    """Ticket 7 has sub-tickets 21 and 22 and blocks 22; ticket 30 has its own sub-ticket 31."""
    kid = lambda n: {"number": n, "state": "open", "not_planned": False, "attrs": []}  # noqa: E731
    tracker.tickets[7].update(children=[kid(21), kid(22)], blocking=[{"number": 22, "state": "open"}])
    tracker.tickets.update({n: {"stage": "in-dev", "attrs": [], "assignees": []} for n in (21, 22, 31)})
    tracker.tickets[30] = {"stage": "in-dev", "children": [kid(31)], "attrs": [], "assignees": []}
    return {"children": (7, [21, 22]), "blocking": (7, [22]), "other": (30, [31])}


class MemorySystemContractTest(SystemContract, unittest.TestCase):
    def make(self):
        return MemorySystem({7: {"stage": "in-review", "attrs": ["risk:low", "factory:fix-mid"], "pr": 5, "head": "h1",
                                 "pr_labels": ["factory:fix-mid"], "assignees": []}})

    def relate(self, s):
        return relate_in_memory(s)


class TagTrackerContractTest(SystemContract, unittest.TestCase):
    def make(self):
        return TagTracker({7: {"tags": ["stage:in-review"], "attrs": ["risk:low", "factory:fix-mid"], "pr": 5, "head": "h1",
                               "pr_labels": ["factory:fix-mid"], "assignees": []}})

    def relate(self, s):
        return relate_in_memory(s)


def flag(args, name):
    """The value of `-f <name>=...` (or `--<name> ...`) in a gh call."""
    for i, x in enumerate(args[:-1]):
        if x in ("-f", "-F") and args[i + 1].startswith(f"{name}="):
            return args[i + 1].split("=", 1)[1]
        if x == f"--{name}":
            return args[i + 1]
    return None


class ContractGh(FakeGh):
    """FakeGh that keeps what the tracker writes, so a second read sees it: reviews and comments posted, the PR
    description, requested reviewers, the merge, created issues, sub-issue and blocked-by links, labels on them."""

    def __init__(self, **kw):
        super().__init__(labels=LABELS, **kw)
        self.pr_comments, self.pr_body = [], ""

    def __call__(self, args, timeout=None):
        a = next((x for x in args if x.startswith("repos/")), "")
        post = args[:3] == ["api", "-X", "POST"]
        at = "2026-01-03T00:00:00Z"
        if post and a.endswith("/pulls/5/reviews"):
            self.writes.append(args)
            self.reviews.append(review(BOT, flag(args, "body"), commit=flag(args, "commit_id"), at=at))
            return ""
        if post and a.endswith("/pulls/5/requested_reviewers"):
            self.writes.append(args)
            self.requested += [x.split("=", 1)[1] for x in args if x.startswith("reviewers[]=")]
            return ""
        if args[:2] == ["issue", "comment"]:
            self.writes.append(args)
            c = {"body": flag(args, "body"), "user": {"login": BOT, "type": "Bot"}, "created_at": at}
            (self.comments if args[2] == "7" else self.pr_comments).append(c)
            return ""
        if a.endswith("issues/5/comments") and "-f" not in args:
            return G.recorded(a, self.pr_comments)
        if args[:2] == ["pr", "edit"] and "--body" in args:
            self.writes.append(args)
            self.pr_body = flag(args, "body")
            return ""
        if args[:2] == ["pr", "merge"]:
            self.writes.append(args)
            self.merged = True
            return ""
        if post and a == f"repos/{REPO}/issues":
            self.writes.append(args)
            n = 900 + len(self.open_issues)
            self.open_issues.append({"id": n, "number": n, "title": flag(args, "title"), "body": flag(args, "body"),
                                     "labels": [{"name": x.split("=", 1)[1]} for x in args if x.startswith("labels[]=")]})
            return json.dumps(self.open_issues[-1])
        if post and a.endswith("/issues/7/sub_issues"):
            self.writes.append(args)
            n = int(flag(args, "sub_issue_id"))
            self.sub.append(next(i["title"] for i in self.open_issues if i["id"] == n))
            return ""
        if post and a.endswith("/issues/7/dependencies/blocked_by"):
            self.writes.append(args)
            self.blocked.append({"state": "open", "id": int(flag(args, "issue_id"))})
            return ""
        if post and a.endswith("/labels") and "/issues/9" in a:
            self.writes.append(args)
            n = int(a.split("/issues/")[1].split("/")[0])
            issue = next(i for i in self.open_issues if i["number"] == n)
            issue["labels"] += [{"name": x.split("=", 1)[1]} for x in args if x.startswith("labels[]=")]
            return ""
        out = super().__call__(args, timeout)
        if a.endswith("/pulls/5") and out:
            return json.dumps({**json.loads(out), "body": self.pr_body})
        return out


class GitHubContractTest(SystemContract, unittest.TestCase):
    SETUP = {
        ("join", "ticket"): lambda s: s.gh.reviews.append(review(BOT, M)),  # the round at the head is complete
        ("request-reviewers", "pr"): lambda s: s.gh.reviews.append(review("alice", "", state="CHANGES_REQUESTED")),
        ("pr-notes-sync", "pr"): lambda s: s.gh.comments.append(
            {"body": f"{gate_github.NOTES_PARKED}\n\n{derive.notes_block({'for': 'fixer', 'hint': 'h'})}",
             "user": {"login": BOT, "type": "Bot"}, "created_at": "2026-01-02T00:00:00Z"}),
    }
    XFAIL = {
        "test_a_stage_effect_that_expects_another_stage_writes_nothing": "#1280: the stage effect ignores `expect`",
        "test_a_ticket_event_names_its_actor": "#1280: the stub keeps no events of what the tracker writes",
        "test_find_blocking_lists_the_tickets_a_ticket_blocks": "#1280: find has no `blocking` query",
        "test_event_keeps_the_canonical_trigger_name": "#1280: event reads raw webhook payloads",
        "test_event_without_a_ticket_is_none": "#1280: event reads raw webhook payloads",
    }
    NO_WRITE = {("seen", "ticket"): "GitHub keeps no seen list: a duplicate event fails its node's conditions"}
    EXEMPT = {
        ("react", "ticket"): "the reactions API keeps one per user and answers the existing one: the handler reports it",
        ("react", "pr"): "as react on the ticket",
        ("update-branch", "pr"): "a single repository's update is not read first: GitHub refuses the second PUT, "
                                 "its expected_head_sha having moved",
    }

    def setUp(self):
        for p in (patch.object(G.M_GH.time, "sleep"), patch.object(GitHubGateTracker, "RECENT_MERGE_SECONDS", 0)):
            p.start()
            self.addCleanup(p.stop)

    NUMBERS = (1, 2)  # what the stub numbers the two sub-tickets that `relate` creates

    def relate(self, s):
        """Two sub-tickets of 7, created through the effect: the second waits for the first."""
        self.assertTrue(s.apply({"ticket": 7, "kind": "create-issues",
                                 "value": [{"title": "A", "body": "a"}, {"title": "B", "body": "b"}]}))
        first, second = self.NUMBERS
        return {"children": (7, [first, second]), "blocking": (first, [second]), "other": (second, [])}

    def make(self):
        gh = ContractGh()
        t = GitHubGateTracker(REPO, STAGES, BOT, fix_cap=3, run=gh)
        t.gh = gh
        return t


class ContractYouTrack:
    """Enough of YouTrack's REST API for the contract, keeping what the store writes: issues with tags, custom fields
    (`State: Done` resolves), comments, `subtask of` and `depends on` commands, and created issues. A tag exists once
    it is named (deploy creates the definition's tags before the gate runs)."""

    def __init__(self, attrs):
        self.issues, self.tags, self.writes = {}, {}, []
        self._new("7", "Ticket 7", "the scope", attrs)

    def _new(self, n, summary, description, attrs=()):
        self.issues[n] = {"summary": summary, "description": description, "resolved": None, "tags": list(attrs),
                          "fields": {}, "comments": [], "parent": None, "depends": []}
        return n

    def _tag(self, name):
        return self.tags.setdefault(name, f"id:{name}")

    def _links(self, n):
        ref = lambda m: {"idReadable": m, "resolved": self.issues[m]["resolved"],  # noqa: E731
                         "tags": [{"name": t} for t in self.issues[m]["tags"]]}
        i = self.issues[n]
        return [{"direction": "INWARD", "linkType": {"name": "Depend"}, "issues": [ref(m) for m in i["depends"]]},
                {"direction": "OUTWARD", "linkType": {"name": "Depend"},
                 "issues": [ref(m) for m, x in self.issues.items() if n in x["depends"]]},
                {"direction": "INWARD", "linkType": {"name": "Subtask"}, "issues": [ref(i["parent"])] if i["parent"] else []},
                {"direction": "OUTWARD", "linkType": {"name": "Subtask"},
                 "issues": [ref(m) for m, x in self.issues.items() if x["parent"] == n]}]

    def _issue(self, n):
        i = self.issues[n]
        return {"idReadable": n, "summary": i["summary"], "description": i["description"], "resolved": i["resolved"],
                "updated": 1, "reporter": {"login": "alice"}, "tags": [{"id": self._tag(t), "name": t} for t in i["tags"]],
                "customFields": [{"name": k, "value": v} for k, v in i["fields"].items()], "links": self._links(n)}

    def _query(self, query):
        if query.startswith("subtask of: "):
            return [{"idReadable": m, "summary": x["summary"]} for m, x in self.issues.items()
                    if x["parent"] == query.split(": ", 1)[1]]
        if query.startswith("#Unresolved summary: {"):
            title = query[len("#Unresolved summary: {"):-1]
            return [{"idReadable": m, "summary": x["summary"]} for m, x in self.issues.items()
                    if x["summary"] == title and not x["resolved"]]
        return []

    def __call__(self, method, path, body=None):
        url, q = urlsplit(path), parse_qs(urlsplit(path).query)
        parts = url.path.split("/")[2:]
        if method != "GET":
            self.writes.append((method, url.path, body))
        if parts == ["tags"]:
            if "query" in q:
                self._tag(q["query"][0])
            return [{"id": i, "name": t} for t, i in self.tags.items() if "query" not in q or t == q["query"][0]]
        if parts == ["issues"]:
            if method == "POST":
                return {"idReadable": self._new(f"T-{900 + len(self.issues)}", body["summary"], body["description"])}
            return self._query(q.get("query", [""])[0])
        n, rest = parts[1], parts[2:]
        i = self.issues[n]
        if not rest:
            if method == "GET":
                return self._issue(n)
            for f in body["customFields"]:
                i["fields"][f["name"]] = f["value"]
                if f["name"] == "State":
                    i["resolved"] = 1 if (f["value"] or {}).get("name") == "Done" else None
            return {}
        if rest == ["activities"]:
            return []
        if rest == ["comments"]:
            if method == "POST":
                i["comments"].append({"text": body["text"], "created": 1767225600000 + len(i["comments"]),
                                      "author": {"login": BOT}})
                return {}
            return list(i["comments"])
        if rest[0] == "tags":
            if method == "POST":
                i["tags"].append(next(t for t, x in self.tags.items() if x == body["id"]))
            else:
                i["tags"] = [t for t in i["tags"] if self.tags[t] != rest[1]]
            return {}
        if rest == ["execute"]:
            verb, _, other = body["query"].rpartition(" ")
            if verb == "subtask of":
                i["parent"] = other
            elif verb == "depends on" and other not in i["depends"]:
                i["depends"].append(other)
            return {}
        raise AssertionError(path)


class YouTrackContractTest(SystemContract, unittest.TestCase):
    """The youtrack plugin's tracker: its tickets in YouTrack (the store's own effects: stage, tags, set-field), its
    pull requests on GitHub."""
    SETUP = {
        ("join", "ticket"): GitHubContractTest.SETUP[("join", "ticket")],
        ("request-reviewers", "pr"): GitHubContractTest.SETUP[("request-reviewers", "pr")],
        ("pr-notes-sync", "pr"): lambda s: s.tickets.comment(
            7, f"{gate_github.NOTES_PARKED}\n\n{derive.notes_block({'for': 'fixer', 'hint': 'h'})}"),
    }
    NO_WRITE = GitHubContractTest.NO_WRITE
    EXEMPT = GitHubContractTest.EXEMPT
    NUMBERS = ("T-901", "T-902")
    XFAIL = {
        "test_a_stage_effect_that_expects_another_stage_writes_nothing": "#1281: the stage effect ignores `expect`",
        "test_a_ticket_event_names_its_actor": "#1281: the store keeps no events of what it writes",
        "test_a_stage_effect_that_expects_the_current_stage_moves_it": "#1281: the stage effect ignores `expect`",
        "test_find_stage_lists_the_tickets_in_a_stage": "#1281: find stage reads no stage of the stub's tickets",
        "test_find_blocking_lists_the_tickets_a_ticket_blocks": "#1281: find has no `blocking` query",
        "test_event_keeps_the_canonical_trigger_name": "#1281: event reads raw webhook payloads",
        "test_event_without_a_ticket_is_none": "#1281: event reads raw webhook payloads",
    }
    setUp = GitHubContractTest.setUp
    relate = GitHubContractTest.relate

    def make(self):
        gh, yt = ContractGh(), ContractYouTrack([STAGES["in-review"], "risk:low", "factory:fix-mid"])
        t = GitHubGateTracker(REPO, STAGES, BOT, fix_cap=3, run=gh, tickets=YouTrackTicketStore(yt, bots=[BOT]))
        t.gh, t.yt = gh, yt
        return t

    def test_ticket_effects_are_the_stores_and_write_youtrack(self):
        s = self.make()
        mine = {k for k, x in effects.registry(s).items() if str(x.apply).startswith("factory_plugins.youtrack.")}
        self.assertEqual(mine, {("stage", "ticket"), ("label", "ticket"), ("unlabel", "ticket"), ("set-field", "ticket")})
        self.assertTrue(s.apply({"ticket": 7, "kind": "set-field", "value": {"risk": "high"}}))
        self.assertEqual(s.yt.issues["7"]["tags"], [STAGES["in-review"], "factory:fix-mid", "risk:high"])
        self.assertEqual([w for w in s.gh.writes if "labels" in str(w)], [])


class ContractSpace:
    """Enough of Space's HTTP API for the contract, keeping what the host writes: merge request 5 of ticket 7 (branch
    `ez/7-...`) with its labels, reviews (the bot's, at the posted head), its merge and its branch update."""

    def __init__(self):
        self.mrs = [{"number": 5, "title": "Ticket 7", "state": "open", "branch": "ez/7-thing", "head": "h1",
                     "ci": "none", "labels": ["factory:fix-mid"], "reviews": [], "behind": True}]
        self.writes = []

    def __call__(self, method, path, body=None):
        if method == "GET" and path.endswith("/code-reviews"):
            return {"data": self.mrs}
        number, _, action = path.split("/code-reviews/number:", 1)[1].partition("/")
        mr = next(m for m in self.mrs if m["number"] == int(number))
        if method == "GET":
            return mr
        self.writes.append((method, path, body))
        if action == "labels":
            mr["labels"].append(body["label"])
        elif action.startswith("labels/"):
            mr["labels"].remove(action.split("/", 1)[1])
        elif action == "reviews":
            mr["reviews"].append({"author": BOT, "verdict": body["verdict"], "commit_id": body["commitId"],
                                  "body": body["body"], "submitted_at": "2026-01-03T00:00:00Z"})
        elif action == "merge":
            mr["state"] = "merged"
        elif action == "update-branch":
            mr["behind"] = False
        else:
            raise AssertionError(path)
        return {}


class SpaceContractTest(SystemContract, unittest.TestCase):
    """The space plugin's vcs beside in-memory tickets (gate_tracker.OnVcs): every `(kind, "pr")` it declares goes
    to Space over its HTTP runner and converges; the ticket effects stay the tracker's."""

    def make(self):
        space = ContractSpace()
        vcs = SpaceCodeHost.from_config(descriptor={"vcs": {"adapter": "space", "url": "https://space.example",
                                                             "project": "IJ"}}, http=space, env={"SPACE_TOKEN": "t"})
        s = OnVcs.of(MemorySystemContractTest.make(self), vcs)
        s.space = space
        return s

    def relate(self, s):
        return relate_in_memory(s.tracker)

    def test_pr_effects_are_spaces_and_write_space(self):
        s = self.make()
        mine = {k for k, x in effects.registry(s).items() if getattr(x.apply, "__module__", "") == "factory_plugins.space.gate"}
        self.assertEqual(mine, {("label", "pr"), ("unlabel", "pr"), ("review", "pr"), ("update-branch", "pr"),
                                ("merge", "pr")})
        self.assertTrue(s.apply({"ticket": 7, "kind": "label", "value": "factory:needs-human", "on": "pr"}))
        self.assertEqual([(m, p.rsplit("/", 1)[1]) for m, p, _ in s.space.writes], [("POST", "labels")])
        self.assertNotIn("factory:needs-human", s.tracker.tickets[7]["pr_labels"])
        self.assertTrue(s.apply({"ticket": 7, "kind": "label", "value": "x"}))  # a ticket label: the tracker's
        self.assertEqual(len(s.space.writes), 1)

    def test_facts_are_the_trackers_with_the_merge_request_facts(self):
        s = self.make()
        s.space.mrs[0]["ci"] = "success"
        f = s.facts(7)
        self.assertEqual((f["pr"], f["head"], f["ci"], f["pr_labels"], f["stage"]),
                         (5, "h1", "green", ["factory:fix-mid"], "in-review"))


if __name__ == "__main__":
    unittest.main()

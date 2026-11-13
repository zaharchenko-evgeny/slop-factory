#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Tests for per-environment trackers with scope (#1069, factory/scopes.py). The example is five projects of one
factory: four in one YouTrack project split by the `Subsystem` field, one in GitHub split by a label.
Run: uv run factory/tests/test_env_trackers.py"""
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import test_gate_github as contract  # noqa: E402
import config as cfg  # noqa: E402
from factory_plugins.air import deploy as air  # noqa: E402
import gate  # noqa: E402
import nodes  # noqa: E402
import plugins  # noqa: E402
import scopes  # noqa: E402
from factory_plugins.github.gate import GitHubCodeHost, GitHubTicketStore  # noqa: E402
from factory_plugins.youtrack.store import YouTrackTicketStore  # noqa: E402

YT = {"adapter": "youtrack", "url": "https://yt.example", "project": "PRJ"}
SUBSYSTEMS = {"backend": "Backend", "frontend": "Frontend", "mobile": "Mobile", "infra": "Infra"}


def example(**edit):
    """The user's example: a top-level YouTrack tracker, four environments in project PRJ by Subsystem, and `web` in
    GitHub's o/web by the label `area:web`; `team` has no tracker of its own."""
    c = copy.deepcopy(contract.CONFIG)
    c["tracker"] = {**c["tracker"], **YT}
    c["code_host"] = {"repository": "o/backend"}
    names = [*SUBSYSTEMS, "web"]
    c["repositories"] = [{"name": n, "url": f"https://github.com/o/{n}", "default_branch": "main",
                          "role": "primary" if n == "backend" else "project"} for n in names]
    c["environments"] = {n: {"repositories": [n], "tracker": {"adapter": "youtrack", "project": "PRJ",
                                                              "scope": {"field": "Subsystem", "value": v}}}
                         for n, v in SUBSYSTEMS.items()}
    c["environments"]["web"] = {"repositories": ["web"], "tracker": {"adapter": "github-issues", "repository": "o/web",
                                                                     "scope": {"label": "area:web"}}}
    c["environments"]["team"] = {"repositories": ["backend"]}
    for k, v in edit.items():
        c[k] = v
    return c


class FakeYouTrack:
    """YouTrack's REST API over `issues` ({id: raw issue}); records every request."""

    def __init__(self, issues=None):
        self.issues, self.calls = copy.deepcopy(issues or {}), []

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        parts = urlsplit(path).path.split("/")[2:]
        if parts == ["issues"]:
            return {"idReadable": "PRJ-10"} if method == "POST" else []
        if parts[0] == "tags":
            return [{"id": "t1", "name": parse_qs(urlsplit(path).query).get("query", [""])[0]}]
        issue = self.issues.setdefault(parts[1], {"idReadable": parts[1], "tags": [], "customFields": []})
        if len(parts) == 2 and method == "GET":
            return copy.deepcopy(issue)
        if len(parts) == 2:
            for f in body["customFields"]:
                issue["customFields"] = [x for x in issue["customFields"] if x["name"] != f["name"]] + [f]
            return {}
        return []

    def queries(self):
        return [unquote(parse_qs(urlsplit(p).query)["query"][0]) for m, p, _ in self.calls
                if urlsplit(p).path == "/api/issues" and m == "GET"]

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class FakeGh:
    """`gh` over GitHub's o/web issues ({number: labels}) and PR heads ({(repo, number): ref}); records every call."""

    def __init__(self, issues=None, prs=None):
        self.issues, self.prs, self.calls = issues or {}, prs or {}, []

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        path = next((x for x in args if x.startswith("repos/")), "")
        if args[:2] == ["api", "graphql"]:
            return json.dumps({"data": {"search": {"nodes": [], "pageInfo": {"hasNextPage": False}}}})
        if "-X" in args:
            return json.dumps({"id": 100, "number": 9}) if path.endswith("/issues") else "{}"
        if "--paginate" in args:
            return ""
        repo = "/".join(path.removeprefix("repos/").split("/")[:2])
        rest = "/".join(path.removeprefix("repos/").split("/")[2:])
        if rest.startswith("pulls/"):
            return json.dumps({"head": {"ref": self.prs[(repo, int(rest[6:]))]}, "title": ""})
        if rest.startswith("issues/"):
            n = int(rest[7:])
            if n not in self.issues:
                raise RuntimeError("HTTP 404")
            return json.dumps({"title": "t", "body": "", "state": "open", "user": {"login": "a"}, "assignees": [],
                               "labels": [{"name": x} for x in self.issues[n]]})
        return "{}"

    def writes(self):
        return [c for c in self.calls if "-X" in c or c[:2] in (["issue", "edit"], ["issue", "comment"], ["pr", "edit"])]


def yt_issue(n, subsystem=None, tags=()):
    return {"idReadable": n, "summary": "t", "description": "", "resolved": None, "updated": 1,
            "reporter": {"login": "a"}, "tags": [{"id": f"id:{t}", "name": t} for t in tags],
            "customFields": [{"name": "Subsystem", "value": {"name": subsystem}}] if subsystem else [], "links": []}


def scoped(config, yt, gh, env=None):
    """The gate's tracker over the example's environments, on the stubbed runners."""
    def build(c, e):
        youtrack = scopes.adapter(scopes.project_envs(c)[e]) == "youtrack"
        return gate.env_systems(c, e, run=gh, **({"yt_run": yt} if youtrack else {}))
    return scopes.ScopedTracker(config, build, env)


TICKETS = {"PRJ-1": "Backend", "PRJ-2": "Frontend", "PRJ-3": "Mobile", "PRJ-4": "Infra"}


def fakes():
    yt = FakeYouTrack({**{n: yt_issue(n, s) for n, s in TICKETS.items()}, "PRJ-9": yt_issue("PRJ-9")})
    gh = FakeGh({5: ["area:web", "factory:ready"], 6: ["factory:ready"]},
                {("o/web", 12): "ez/5-fix", ("o/frontend", 13): "ez/PRJ-2-ui"})
    return yt, gh


class ResolutionTest(unittest.TestCase):
    def test_merged_blocks_inherit_the_top_level_tracker_of_the_same_adapter(self):
        envs = scopes.project_envs(example())
        self.assertEqual(list(envs), [*SUBSYSTEMS, "web"])  # `team` declares no tracker
        self.assertEqual((envs["backend"]["url"], envs["backend"]["stages"]), ("https://yt.example", contract.STAGES))
        self.assertNotIn("url", envs["web"])  # another adapter: only the factory's own keys carry over
        self.assertEqual((envs["web"]["repository"], envs["web"]["stages"]), ("o/web", contract.STAGES))

    def test_each_ticket_resolves_to_its_environment(self):
        yt, gh = fakes()
        s = scoped(example(), yt, gh)
        got = {n: s.env_of(n) for n in [*TICKETS, 5]}
        self.assertEqual(got, {"PRJ-1": "backend", "PRJ-2": "frontend", "PRJ-3": "mobile", "PRJ-4": "infra", 5: "web"})
        self.assertIsNone(s.env_of("PRJ-9"))  # no Subsystem: no environment's
        self.assertIsNone(s.env_of(6))  # no `area:web` label
        self.assertIsInstance(s.systems("backend")[0].tickets, YouTrackTicketStore)
        self.assertEqual((s.systems("backend")[0].pr_repo, s.systems("web")[0].repo), ("o/backend", "o/web"))

    def test_the_runs_environment_takes_only_its_own_tickets(self):
        yt, gh = fakes()
        s = scoped(example(), yt, gh, env="frontend")
        self.assertEqual((s.env_of("PRJ-2"), s.env_of("PRJ-1")), ("frontend", None))
        with self.assertRaises(scopes.OutOfScope):
            s.facts("PRJ-1")

    def test_a_pr_resolves_through_its_repositorys_environment(self):
        yt, gh = fakes()
        s = scoped(example(), yt, gh)
        self.assertEqual(s.ticket_for_pr(12, "o/web"), 5)
        self.assertEqual((s.prefer, s.env_of(5)), ("web", "web"))
        self.assertIsInstance(s.vcs_for("o/web")[1], GitHubCodeHost)
        self.assertEqual(s.vcs_for("o/web")[1].repo, "o/web")
        s = scoped(example(), yt, gh)
        self.assertEqual(s.ticket_for_pr(13, "o/frontend"), "PRJ-2")
        self.assertEqual((s.prefer, s.env_of("PRJ-2")), ("frontend", "frontend"))

    def test_no_environment_tracker_keeps_one_top_level_tracker(self):
        c = example()
        for e in c["environments"].values():
            e.pop("tracker", None)
        self.assertEqual(scopes.project_envs(c), {})
        with mock.patch.object(gate, "tracker_of", return_value="T"), \
                mock.patch.object(GitHubCodeHost, "from_config", return_value="V"):
            self.assertEqual(gate.systems({**c, "tracker": {"adapter": "github-issues"}}), ("T", "V"))


class GateNoOpTest(unittest.TestCase):
    """A step on a ticket outside every scope is a quiet no-op: one `skip:` line, nothing written."""

    def run_gate(self, s, *argv):
        out = io.StringIO()
        with mock.patch.object(gate, "systems", return_value=(s, s)), contextlib.redirect_stdout(out):
            code = gate.main(list(argv))
        return code, out.getvalue()

    def files(self, ticket):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [p.unlink() for p in d.iterdir()] and d.rmdir())
        spec = {"node": "work", "role": "developer", "when": {"always": True},
                "outcomes": {"done": {"effects": [{"label": "factory:done-x"}, {"stage": "done"}]}}}
        (d / "spec.json").write_text(json.dumps(spec))
        (d / "event.json").write_text(json.dumps({"id": "e1", "ticket": ticket}))
        (d / "payload.json").write_text(json.dumps({"ticket": ticket}))
        return d

    def test_check_does_not_go_and_apply_writes_nothing(self):
        yt, gh = fakes()
        d = self.files("PRJ-9")
        code, out = self.run_gate(scoped(example(), yt, gh), "check", "--node", "work", "--spec", str(d / "spec.json"),
                                  "--event", str(d / "event.json"))
        self.assertEqual((code, json.loads(out)), (gate.NO_GO, {"go": False, "reason": "skip: PRJ-9 outside every environment scope"}))
        code, out = self.run_gate(scoped(example(), yt, gh), "apply", "--node", "work", "--outcome", "done",
                                  "--spec", str(d / "spec.json"), "--payload", str(d / "payload.json"))
        self.assertEqual((code, out.strip()), (gate.GO, json.dumps({"skipped": True, "reason": "skip: PRJ-9 outside every environment scope"})))
        self.assertEqual((yt.writes(), gh.writes()), ([], []))

    def test_sweep_of_an_unscoped_issue_skips_it(self):
        yt, gh = fakes()
        code, out = self.run_gate(scoped(example(), yt, gh), "sweep", "--issue", "6")
        self.assertEqual((code, json.loads(out.splitlines()[0])["sweep"]), (gate.GO, []))
        self.assertEqual(gh.writes(), [])


class SweepTest(unittest.TestCase):
    def test_each_environment_sweeps_with_its_scoped_query(self):
        yt, gh = fakes()
        s = scoped(example(), yt, gh)
        self.assertEqual(s.sweep_tickets(["ready"], open_only=True), [])
        ready = contract.STAGES["ready"]
        self.assertEqual(yt.queries(), [f"((tag: {{{ready}}}) and #Unresolved) and Subsystem: {{{v}}}" for v in SUBSYSTEMS.values()])
        searches = [c for c in gh.calls if c[:2] == ["api", "graphql"]]
        self.assertEqual([c[-1] for c in searches], [f'q=repo:o/web is:issue is:open label:"{ready}" label:"area:web"'])

    def test_a_swept_ticket_belongs_to_the_environment_that_found_it(self):
        yt, gh = fakes()
        s = scoped(example(), yt, gh)
        s.systems("mobile")[0].sweep_tickets = lambda *a, **k: ["PRJ-3"]
        self.assertEqual(s.sweep_tickets(["ready"]), ["PRJ-3"])
        self.assertEqual(s._env_of["PRJ-3"], "mobile")


class CreatedTicketsTest(unittest.TestCase):
    def test_a_youtrack_sub_issue_gets_the_scope_field(self):
        yt = FakeYouTrack()
        store = YouTrackTicketStore(yt, project="PRJ", scope={"field": "Subsystem", "value": "Backend"})
        store._command = lambda *a: None
        store.open_blockers_all = lambda n: []
        store.set_stage = lambda *a: True
        store.create_children("PRJ-1", [{"title": "child", "body": "b"}], "factory:ready")
        self.assertIn(("POST", "/api/issues/PRJ-10?fields=idReadable",
                       {"customFields": [{"name": "Subsystem", "$type": "SingleEnumIssueCustomField",
                                          "value": {"name": "Backend"}}]}), yt.calls)

    def test_a_youtrack_child_named_for_another_repository_gets_its_scope(self):  # #1259
        yt = FakeYouTrack()
        store = YouTrackTicketStore(yt, project="LME", scope={"field": "Subsystem", "value": "JBeval"},
                                    repository_scopes={"JetBrains/harbor-infra": {"field": "Subsystem", "value": "Harbor"}})
        store._command = lambda *a: None
        store.open_blockers_all = lambda n: []
        store.set_stage = lambda *a: True
        store.create_children("LME-1", [{"title": "harbor part", "body": "b", "repo": "harbor-infra"},
                                        {"title": "jbeval part", "body": "b"}], "factory:ready")
        scopes_written = [f["value"]["name"] for m, p, b in yt.calls if m == "POST" and b and "customFields" in b
                          for f in b["customFields"] if f["name"] == "Subsystem"]
        self.assertEqual(scopes_written, ["Harbor", "JBeval"])

    def test_a_github_sub_issue_gets_the_scope_label(self):
        gh = FakeGh({1: ["risk:low"]})
        store = GitHubTicketStore("o/web", gh, scope={"label": "area:web"})
        store.create_children(1, [{"title": "child", "body": "b"}], "factory:ready")
        create = next(c for c in gh.calls if "-X" in c and c[c.index("-X") + 2] == "repos/o/web/issues")
        self.assertIn("labels[]=area:web", create)
        self.assertIn("labels[]=risk:low", create)

    def test_unscoped_stores_create_as_before(self):
        yt = FakeYouTrack()
        YouTrackTicketStore(yt, project="PRJ")._create("t", "b")
        self.assertFalse([c for c in yt.calls if c[0] == "POST" and "customFields" in (c[2] or {})])


class DeployTest(unittest.TestCase):
    def plan(self):
        before = cfg.team_root()
        cfg.set_team(HERE / "fixtures/slop")
        try:
            conf = copy.deepcopy(cfg.load())
            ex = example()
            conf["tracker"] = {**conf["tracker"], **YT}
            conf["repositories"] += [{**r, "role": "project"} for r in ex["repositories"]]
            conf["environments"].update({k: v for k, v in ex["environments"].items() if k != "team"})
            compiled = copy.deepcopy(nodes.load())
            for name in [*SUBSYSTEMS, "web"]:
                compiled["environments"][name] = {**compiled["environments"]["engine"], "repository": name}
            compiled["nodes"]["implement-medium"]["triggers"] = ["youtrack.tagAdded factory:implement-medium"]
            # #1069's fan-out is the path of a definition not in the ADR-0027 form; the Slop fixture now is
            with mock.patch.object(air.definition, "adopted", return_value=False):
                return air.plan_air(compiled, conf)
        finally:
            cfg.set_team(before)

    def test_one_automation_per_project_environment(self):
        plan = self.plan()
        a = {x["name"]: x for x in plan["automations"]}
        for env, value in SUBSYSTEMS.items():
            x = a[f"[Slop factory] implement-medium {env}"]
            self.assertEqual(x["environmentConfigId"], f"[Slop factory] {env}")
            self.assertEqual(x["repository"]["fullName"], f"o/{env}")
            self.assertEqual(x["triggers"], [{"triggerType": "EVENT", "source": "youtrack", "eventType": "issueUpdated",
                                              "filters": {"tagAdded": "factory:implement-medium", "Subsystem": value}}])
        self.assertEqual(a["[Slop factory] implement-medium web"]["triggers"][0]["filters"],
                         {"tagAdded": "factory:implement-medium"})  # GitHub: the gate's scope check
        self.assertNotIn("[Slop factory] implement-medium", a)  # a default-environment node runs only per project
        self.assertIn("[Slop factory] retro", a)  # another environment's node is planned once, as before
        envs = {e["name"]: e for e in plan["environments"]}
        for env in [*SUBSYSTEMS, "web"]:
            self.assertEqual(envs[f"[Slop factory] {env}"]["variables"]["FACTORY_ENV"], env)
        self.assertIn("o/web", plan["label_plan"])


class ValidationTest(unittest.TestCase):
    def test_the_example_is_valid_and_turns_on_both_plugins(self):
        self.assertEqual(scopes.errors(example()), [])
        self.assertTrue({"github", "youtrack"} <= set(plugins.selected(example())))

    def test_scope_keys_must_be_the_adapters(self):
        c = example()
        c["environments"]["web"]["tracker"]["scope"] = {"field": "Area", "value": "web"}
        c["environments"]["infra"]["tracker"]["scope"] = {"label": "infra"}
        errs = scopes.errors(c)
        self.assertIn("bad-scope: factory.yaml: environments.web.tracker.scope: github-issues takes label", errs)
        self.assertIn("bad-scope: factory.yaml: environments.infra.tracker.scope: youtrack takes field and value "
                      "(optional: type)", errs)

    def test_overlapping_scopes_on_one_tracker(self):
        c = example()
        c["environments"]["mobile"]["tracker"]["scope"] = {"field": "Subsystem", "value": "Frontend"}
        self.assertEqual(scopes.errors(c), ["overlapping-scope: factory.yaml: environments.mobile.tracker and "
                                            "environments.frontend.tracker have the same scope on one tracker"])

    def test_an_unscoped_environment_beside_scoped_ones_is_ambiguous(self):
        c = example()
        del c["environments"]["infra"]["tracker"]["scope"]
        self.assertEqual(scopes.errors(c), ["ambiguous-scope: factory.yaml: environments.infra.tracker has no scope "
                                            "but shares its tracker with backend, frontend, mobile, infra"])

    def test_one_environment_per_tracker_needs_no_scope(self):
        c = example()
        c["environments"] = {"web": {"repositories": ["web"], "tracker": {"adapter": "github-issues", "repository": "o/web"}}}
        self.assertEqual(scopes.errors(c), [])

    def test_an_adapter_no_plugin_provides(self):
        c = example()
        c["environments"]["web"]["tracker"]["adapter"] = "jira"
        self.assertIn("env-tracker: factory.yaml: environments.web.tracker: adapter jira: no selected plugin provides "
                      "this tracker", scopes.errors(c))

    def test_the_project_schema_takes_an_environment_tracker(self):
        schema = json.loads((HERE.parents[1] / "engine/project.schema.json").read_text())
        errs = []
        cfg._check(schema["properties"]["environments"], example()["environments"], "environments", errs, schema)
        self.assertEqual(errs, [])
        bad = {"x": {"tracker": {"adapter": "youtrack", "scope": {"subsystem": "Backend"}}}}
        cfg._check(schema["properties"]["environments"], bad, "environments", errs, schema)
        self.assertTrue(errs)


class IntakePlaceTest(unittest.TestCase):
    """#1361: offline-eval-factory's shape. Four environments narrow LME by Subsystem, `intake` (writes nothing) lists
    o/jbeval on all of LME, and `team` keeps its own GitHub issues. A ticket with no Subsystem is intake's; the
    unscoped place never takes a ticket a scoped place holds, whatever the order of the environments."""
    SCOPED = {"engine": ("o/jbeval", "JBeval"), "harbor": ("o/harbor-infra", "Harbor"),
              "corpus": ("o/corpus", "Datasets"), "docs": ("o/docs", "Documentation")}
    TICKETS = {"LME-1": "JBeval", "LME-2": "Harbor", "LME-3": "Datasets", "LME-1760": None}

    def config(self, intake_first):
        import shutil
        import yaml
        import definition
        root = Path(tempfile.mkdtemp()) / "team"
        self.addCleanup(shutil.rmtree, root.parent)
        shutil.copytree(Path(__file__).parent / "fixtures/v2", root)
        f = yaml.safe_load((root / "factory.yaml").read_text())
        base = {k: v for k, v in f["environments"]["engine"].items() if k != "repositories"}
        envs = {e: [{"repo": r, "write": True, "tracker": {"scope": {"field": "Subsystem", "value": v}}},
                    *({"repo": o, "tracker": "none"} for o, _ in self.SCOPED.values() if o != r)]
                for e, (r, v) in self.SCOPED.items()}
        intake = {"intake": [{"repo": "o/jbeval", "tracker": {"adapter": "youtrack"}},
                             *({"repo": o, "tracker": "none"} for o, _ in list(self.SCOPED.values())[1:])]}
        envs = {**intake, **envs} if intake_first else {**envs, **intake}
        envs["team"] = [{"repo": "o/team", "write": True, "tracker": {"adapter": "github-issues"}},
                        {"repo": "o/jbeval", "tracker": "none"}]
        f["environments"] = {n: {**base, "repositories": rs} for n, rs in envs.items()}
        f["tracker"] = {"label_prefix": "factory:", **YT, "project": "LME"}
        (root / "factory.yaml").write_text(yaml.safe_dump(f, sort_keys=False))
        return definition.config(definition.load(root))

    def scoped(self, config, env):
        yt = FakeYouTrack({n: yt_issue(n, s) for n, s in self.TICKETS.items()})
        gh = FakeGh({76: ["factory:triage"]})
        envs, repos = gate.place_keys(config)

        def build(c, key):
            youtrack = scopes.adapter(envs[key]) == "youtrack"
            return gate.place_systems(c, key, run=gh, **({"yt_run": yt} if youtrack else {}))
        return scopes.ScopedTracker(config, build, gate.run_place(config, env, envs), envs, repos), yt, gh

    def test_scoped_places_come_first_and_keep_their_keys(self):
        for intake_first in (False, True):
            with self.subTest(intake_first=intake_first):
                envs, _ = gate.place_keys(self.config(intake_first))
                self.assertEqual([(k, b.get("scope", {}).get("value"), b["adapter"]) for k, b in envs.items()],
                                 [("place:o/jbeval", "JBeval", "youtrack"), ("place:o/harbor-infra", "Harbor", "youtrack"),
                                  ("place:o/corpus", "Datasets", "youtrack"), ("place:o/docs", "Documentation", "youtrack"),
                                  ("place:o/jbeval#2", None, "youtrack"), ("place:o/team", None, "github-issues")])

    def test_intake_resolves_each_ticket_to_its_scoped_place_else_the_unscoped_one(self):
        for intake_first in (False, True):
            with self.subTest(intake_first=intake_first):
                s, yt, gh = self.scoped(self.config(intake_first), "intake")
                self.assertEqual({n: s.env_of(n) for n in [*self.TICKETS, 76]},
                                 {"LME-1": "place:o/jbeval", "LME-2": "place:o/harbor-infra", "LME-3": "place:o/corpus",
                                  "LME-1760": "place:o/jbeval#2", 76: "place:o/team"})
                self.assertEqual(s.systems("place:o/harbor-infra")[1].repo, "o/harbor-infra")  # LME-2's PRs
                self.assertEqual(s.env_of_repository("o/jbeval"), "place:o/jbeval")  # a jbeval PR: the scoped place
                s.apply({"ticket": "LME-1760", "kind": "stage", "value": "blocked", "mirror": False, "run_id": "r"})
                self.assertTrue(yt.writes())
                self.assertTrue(all("LME-1760" in path for _, path, _ in yt.writes()), yt.writes())
                self.assertEqual(gh.writes(), [])

    def test_a_swept_ticket_is_its_scoped_places_before_the_unscoped_one(self):
        s, _, _ = self.scoped(self.config(True), "intake")
        found = {"place:o/jbeval": ["LME-1"], "place:o/harbor-infra": ["LME-2"], "place:o/jbeval#2": [*self.TICKETS]}
        for key in s.envs:
            s.systems(key)[0].sweep_tickets = lambda *a, key=key, **k: found.get(key, [])
        self.assertEqual(s.sweep_tickets(["ready"]), ["LME-1", "LME-2", "LME-3", "LME-1760"])
        self.assertEqual({n: s._env_of[n] for n in self.TICKETS},
                         {"LME-1": "place:o/jbeval", "LME-2": "place:o/harbor-infra", "LME-3": "place:o/jbeval#2",
                          "LME-1760": "place:o/jbeval#2"})  # LME-3: no Datasets sweep found it, so intake's

    def test_each_writable_environment_takes_only_its_scoped_place(self):
        config = self.config(True)
        for env, (repo, _) in self.SCOPED.items():
            with self.subTest(env=env):
                s, _, _ = self.scoped(config, env)
                self.assertEqual(s.candidates(), [f"place:{repo}"])
                self.assertEqual(s.envs[f"place:{repo}"]["scope"]["value"], self.SCOPED[env][1])
                self.assertIsNone(s.env_of("LME-1760"))  # no Subsystem: skipped outside intake
        s, _, _ = self.scoped(config, "harbor")
        self.assertEqual((s.env_of("LME-2"), s.env_of("LME-1")), ("place:o/harbor-infra", None))
        s, _, _ = self.scoped(config, "team")
        self.assertEqual((s.candidates(), s.env_of(76), s.env_of("LME-1760")), (["place:o/team"], "place:o/team", None))

    def test_deploy_reads_the_extra_place_once(self):
        import deploy
        config = self.config(True)
        self.assertEqual(deploy.github_homes(config), ["o/team"])
        self.assertEqual(deploy.youtrack_repositories(config), {"o/jbeval", "o/harbor-infra", "o/corpus", "o/docs"})

    def test_state_rules_handle_a_ticket_once_on_its_scoped_place(self):
        from factory_plugins.youtrack import state
        seen, calls = set(), []
        for place, listed in (("harbor", ["LME-2"]), ("intake", ["LME-2", "LME-1760"])):
            store = mock.Mock(state_mirror_field="State", bots=set(), tickets_by_stage=lambda *a, listed=listed, **k: listed,
                              last_field_change=lambda n, f, place=place: calls.append((place, n)) or ("Obsolete", "person"),
                              issue=lambda n: {"attrs": ["factory:ready"]})
            state.apply_rules(mock.Mock(tickets=store), {"Obsolete": "stop"}, {"ready": "factory:ready"}, dry_run=True, seen=seen)
        self.assertEqual(calls, [("harbor", "LME-2"), ("intake", "LME-1760")])


if __name__ == "__main__":
    unittest.main()


class RunPlaceTest(unittest.TestCase):  # #1330: a copy acts only on its own place's tickets
    CONFIG = {"repositories": [{"name": "a", "url": "https://github.com/o/a"}, {"name": "b", "url": "https://github.com/o/b"}],
              "environments": {"b-env": {"writable": ["b"]}, "intake": {"writable": []}}}
    KEYS = {"place:o/a": {}, "place:o/b": {}}

    def test_an_environment_owns_its_writable_repositorys_place(self):
        self.assertEqual(gate.run_place(self.CONFIG, "b-env", self.KEYS), "place:o/b")

    def test_an_environment_that_writes_nothing_sees_every_place(self):
        self.assertIsNone(gate.run_place(self.CONFIG, "intake", self.KEYS))
        self.assertIsNone(gate.run_place(self.CONFIG, None, self.KEYS))

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Tests for factory/definition.py (ADR-0021). Run: uv run factory/tests/test_definition.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import definition  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/v2"
MAIN, OTHER = "zaharchenko-evgeny/slop-factory", "zaharchenko-evgeny/slop-eval-factory"



def live_warnings(defn):
    """The warnings, less `deprecated-key`: the Slop fixture keeps its ADR-0024 keys until slop-eval-factory#92."""
    return [w for w in definition.warnings(defn) if not w.startswith("deprecated-key:")]


def ok_lines(stdout):
    return "\n".join(line for line in stdout.strip().splitlines() if not line.startswith("warning: deprecated-key:"))

class DefinitionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "team"
        shutil.copytree(FIXTURE, self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def edit(self, name, change):
        """Apply `change(data)` to factory.yaml or workflow.yaml of the copy."""
        path = self.root / name
        data = yaml.safe_load(path.read_text())
        change(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False))

    def errors(self):
        return definition.errors(definition.load(self.root))

    def assertError(self, prefix):
        errs = self.errors()
        self.assertTrue(any(e.startswith(prefix) for e in errs), errs)

    def test_fixture_is_valid(self):
        self.assertTrue(definition.present(self.root))
        self.assertEqual(self.errors(), [])

    # --- ADR-0027: repository tracker, vcs and write; node environments ----------------------------------------
    def _envs(self, f, **envs):
        """Replace the environments with `envs`: {name: [(repo, {keys})]}, keeping the engine's other settings."""
        base = {k: v for k, v in f["environments"]["engine"].items() if k != "repositories"}
        f["environments"] = {n: {**base, "repositories": [{"repo": r, **extra} for r, extra in rs]} for n, rs in envs.items()}

    def _node_envs(self, w, name, *envs):
        w["nodes"][name]["environments"] = list(envs)

    def test_repository_blocks_layer_over_environment_and_top(self):
        self.edit("factory.yaml", lambda f: (
            self._envs(f, engine=[(MAIN, {"write": True}),
                                  (OTHER, {"tracker": {"adapter": "youtrack", "scope": {"field": "Subsystem", "value": "S3"}}})]),
            f["environments"]["engine"].update(tracker={"adapter": "youtrack", "url": "https://yt", "project": "LME"})))
        defn = definition.load(self.root)
        main, other = definition.repositories(defn, "engine")
        self.assertEqual((main["repo"], main["write"], other["write"]), (MAIN, True, False))
        self.assertEqual((main["tracker"]["adapter"], main["tracker"]["project"], main["tracker"].get("scope")), ("youtrack", "LME", None))
        self.assertEqual(other["tracker"]["scope"], {"field": "Subsystem", "value": "S3"})
        self.assertNotIn("repository", other["tracker"])  # the github system key does not cross to youtrack
        self.assertEqual(main["vcs"]["adapter"], "github")

    def test_github_issues_place_defaults_to_the_repository(self):
        self.edit("factory.yaml", lambda f: self._envs(f, engine=[(MAIN, {"write": True}), (OTHER, {"token": "T"})]))
        defn = definition.load(self.root)
        places = [definition.contract(defn, "tracker", r["tracker"]).place(r["tracker"], r["repo"])
                  for r in definition.repositories(defn, "engine")]
        self.assertEqual(places, [f"github-issues:{MAIN}", f"github-issues:{OTHER}"])  # top `tracker.repository` stays out
        self.assertEqual(definition._repository_errors(defn), [])

    def test_context_repository_has_no_tracker(self):
        self.edit("factory.yaml", lambda f: self._envs(f, engine=[(MAIN, {"write": True}), (OTHER, {"tracker": "none"})]))
        self.assertIsNone(definition.repositories(definition.load(self.root), "engine")[1]["tracker"])

    def test_copies_per_environment_and_listened_repository(self):
        self.edit("factory.yaml", lambda f: self._envs(
            f, engine=[(MAIN, {"write": True}), (OTHER, {"token": "T"})], team=[(OTHER, {"write": True}), (MAIN, {"tracker": "none"})],
            intake=[(MAIN, {}), (OTHER, {"token": "T"})]))
        self.edit("workflow.yaml", lambda w: (self._node_envs(w, "implement-small", "engine", "team"),
                                              self._node_envs(w, "intake", "intake")))
        defn = definition.load(self.root)
        dev = definition.copies(defn, "implement-small")
        self.assertEqual([(c["env"], c["main"], c["write"], c["additional"]) for c in dev],
                         [("engine", MAIN, True, [OTHER]), ("team", OTHER, True, [MAIN])])
        self.assertEqual([(c["env"], c["main"], c["write"]) for c in definition.copies(defn, "intake")],
                         [("intake", MAIN, False), ("intake", OTHER, False)])  # read-only: every ticket place
        self.assertEqual(definition._repository_errors(defn), [])

    def test_schedule_copies_once_per_environment(self):
        self.edit("factory.yaml", lambda f: self._envs(f, engine=[(MAIN, {"write": True})], team=[(MAIN, {}), (OTHER, {})]))
        self.edit("workflow.yaml", lambda w: self._node_envs(w, "reconcile", "engine", "team"))
        got = [(c["env"], c["repo"], c["main"], c["write"]) for c in definition.copies(definition.load(self.root), "reconcile")]
        self.assertEqual(got, [("engine", None, MAIN, True), ("team", None, MAIN, False)])  # home: writable, else first

    def test_youtrack_copy_has_no_home_repository(self):
        yt = {"adapter": "youtrack", "url": "https://yt", "project": "LME", "scope": {"field": "Subsystem", "value": "S3"}}
        self.edit("factory.yaml", lambda f: self._envs(f, b=[(OTHER, {"write": True, "tracker": yt}), (MAIN, {"tracker": "none"})]))
        self.edit("workflow.yaml", lambda w: self._node_envs(w, "implement-small", "b"))
        defn = definition.load(self.root)
        (c,) = definition.copies(defn, "implement-small")
        self.assertEqual((c["main"], c["write"], c["place"]), (OTHER, True, "youtrack:https://yt/LME|Subsystem=S3"))

    def test_node_hears_twice_is_refused(self):
        self.edit("factory.yaml", lambda f: self._envs(f, engine=[(MAIN, {})], all=[(MAIN, {}), (OTHER, {})]))
        self.edit("workflow.yaml", lambda w: self._node_envs(w, "intake", "engine", "all"))
        self.assertError(f"node-hears-twice: nodes.intake: environments all, engine both listen to github-issues:{MAIN}")

    def test_place_in_two_repositories_is_refused(self):
        yt = {"adapter": "youtrack", "url": "https://yt", "project": "LME"}
        self.edit("factory.yaml", lambda f: self._envs(f, engine=[(MAIN, {"write": True, "tracker": yt}), (OTHER, {"tracker": yt})]))
        self.assertError("place-in-two-repositories: youtrack:https://yt/LME: holds the tickets of")

    def test_unknown_or_invalid_repository_blocks_are_refused(self):
        self.edit("factory.yaml", lambda f: self._envs(f, engine=[(MAIN, {"write": "yes", "tracker": {"adapter": "jira"}}),
                                                                  (OTHER, {"tracker": {"adapter": "youtrack"}})]))
        errs = self.errors()
        for want in (f"bad-write: environments.engine: {MAIN}", f"unknown-tracker: environments.engine: {MAIN}: jira",
                     f"youtrack-project: environments.engine: {OTHER}"):
            with self.subTest(want=want):
                self.assertTrue(any(e.startswith(want) for e in errs), errs)

    def _relabel(self, prefix):
        """Rename every `factory:` trigger label of the copy's workflow to `prefix`."""
        def workflow(w):
            for n in w["nodes"].values():
                for t in n.get("triggers") or []:
                    if isinstance(t, dict) and "label" in t:
                        t["label"] = t["label"].replace("factory:", prefix, 1)
                for oc in (n.get("outcomes") or {}).values():
                    for e in (oc or {}).get("effects") or []:
                        for k in ("label", "unlabel"):
                            if isinstance(e.get(k), str):
                                e[k] = e[k].replace("factory:", prefix, 1)
        self.edit("workflow.yaml", workflow)

    def test_tracker_label_prefix_compiles_stage_labels_and_owned_prefix(self):
        import deploy
        for where in ("top", "environment", "repository"):
            with self.subTest(where=where):
                shutil.rmtree(self.root)
                shutil.copytree(FIXTURE, self.root)
                self._relabel("slop:")

                def factory(f):
                    f["tracker"].pop("label_prefix", None)
                    env = f["environments"]["engine"]
                    entry = env["repositories"][0]
                    if where == "top":
                        f["tracker"]["label_prefix"] = "slop:"
                    elif where == "environment":
                        env["tracker"] = {"adapter": "github-issues", "label_prefix": "slop:"}
                    else:
                        entry["tracker"] = {"adapter": "github-issues", "label_prefix": "slop:"}
                    for spec in f["environments"].values():
                        for r in spec["repositories"][1:]:
                            r["token"] = "T"
                self.edit("factory.yaml", factory)
                defn = definition.load(self.root)
                if where != "top":  # only the default environment's primary repository decides
                    defn["factory"]["environments"].pop("team", None)
                self.assertEqual(definition.label_prefix(defn), "slop:")
                self.assertEqual(self.errors(), [])
                conf = definition.config(defn)
                self.assertEqual(conf["tracker"]["stages"]["ready"], "slop:ready")
                self.assertTrue(all(v.startswith("slop:") for v in conf["tracker"]["stages"].values()))
                self.assertIn("slop:", deploy.owned_label_prefixes(conf))
                self.assertNotIn("factory:", deploy.owned_label_prefixes(conf))

    def test_workflow_label_prefix_is_a_fallback_that_warns_about_the_move(self):
        """Table: where the prefix is set -> the compiled prefix and whether `validate` warns about the move."""
        moved = "deprecated-key: workflow.yaml stages.label_prefix moved to tracker.label_prefix in factory.yaml"
        table = [(None, None, "factory:", False), (None, "work:", "work:", True), ("slop:", None, "slop:", False),
                 ("slop:", "work:", "slop:", True)]  # the tracker key wins over the fallback
        for tracker, workflow_key, want, warns in table:
            with self.subTest(tracker=tracker, workflow=workflow_key):
                shutil.rmtree(self.root)
                shutil.copytree(FIXTURE, self.root)
                self.edit("factory.yaml", lambda f: f["tracker"].pop("label_prefix", None) if tracker is None
                          else f["tracker"].update(label_prefix=tracker))
                if workflow_key:
                    self.edit("workflow.yaml", lambda w: w["stages"].update(label_prefix=workflow_key))
                defn = definition.load(self.root)
                self.assertEqual(definition.label_prefix(defn), want)
                self.assertEqual(moved in definition.warnings(defn), warns)

    def test_empty_or_non_string_label_prefix_is_an_error(self):
        for bad in ("", 7, ["slop:"]):
            with self.subTest(bad=bad):
                self.edit("factory.yaml", lambda f: f["tracker"].update(label_prefix=bad))
                self.assertIn("bad-label-prefix: factory.yaml: `tracker.label_prefix` is a non-empty string", self.errors())

    def test_deprecated_keys_and_several_writable_are_warned(self):
        self.edit("factory.yaml", lambda f: self._envs(f, engine=[(MAIN, {"write": True}), (OTHER, {"write": True})]))
        warns = definition.warnings(definition.load(self.root))
        self.assertIn(f"several-writable: environments.engine: {MAIN}, {OTHER}: the air surface allows one writable "
                      "repository per environment", warns)
        self.assertTrue(any(w.startswith("deprecated-key: nodes.implement-small: `writes`") for w in warns), warns)
        self.assertTrue(any(w.startswith("deprecated-key: agents.") and "`access`" in w for w in warns), warns)

    def test_migrated_slop_definition_validates_clean(self):
        """The Slop definition in the ADR-0027 form (slop-eval-factory#92): no errors, no warnings, the copies below."""
        def factory(f):
            self._envs(f, engine=[(MAIN, {"write": True}), (OTHER, {"token": "T"})],
                       team=[(OTHER, {"write": True}), (MAIN, {"tracker": "none"})], intake=[(MAIN, {}), (OTHER, {"token": "T"})])
            f["tracker"].pop("repositories", None)
            for spec in f["agents"].values():
                spec.pop("access", None)

        def workflow(w):
            (w.get("settings") or {}).pop("environment", None)
            for name, spec in w["nodes"].items():
                for k in ("writes", "listens", "environment"):
                    spec.pop(k, None)
                spec["environments"] = ["intake"] if name == "intake" else ["engine", "team"]
        self.edit("factory.yaml", factory)
        self.edit("workflow.yaml", workflow)
        defn = definition.load(self.root)
        self.assertEqual((definition.errors(defn), definition.warnings(defn)), ([], []))

        def comments_on(f):
            f["plugins"] = ["comments"]
            f["agents"]["dev"]["mentionable"] = True
        self.edit("factory.yaml", comments_on)
        defn = definition.load(self.root)
        self.assertEqual([(c["env"], c["main"], c["write"]) for c in definition.copies(defn, "reply-dev")],
                         [("engine", MAIN, True), ("team", OTHER, True)])  # a generated node takes its agent's environments

    def test_config_compiles_ticket_places(self):
        yt = {"adapter": "youtrack", "url": "https://yt", "project": "LME", "scope": {"field": "Subsystem", "value": "S3"}}
        self.edit("factory.yaml", lambda f: self._envs(f, engine=[(MAIN, {"write": True}), (OTHER, {})],
                                                       b=[("o/c", {"write": True, "tracker": yt}), (MAIN, {"tracker": "none"})]))
        c = definition.config(definition.load(self.root))
        self.assertTrue(c["one_repository_per_ticket"])
        self.assertEqual(c["tracker"]["repositories"], [MAIN, OTHER])  # the GitHub Issues homes, the tracker first
        self.assertEqual([(p["repository"], p["tracker"]["project"], p["repositories"]) for p in c["ticket_places"]],
                         [("o/c", "LME", ["o/c", MAIN])])
        self.assertEqual(c["environments"]["b"]["writable"], ["c"])
        self.assertNotIn("tracker", c["environments"]["b"])  # a place, not a #1069 project environment

    def test_workflow_environments_list_is_the_default(self):
        self.edit("factory.yaml", lambda f: self._envs(f, engine=[(MAIN, {"write": True})], team=[(OTHER, {"write": True})]))

        def workflow(w):
            w["settings"].pop("environment", None)
            w["settings"]["environments"] = ["engine", "team"]
            w["nodes"]["intake"]["environment"] = "team"
        self.edit("workflow.yaml", workflow)
        defn = definition.load(self.root)
        self.assertEqual((definition.node_environments(defn, "fix"), definition.node_environments(defn, "intake")),
                         (["engine", "team"], ["team"]))
        self.assertEqual(definition.default_environment(defn), "engine")

    def test_per_repository_tracker_keys_are_refused(self):
        """ADR-0027: the repository's tracker block says where its tickets are; `repository_field`, `projects` and
        `repositories` would repeat it per repository."""
        for where, put in (("tracker.repository_field", lambda f: f["tracker"].update(repository_field="Repository")),
                           ("tracker.projects", lambda f: f["tracker"].update(projects={"LME": MAIN})),
                           ("tracker.repositories", lambda f: f["tracker"].update(repositories=[MAIN]))):
            with self.subTest(where=where):
                self.setUp()
                self.edit("factory.yaml", lambda f: (self._envs(f, engine=[(MAIN, {"write": True})]), put(f)))
                self.assertError(f"removed-key: {where}")

    def test_removed_repository_keys_are_reported(self):
        """ADR-0027: an environment repository's `main` and `token_env` are gone (`write` is back: its writable repository)."""
        for key in ("main", "token_env"):
            with self.subTest(key=key):
                self.edit("factory.yaml", lambda f: f["environments"]["engine"]["repositories"][1].update({key: True}))
                self.assertError(f"removed-key: environments.engine: zaharchenko-evgeny/slop-eval-factory: `{key}`")

    def test_role_secret_reaches_the_environments_of_its_nodes_only(self):
        self.edit("factory.yaml", lambda f: f["roles"]["router"].update(
            env={"ISSUES_TOKEN": {"required": True, "secret": True, "description": "Issues read/write"}}))
        self.assertEqual(self.errors(), [])
        project, _ = definition.to_project(definition.load(self.root))
        engine, team = project["environments"]["engine"], project["environments"]["team"]
        self.assertIn("ISSUES_TOKEN", engine["secrets"])  # intake (router) runs in the default environment
        self.assertEqual(engine["secret_help"]["ISSUES_TOKEN"], "Issues read/write")
        self.assertNotIn("ISSUES_TOKEN", team["secrets"])

    def test_assignment_effects_validate_and_compile_unchanged(self):
        effects = [{"assign": "owner", "on": "pr"}, {"unassign": "all"}]
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["ready"].update(effects=effects))
        self.assertEqual(self.errors(), [])
        _, workflow = definition.to_project(definition.load(self.root))
        self.assertEqual(workflow["nodes"]["intake"]["outcomes"]["ready"]["effects"], effects)

    def test_assignment_field_form_validates(self):
        effects = [{"assign": {"field": "assignee"}, "on": "pr"}]
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["ready"].update(effects=effects))
        self.assertEqual(self.errors(), [])

    def test_assignment_effect_rejects_other_dicts(self):
        for value in ({"fieldd": "x"}, {"field": "x", "extra": 1}, {"field": 3}):
            with self.subTest(value=value):
                self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["ready"].update(
                    effects=[{"assign": value}]))
                self.assertError("bad-effect: nodes.intake.ready: assign")

    def test_assignment_effect_rejects_non_string_values(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["ready"].update(
            effects=[{"assign": 3}]))
        self.assertError("bad-effect: nodes.intake.ready: assign")

    def test_missing_owner_fails(self):
        self.edit("factory.yaml", lambda f: f.pop("owner"))
        self.assertIn("missing-owner: factory.yaml: `owner` (the GitHub login a person-wait is assigned to)", self.errors())

    def test_owner_reaches_the_descriptor(self):
        descriptor, _ = definition.to_project(definition.load(self.root))
        self.assertEqual(descriptor["owner"], "zaharchenko-evgeny")
        self.assertNotIn("notify", descriptor)

    def test_fixture_warns_only_about_labels_a_person_adds(self):
        self.assertEqual(live_warnings(definition.load(self.root)), [])
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["ready"]["effects"]
                  .append({"label": "factory:nobody"}))
        self.assertEqual(live_warnings(definition.load(self.root)),
                         ["unwatched-label: factory:nobody: no node triggers on it"])

    def test_unknown_agent(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"].update(agent="nobody"))
        self.assertError("unknown-agent: nodes.intake")

    def test_unknown_environment(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["retro"].update(environment="nowhere"))
        self.assertError("unknown-environment: nodes.retro")

    def test_unknown_model(self):
        self.edit("factory.yaml", lambda f: f["agents"]["dev"].update(model="gpt-0"))
        self.assertError("unknown-model: agents.dev")

    def test_unknown_role(self):
        self.edit("factory.yaml", lambda f: f["agents"]["dev"].update(role="painter"))
        self.assertError("unknown-role: agents.dev")

    def test_unknown_skill(self):
        self.edit("factory.yaml", lambda f: f["roles"]["developer"]["skills"].append("juggling"))
        self.assertError("unknown-skill: roles.developer: juggling")

    def test_sourced_skill_needs_sha256(self):
        self.edit("factory.yaml", lambda f: f["skills"]["tdd"].pop("sha256"))
        self.assertError("unpinned-skill: skills.tdd")

    def test_inline_role_skill(self):
        self.edit("factory.yaml", lambda f: f["roles"]["reviewer"]["skills"].append(
            {"adr-reading": {"path": "roles/reviewer/skills/adr-reading"}}))
        self.assertEqual(self.errors(), [])
        self.edit("factory.yaml", lambda f: f["roles"]["reviewer"]["skills"].append({"broken": {}}))
        self.assertError("bad-skill: roles.reviewer: broken")

    def test_unknown_webhook(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["notify"]["triggers"].append({"webhook": "nope"}))
        self.assertError("unknown-webhook: nodes.notify: nope")

    def test_event_node_in_any_environment_hears_its_ticket_repository(self):
        """ADR-0024: a copy's main is the repository it listens to, whatever the first checkout of its environment."""
        for trigger in ({"label": "factory:needs-human"}, {"pr": "merged"}, {"ci": "failed"}):
            with self.subTest(trigger=trigger):
                self.edit("workflow.yaml", lambda w: w["nodes"]["notify"].update(environment="team", triggers=[trigger]))
                self.assertFalse([e for e in self.errors() if e.startswith("deaf-trigger")])

    def test_scheduled_node_in_any_environment_is_fine(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["retro"].update(environment="team"))
        self.assertEqual(self.errors(), [])

    def test_pr_label_node_in_pr_repository_environment_is_fine(self):
        def add_pr_repository(factory):
            factory["environments"]["engine"]["repositories"].append({"repo": "owner/web"})
            factory["environments"]["web"] = {"repositories": [{"repo": "owner/web"}]}
        self.edit("factory.yaml", add_pr_repository)
        self.edit("workflow.yaml", lambda w: w["nodes"]["retro"].update(
            environment="web", writes=["owner/web"], triggers=["manual"], outcomes={**w["nodes"]["retro"]["outcomes"],
                                         "proposed": {"effects": [{"label": "factory:knowledge-review", "on": "pr"},
                                                                      {"label": "factory:review-low", "on": "pr"}]}}))
        self.edit("workflow.yaml", lambda w: w["nodes"]["review-low"].update(
            environment="web", triggers=[{"label": "factory:review-low"}]))
        self.edit("workflow.yaml", lambda w: w["nodes"]["knowledge-review"].update(environment="web"))
        self.assertEqual(self.errors(), [])

    def _full_nodes(self):
        w = definition.load(self.root)
        agents = w["factory"]["agents"]
        return [n for n, spec in w["workflow"]["nodes"].items() if (agents.get(spec.get("agent")) or {}).get("access") == "full" and not spec.get("generated")]

    def test_environment_without_main_is_valid(self):
        self.edit("workflow.yaml", lambda w: [w["nodes"][n].update(writes=[MAIN]) for n in self._full_nodes()])
        self.assertEqual(self.errors(), [])

    def test_environment_without_main_compiles(self):
        self.edit("workflow.yaml", lambda w: [w["nodes"][n].update(writes=[MAIN]) for n in self._full_nodes()])
        defn = definition.load(self.root)
        self.assertEqual(definition.errors(defn), [])
        project, _ = definition.to_project(defn)
        self.assertEqual([r["name"] for r in project["repositories"] if r["role"] == "primary"], ["slop-factory"])
        self.assertEqual(definition.nodes(defn)["environments"]["engine"]["repository"], "primary")

    def test_environment_without_repositories_is_refused(self):
        def empty(f):
            f["environments"]["engine"]["repositories"] = []
            f["tracker"].pop("repositories", None)
        self.edit("factory.yaml", empty)
        self.assertError("empty-environment: environments.engine")

    def test_malformed_writes_is_bad_writes_not_a_crash(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["implement-small"].update(writes=7))
        self.assertError("bad-writes: nodes.implement-small")

    def test_malformed_tracker_repositories_is_reported_not_a_crash(self):
        self.edit("factory.yaml", lambda f: f["tracker"].update(repositories=7))
        self.assertError("bad-tracker-repositories: factory.yaml")

    def _two_ticket_repositories(self):
        self.edit("factory.yaml", lambda f: f["tracker"].update(repositories=[MAIN, OTHER]))
        return definition.load(self.root)

    def test_listens_defaults_to_every_ticket_repository(self):
        defn = self._two_ticket_repositories()
        self.assertEqual(definition.node_listens(defn, "implement-small"), [MAIN, OTHER])
        self.assertEqual(definition.node_listens(defn, "human"), [])
        self.assertEqual(definition.tracker_repositories(definition.load(self.root)), [MAIN, OTHER])

    def test_youtrack_projects_link_the_ticket_repositories(self):
        def youtrack(f):
            f["tracker"] = {"adapter": "youtrack", "url": "https://y", "project": "API", "projects": {"API": MAIN, "WEB": OTHER}}
        self.edit("factory.yaml", youtrack)
        defn = definition.load(self.root)
        self.assertEqual(definition.tracker_repositories(defn), [MAIN, OTHER])
        self.assertEqual(definition.node_listens(defn, "implement-small"), [MAIN, OTHER])
        self.assertFalse([e for e in definition.errors(defn) if e.startswith("deaf-trigger")])  # YouTrack wakes any repository

    def test_mainless_environment_of_several_repositories_needs_no_environment_per_repository(self):
        def two_repos(f):
            f["environments"]["engine"]["repositories"] = [{"repo": MAIN}, {"repo": "o/web"}]
            f["tracker"]["repositories"] = [MAIN, "o/web"]
        self.edit("factory.yaml", two_repos)
        self.edit("workflow.yaml", lambda w: [w["nodes"][n].update(writes="all") for n in self._full_nodes()
                                              if not any("schedule" in t for t in w["nodes"][n]["triggers"] if isinstance(t, dict))])
        self.assertEqual(self.errors(), [])

    def test_listens_tracker_is_the_first_ticket_repository(self):
        self._two_ticket_repositories()
        self.edit("workflow.yaml", lambda w: w["nodes"]["implement-small"].update(listens="tracker"))
        self.assertEqual(definition.node_listens(definition.load(self.root), "implement-small"), [MAIN])

    def test_listens_outside_ticket_repositories_is_bad_listens(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"].update(listens=["o/elsewhere"]))
        self.assertError("bad-listens: nodes.intake: o/elsewhere")

    def test_tracker_repository_must_be_in_an_environment(self):
        self.edit("factory.yaml", lambda f: f["tracker"].update(repositories=[MAIN, "o/elsewhere"]))
        self.assertError("tracker-repository: factory.yaml: tracker repository o/elsewhere")

    def test_event_node_writing_an_unheard_repository_is_refused(self):
        node = self._full_nodes()[0]
        self.edit("workflow.yaml", lambda w: w["nodes"][node].update(writes=[OTHER]))
        self.assertError("writes-not-heard: nodes.%s: writes %s" % (node, OTHER))
        self._two_ticket_repositories()
        self.assertEqual(self.errors(), [])

    def test_event_node_with_empty_listens_writing_a_repository_is_refused(self):
        node = self._full_nodes()[0]
        self.edit("workflow.yaml", lambda w: w["nodes"][node].update(listens=[], writes=[OTHER]))
        self.assertError("writes-not-heard: nodes.%s: writes %s" % (node, OTHER))

    def test_scheduled_node_writing_two_repositories_is_refused(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["implement-small"].update(writes="all", triggers=[{"schedule": "0 * * * *"}]))
        self.assertError("schedule-writes-several: nodes.implement-small")
        self.edit("workflow.yaml", lambda w: w["nodes"]["implement-small"].update(writes=[MAIN]))
        self.assertFalse([e for e in self.errors() if e.startswith("schedule-writes")])

    def test_writes_all_expands_to_environment_repositories(self):
        node = self._full_nodes()[0]
        self.edit("workflow.yaml", lambda w: w["nodes"][node].update(writes="all"))
        defn = definition.load(self.root)
        env = definition._parts(defn, node)[3]
        self.assertEqual(definition.node_writes(defn, node), [definition.full_name(r["repo"]) for r in env["repositories"]])
        self.assertEqual(definition.node_writes(defn, "intake"), [])

    def test_writes_outside_environment_is_bad_writes(self):
        node = self._full_nodes()[0]
        self.edit("workflow.yaml", lambda w: w["nodes"][node].update(writes=["o/elsewhere"]))
        self.assertError("bad-writes: nodes.%s: o/elsewhere" % node)

    def test_writes_with_read_only_agent_is_refused(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"].update(writes="all"))
        self.assertError("writes-read-only: nodes.intake")

    def test_full_access_node_without_writes_is_refused(self):
        node = self._full_nodes()[0]
        self.edit("workflow.yaml", lambda w: w["nodes"][node].pop("writes"))
        self.assertError("no-writes: nodes.%s" % node)

    def test_role_needs_body_and_body_needs_role(self):
        (self.root / "roles/router/role.md").unlink()
        self.assertError("missing-role-body: roles.router")
        (self.root / "roles/painter").mkdir()
        (self.root / "roles/painter/role.md").write_text("Paint.\n")
        self.assertError("unlisted-role: roles/painter")

    def test_else_names_an_outcome(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["automerge"]["outcomes"]["merge"].update({"else": "nothing"}))
        self.assertError("unknown-else: nodes.automerge.merge: nothing")

    def test_guard_reads_known_facts(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["merge"]["outcomes"]["merged"].update({"if": {"colour": "red"}}))
        self.assertError("unknown-guard: nodes.merge.merged: colour")
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["ready"]["effects"].append({"set-field": "risk"}))
        self.assertError("unknown-effect: nodes.intake.ready: set-field")

    def test_undefined_template_name(self):
        body = self.root / "roles/router/role.md"
        body.write_text(body.read_text() + "\nLimit {{ settings.nope }}\n")
        self.assertError("render: nodes.intake")

    MCP = {"youtrack": {"connector": "internal-youtrack"},
           "idea": {"command": "npx", "args": ["-y", "@jetbrains/mcp-proxy"], "env": {"IDE_PORT": "${IDE_PORT}"}},
           "docs": {"url": "https://mcp.example/sse", "headers": {"Authorization": "Bearer ${DOCS_TOKEN}"}}}

    def add_mcp(self, layer="roles.developer", servers=None, env=("IDE_PORT", "DOCS_TOKEN")):
        """Put `servers` on `layer` ("roles.developer", "agents.dev", "defaults") and declare `env` in defaults."""
        def change(f):
            f["defaults"].setdefault("env", {}).update({v: {"required": False} for v in env})
            top, _, name = layer.partition(".")
            (f["defaults"] if top == "defaults" else f[top][name])["mcp"] = servers if servers is not None else self.MCP
        self.edit("factory.yaml", change)

    def test_mcp_merges_by_key_across_layers_and_null_removes(self):
        self.add_mcp("defaults", {"glean": {"connector": "internal-glean"}, "docs": {"url": "https://old.example/sse"}})
        self.add_mcp("roles.developer")
        self.edit("factory.yaml", lambda f: f["agents"]["dev"].update(mcp={"slack": {"connector": "slack"}, "docs": None}))
        self.edit("workflow.yaml", lambda w: w["nodes"]["implement-medium"].update(mcp={"glean": None}))
        defn = definition.load(self.root)
        self.assertEqual(sorted(definition.mcp(defn, "implement-medium")), ["idea", "slack", "youtrack"])
        self.assertEqual(sorted(definition.mcp(defn, "implement-small")), ["docs", "glean", "idea", "youtrack"])  # other agent
        self.assertEqual(definition.mcp(defn, "implement-small")["docs"], self.MCP["docs"])   # role replaces default's entry whole
        self.assertEqual(definition.mcp_connectors(definition.mcp(defn, "implement-small")), ["internal-glean", "internal-youtrack"])
        self.assertEqual(sorted(definition.mcp_custom(definition.mcp(defn, "implement-small"))), ["docs", "idea"])

    def test_mcp_connectors_reach_the_compiled_node(self):
        self.add_mcp()
        _, wf = definition.to_project(definition.load(self.root))
        self.assertEqual(wf["nodes"]["implement-medium"]["mcp"], ["internal-youtrack"])
        self.assertNotIn("mcp", wf["nodes"]["intake"])

    def test_mcp_valid_servers_pass_validate(self):
        self.add_mcp()
        self.assertEqual(self.errors(), [])

    def test_mcp_validate_unknown_connector(self):
        self.add_mcp(servers={"x": {"connector": "nope"}})
        self.assertError("unknown-mcp-connector: roles.developer.mcp.x: nope")
        self.edit("factory.yaml", lambda f: f.setdefault("targets", {}).update(air={"mcp_connectors": ["nope"]}))
        self.assertEqual(self.errors(), [])   # the list in `targets.air.mcp_connectors` replaces the default

    def test_mcp_validate_undeclared_env(self):
        self.add_mcp(env=("IDE_PORT",))
        self.assertError("mcp-undeclared-env: roles.developer.mcp.docs: ${DOCS_TOKEN}")
        self.add_mcp()   # now declared
        self.assertEqual(self.errors(), [])
        self.edit("workflow.yaml", lambda w: w["nodes"]["fix"].update(mcp={"d": {"url": "https://x.example/${HOST}"}}))
        self.assertError("mcp-undeclared-env: nodes.fix.mcp.d: ${HOST}")
        self.edit("workflow.yaml", lambda w: w["nodes"]["fix"].update(env={"HOST": {"required": False}}))   # the node's own env
        self.assertEqual(self.errors(), [])

    def test_mcp_validate_literal_secret(self):
        self.add_mcp(servers={"docs": {"url": "https://x.example", "headers": {"Authorization": "Bearer abc123"}},
                              "idea": {"command": "x", "env": {"PLAIN": "ghp_abcdef123456"}}})
        errs = self.errors()
        self.assertIn("mcp-literal-secret: roles.developer.mcp.docs.headers.Authorization: use a ${VAR} reference", errs)
        self.assertIn("mcp-literal-secret: roles.developer.mcp.idea.env.PLAIN: use a ${VAR} reference", errs)

    def test_mcp_validate_mixed_entry(self):
        self.add_mcp(servers={"x": {"connector": "slack", "command": "npx"}})
        self.assertError("mcp-mixed-entry: roles.developer.mcp.x")

    def test_nodes_have_no_prompt(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"].update(prompt="Scope it."))
        self.assertError("node-prompt: nodes.intake")

    def test_compile_lists_the_writable_repositories_of_each_environment(self):
        import definition
        envs = definition.to_project(definition.load(self.root))[0]["environments"]
        # ADR-0024: what the environment's nodes write (developers the engine, retro the knowledge base)
        self.assertEqual({name: env["writable"] for name, env in envs.items()},
                         {"engine": ["slop-factory"], "knowledge": ["slop-eval-factory"], "team": []})

    def test_repository_token_is_a_secret_name_and_optional(self):
        """#1236: (the second repository's keys, the error). Every listed repository is checked out, where Air's token
        writes it (#1246), so a ticket-holding one validates without a `token` too."""
        for extra, error in (({}, None), ({"token": ""}, "bad-token: environments.engine: " + OTHER),
                             ({"token": 3}, "bad-token: environments.engine: " + OTHER),
                             ({"token": "FACTORY_ISSUES_TOKEN"}, None), ({"tracker": {"adapter": "github-issues"}}, None)):
            with self.subTest(extra=extra):
                self.setUp()
                self.edit("factory.yaml", lambda f: self._envs(f, engine=[(MAIN, {"write": True}), (OTHER, extra)]))
                errs = [e for e in self.errors() if "token" in e]
                if error:
                    self.assertTrue(any(e.startswith(error) for e in errs), errs)
                else:
                    self.assertEqual(errs, [])

    def test_issues_token_env_is_deprecated_and_still_compiled(self):
        """#1236: until the team declares `token:`, `issues_token_env` keeps its old meaning, with a warning."""
        def change(f):
            self._envs(f, engine=[(MAIN, {"write": True}), (OTHER, {})])
            f["defaults"].setdefault("settings", {})["issues_token_env"] = "OLD"
        self.edit("factory.yaml", change)
        defn = definition.load(self.root)
        project = definition.to_project(defn)[0]
        self.assertEqual(project["issues_token_env"], "OLD")
        self.assertNotIn("tokens", project["environments"]["engine"])  # never a declared token: checkouts keep Air's
        self.assertTrue(any(w.startswith("deprecated-key: defaults.settings.issues_token_env") for w in definition.warnings(defn)))

    def test_compile_lists_each_repository_token(self):
        self.edit("factory.yaml", lambda f: self._envs(f, engine=[(MAIN, {"write": True}), (OTHER, {"token": "T"})]))
        envs = definition.to_project(definition.load(self.root))[0]["environments"]
        self.assertEqual(envs["engine"]["tokens"], {"slop-eval-factory": "T"})

    def test_settings_merge_order(self):
        defn = definition.load(self.root)
        s = definition.settings(defn, "fix")
        self.assertEqual(s["fix_rounds"], 3)                    # node
        self.assertEqual(s["limits"]["runs_per_day"], 20)       # workflow settings
        self.assertEqual(s["size_stop_factor"], 1.5)            # agent `dev`
        self.assertEqual(s["branch_pattern"], "ez/{ticket}-{slug}")   # factory defaults
        defn["workflow"]["nodes"]["fix"]["settings"]["branch_pattern"] = None   # `null` clears
        self.assertNotIn("branch_pattern", definition.settings(defn, "fix"))

    def test_render_uses_environment_repositories(self):
        defn = definition.load(self.root)
        body = definition.render(defn, "knowledge-review", "zaharchenko-evgeny/slop-eval-factory")  # the copy's repository
        self.assertIn("`zaharchenko-evgeny/slop-eval-factory` (main), `zaharchenko-evgeny/slop-factory`.", body)
        self.assertIn("with focus knowledge;", body)
        self.assertIn("in mode `fix`", definition.render(defn, "fix"))          # what differs comes from settings

    def test_compile_is_one_to_one(self):
        defn = definition.load(self.root)
        descriptor, wf = definition.to_project(defn)
        self.assertEqual(set(wf["nodes"]), set(defn["workflow"]["nodes"]))
        n = wf["nodes"]
        self.assertEqual((n["implement-small"]["tier"], n["implement-medium"]["tier"], n["review-high"]["tier"]),
                         ("luna", "sonnet", "sol"))
        self.assertNotIn("tiers", n["implement-small"])
        self.assertEqual(n["implement-small"]["wakes_on"], ["command implement-small"])
        self.assertEqual(descriptor["targets"]["air"]["models"]["sol"], "codex/gpt-6.1-sol")
        self.assertEqual(descriptor["commands"]["review-low"], {"store": {"pr_label": "factory:review-low"}})
        self.assertEqual(descriptor["commands"]["risk:low"], {"store": {"label": "risk:low"}})

    def test_ci_result_labels_are_pr_labels(self):
        """#790: ci-sync labels the PR itself, so no effect needs `on: pr` for a CI result label (ci-failed's)."""
        descriptor = definition.to_project(definition.load(ROOT / "tests/fixtures/slop"))[0]
        for fact in ("ci-red",):
            self.assertEqual(descriptor["commands"][fact], {"store": {"pr_label": f"factory:{fact}"}})

    def test_compile_triggers(self):
        wakes = {k: v["wakes_on"] for k, v in definition.to_project(definition.load(self.root))[1]["nodes"].items()}
        self.assertEqual(wakes["fix"], ["command fix"])   # red CI reaches it through the reviewer's verdict
        self.assertEqual(wakes["finish"], ["state pr-merged", "ticket closed"])
        self.assertEqual(wakes["notify"], ["state blocked", "state needs-answers", "command needs-human"])
        self.assertEqual(wakes["retro"], ["schedule retro"])
        self.assertEqual(wakes["human"], ["manual"])

    def test_compile_guards_and_effects(self):
        desc, wf = definition.to_project(definition.load(self.root))
        n = wf["nodes"]
        self.assertNotIn("done", n["human"]["outcomes"]["ready"]["from"])          # `!done`
        self.assertIn("draft", n["human"]["outcomes"]["ready"]["from"])
        self.assertEqual(n["merge"]["outcomes"]["merged"]["if"], {"review": "approved-at-head"})
        self.assertEqual(n["merge"]["outcomes"]["merged"]["ci"], ["green"])
        self.assertEqual(n["automerge"]["outcomes"]["merge"]["if"], {"label": "risk:low"})
        self.assertEqual(n["automerge"]["outcomes"]["merge"]["else"], "needs-human")
        self.assertEqual(n["dispatch"]["outcomes"]["small"]["effects"],
                         [{"command": "complexity:small"}, {"command": "implement-small"}, {"state": "in-dev"}])
        self.assertEqual(desc["states"]["done"]["kind"], "final")

    def test_node_timeout_reaches_reconcile(self):  # a review that waits for CI is not a missed run
        self.edit("workflow.yaml", lambda w: w["nodes"]["fix"].update(timeout_minutes=50))
        defn = definition.load(self.root)
        self.assertEqual(definition.nodes(defn)["nodes"]["fix"]["timeout_minutes"], 50)
        self.assertEqual(definition.to_project(defn)[1]["nodes"]["fix"]["timeout_minutes"], 50)
        self.assertNotIn("timeout_minutes", definition.nodes(defn)["nodes"]["merge"])

    def test_nodes_read_the_definition_in_the_gate_shape(self):
        graph = definition.nodes(definition.load(self.root))
        n = graph["nodes"]
        self.assertEqual(n["fix"]["triggers"], ["pull_request.labeled factory:fix"])
        self.assertEqual(n["finish"]["triggers"], ["pull_request.merged", "issues.closed"])
        self.assertEqual(n["retro"]["triggers"], ["schedule retro"])
        self.assertTrue(n["retro"]["routine"])
        self.assertEqual(n["human"]["triggers"], ["manual"])
        self.assertEqual(n["human"]["when"], {"actor": "human"})
        self.assertNotIn("done", n["human"]["outcomes"]["ready"]["from"])          # `!done`
        self.assertIn("draft", n["human"]["outcomes"]["ready"]["from"])
        self.assertEqual(n["dispatch"]["outcomes"]["small"]["effects"],
                         [{"label": "complexity:small"}, {"label": "factory:implement-small"}, {"stage": "in-dev"}])
        self.assertEqual(n["automerge"]["triggers"], ["pull_request.labeled factory:automerge"])  # a PR label
        self.assertTrue(n["automerge"]["ci-independent"])
        self.assertEqual(n["automerge"]["outcomes"]["merge"]["effects"][1], {"label": "factory:merge", "on": "pr"})
        self.assertEqual(graph["schedules"]["retro"], {"cron": "0 2 * * *"})
        self.assertEqual(graph["environments"]["engine"]["repository"], "primary")
        self.assertEqual(graph["environments"]["team"]["repository"], "slop-eval-factory")

    def test_config_reads_stages_and_commands_from_the_definition(self):
        tracker = definition.config(definition.load(self.root))["tracker"]
        self.assertEqual(tracker["stages"]["in-dev"], "factory:in-dev")
        self.assertEqual(tracker["stage_kinds"]["done"], "final")
        self.assertNotIn("pr-merged", tracker["stages"])  # a PR fact is no label
        self.assertEqual(tracker["commands"]["review-low"], {"pr_label": "factory:review-low"})

    def test_compiled_view_passes_engine_checks(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"FACTORY_TEAM": str(self.root)}):  # restores the caller's value
            import config, nodes  # noqa: E401
            self.assertEqual(nodes.validate(nodes.load(), config.load()), [])

    def test_a_ticket_walks_the_agent_only_flow(self):
        """triage -> ready -> in-dev -> in-review -> approved -> merged -> done on a memory tracker (ADR-0021)."""
        import config
        import gate
        import nodes as nodes_mod
        from gate_port import MemorySystem
        before = config._team
        config.set_team(self.root)
        try:
            n = nodes_mod.load()
            t = MemorySystem({"7": {"stage": "triage", "head": "h1", "pr": 5, "pr_state": "open", "ci": "green"}})
            written, plain = [], t.apply
            t.apply = lambda e: written.append(e) or plain(e)
            for node, outcome, extra in [("intake", "medium-high", {}), ("dispatch", "medium", {}),
                                         ("implement-medium", "done-low", {}),
                                         ("review-low", "approve", {"findings": [], "verdict": "approve"})]:
                self.assertTrue(gate.apply(node, outcome, {"ticket": "7", **extra}, t, n)[1]["applied"], node)
            self.assertEqual([e["focus"] for e in written if e["kind"] == "review"], ["review-low"])  # the node names it
            attrs = t.facts("7")["attrs"]
            self.assertIn("risk:low", attrs)
            self.assertNotIn("risk:high", attrs)                     # one value per group
            t.tickets["7"]["reviews"] = [{"agent": True, "commit_id": "h1", "verdict": "approve"}]
            self.assertTrue(gate.apply("automerge", "merge", {"ticket": "7"}, t, n)[1]["applied"])
            self.assertEqual(gate.decide("merge", "7", t, n)[1]["outcome"], "merged")
            t.tickets["7"]["pr_state"] = "merged"
            self.assertEqual(gate.decide("finish", "7", t, n)[1]["outcome"], "done")
            self.assertEqual(t.facts("7")["stage"], "done")
            risky = MemorySystem({"8": {"stage": "approved", "attrs": ["risk:high"], "pr": 6, "pr_state": "open"}})
            gate.apply("automerge", "merge", {"ticket": "8", "reason": "[factory] fine"}, risky, n)
            self.assertIn("factory:needs-human", risky.facts("8")["attrs"])   # the guard sent it to a person
        finally:
            config.set_team(before)

    def test_the_last_child_done_finishes_its_split_parent(self):
        import config
        import gate
        import nodes as nodes_mod
        from gate_port import MemorySystem
        before = config._team
        config.set_team(self.root)
        try:
            n = nodes_mod.load()
            t = MemorySystem({"9": {"stage": "split"}, "7": {"stage": "approved", "pr_state": "merged"},
                               "8": {"stage": "done"}})
            state = {"7": "open", "8": "closed"}
            t.parent = lambda x: "9" if str(x) in ("7", "8") else None
            t.children = lambda x: [{"number": k, "state": s} for k, s in state.items()] if str(x) == "9" else []
            plain = t.apply
            t.apply = lambda e: (state.__setitem__("7", "closed") if e["kind"] == "stage" and e["ticket"] == "7" else None) \
                or plain(e)
            out = gate.decide("finish", "7", t, n)[1]
            self.assertEqual((out["outcome"], out["parent"]["outcome"]), ("done", "children-done"))
            self.assertEqual(t.facts("9")["stage"], "done")
        finally:
            config.set_team(before)

    def test_neighbours_come_from_the_workflow(self):
        from factory_plugins.comments import mentions
        pairs = mentions.neighbours(definition.load(self.root))
        for a, b in [("dev", "reviewer"), ("dev", "reviewer-cheap"), ("reviewer", "approver"), ("dispatcher", "dev"),
                     ("approver", "clerk"), ("dev", "dev-strong")]:
            self.assertIn(frozenset((a, b)), pairs, (a, b))
        self.assertNotIn(frozenset(("router", "approver")), pairs)

    def test_comment_marker_names_the_agent(self):
        import config
        import gate
        before = config._team
        config.set_team(self.root)
        try:
            self.assertEqual(gate.comment_marker("review-high"), "<!-- factory:from reviewer -->")
            self.assertEqual(gate.comment_marker("reply-dev"), "<!-- factory:from dev reply -->")
        finally:
            config.set_team(before)

    def test_mentionable_agents_get_a_generated_reply_step(self):
        defn = definition.load(self.root)
        nodes = defn["workflow"]["nodes"]
        self.assertNotIn("mentions", nodes)  # #893: the comment node routes a mention to `reply-<agent>`
        self.assertIn({"wake": "asker"}, nodes["reply-dev"]["outcomes"]["replied"]["effects"])
        self.assertEqual((nodes["reply-dev"]["agent"], nodes["reply-dev"]["output"]), ("dev", "read-only"))
        self.assertIn("Someone asked you", definition.render(defn, "reply-reviewer"))
        self.edit("workflow.yaml", lambda w: w["nodes"].update({"reply-x": {"agent": "dev", "triggers": ["manual"],
                                                                            "outcomes": {"done": {}}}}))
        self.assertError("reserved-node: nodes.reply-x")

    def test_fix_and_update_merge_main_on_a_conflicting_pr(self):
        import config
        import gate
        import nodes as nodes_mod
        from gate_port import MemorySystem
        before = config._team
        config.set_team(self.root)
        try:
            n = nodes_mod.load()
            for conflict, want in ((True, True), (False, False)):
                t = MemorySystem({"7": {"stage": "changes-requested", "pr": 5, "head": "h1", "conflict": conflict}})
                for node in ("fix", "update"):
                    with self.subTest(node=node, conflict=conflict):
                        code, out = gate.check(node, {"id": "e1", "ticket": "7"}, t, n, config.load())
                        self.assertEqual((code, out["merge_main"]), (gate.GO, want))
        finally:
            config.set_team(before)

    def test_a_failed_review_write_leaves_labels_and_stage_as_they_were(self):
        import config
        import gate
        import nodes as nodes_mod
        from gate_port import MemorySystem
        before = config._team
        config.set_team(ROOT / "tests/fixtures/slop")  # its review outcomes replace the risk label (#998)
        try:
            n = nodes_mod.load()
            t = MemorySystem({"7": {"stage": "in-review", "pr": 5, "head": "h1", "ci": "green", "attrs": ["risk:low"],
                                     "pr_labels": ["factory:review-low"]}})
            apply = t.apply

            def failing(e):
                if e["kind"] == "review":
                    raise RuntimeError("HTTP 403: Resource not accessible by integration")
                return apply(e)
            t.apply = failing
            with self.assertRaises(RuntimeError):
                gate.apply("review-low", "approve-risky", {"ticket": "7", "summary": "ok"}, t, n)
            f = t.facts("7")
            self.assertEqual((f["stage"], "risk:low" in f["attrs"]), ("in-review", True))
        finally:
            config.set_team(before)

    def test_a_person_asks_dev_on_the_pr_and_dev_answers_there(self):
        import config
        import gate
        import nodes as nodes_mod
        from gate_port import MemorySystem
        before = config._team
        config.set_team(self.root)
        try:
            n = nodes_mod.load()
            t = MemorySystem({"7": {"stage": "in-review", "pr": 5, "head": "h1"}})
            t.pr_comments = lambda ticket: [{"author": "alice", "body": "@factory:dev why the retry?", "at": "2"}]
            t.apply({"kind": "label", "ticket": "7", "value": "factory:reply-dev"})  # the comment node's `reply: dev`
            begin = gate.check("reply-dev", {"id": "e1", "ticket": "7"}, t, n, config.load())[1]
            self.assertEqual({k: begin["mention"][k] for k in ("by", "on")}, {"by": "alice", "on": "pr"})
            sent = []
            apply = t.apply
            t.apply = lambda e: sent.append(e) or apply(e)
            gate.apply("reply-dev", "replied", {"ticket": "7", "reply": "It covers the flaky API."}, t, n)
            reply = next(e for e in sent if e["kind"] == "comment")
            self.assertEqual(reply.get("on"), "pr")                                   # where the question was
            self.assertTrue(reply["value"].startswith("@alice It covers the flaky API."))
            self.assertIn("<!-- factory:from dev reply -->", reply["value"])
            self.assertNotIn("factory:reply-dev", t.facts("7")["attrs"])
            self.assertFalse([e for e in sent if e["kind"] == "label"])  # a person asked: nobody is woken
            t.pr_comments = lambda ticket: [{"author": "bot", "body": reply["value"], "at": "3"}]
            from factory_plugins.comments import mentions
            self.assertFalse(mentions.holds("dev", t.facts("7"), t, "7"))  # a reply asks no one
        finally:
            config.set_team(before)

    def test_ask_is_an_outcome_of_every_mentionable_node_and_the_answer_returns_to_the_asker(self):
        import config
        import gate
        import nodes as nodes_mod
        from gate_port import MemorySystem
        before = config._team
        config.set_team(self.root)
        try:
            n = nodes_mod.load()
            agents = {k: v.get("agent") for k, v in definition.load(self.root)["workflow"]["nodes"].items()}
            for name, node in n["nodes"].items():
                self.assertEqual("ask" in node["outcomes"], agents[name] in ("dev", "reviewer") and not name.startswith("reply-"), name)
            self.assertEqual(n["nodes"]["implement-medium"]["outcomes"]["ask"]["to"], ["reviewer"])
            t = MemorySystem({"7": {"stage": "in-dev"}})
            gate.apply("implement-medium", "ask", {"ticket": "7", "to": "reviewer", "text": "Is X in scope?"}, t, n)
            ask = next(json.loads(v) for k, v in t.tickets["7"]["log"] if k == "comment")
            self.assertTrue(ask.startswith("[factory] **dev (developer):** @factory:reviewer Is X in scope?\n"))
            self.assertEqual(t.facts("7")["stage"], "in-dev")
            self.assertIn("factory:comment", t.facts("7")["attrs"])                    # the comment node wakes
            t.tickets["7"]["comments"] = [{"author": "bot", "body": ask, "at": "1"}]
            t.apply({"kind": "label", "ticket": "7", "value": "factory:reply-reviewer"})  # its `reply: reviewer`
            t.tickets["7"]["log"].clear()
            with self.assertRaises(gate.Bad):
                gate.apply("reply-reviewer", "ask", {"ticket": "7", "to": "dev", "text": "?"}, t, n)
            sent = []
            apply = t.apply
            t.apply = lambda e: sent.append(e) or apply(e)
            gate.apply("reply-reviewer", "replied", {"ticket": "7", "reply": "Yes."}, t, n)
            answer = next(e for e in sent if e["kind"] == "comment")
            self.assertTrue(answer["value"].startswith("@factory:dev Yes."))
            self.assertEqual(answer["notes"]["context"], "Answer to ask 7-1: Yes.")
            self.assertEqual([e["kind"] for e in sent if e["value"] == "factory:implement-medium"],
                             ["unlabel", "label"])                                       # the asker wakes
            self.assertEqual(t.facts("7")["stage"], "in-dev")
        finally:
            config.set_team(before)

    def test_routine_with_effects_is_rejected_and_bare_outcomes_pass(self):
        for effects in ([{"label": "factory:x", "on": "pr"}], [{"comment": "hi"}], [{"label": "factory:x"}]):
            self.edit("workflow.yaml", lambda w: w["nodes"]["retro"]["outcomes"].update(proposed={"effects": effects}))
            self.assertError("routine-effects: nodes.retro.proposed")
        self.edit("workflow.yaml", lambda w: w["nodes"]["retro"]["outcomes"].update(proposed={}, nothing=None))
        self.assertEqual(self.errors(), [])

    def test_ticket_node_keeps_its_effects(self):
        self.assertFalse([e for e in self.errors() if e.startswith("routine-effects")])
        self.edit("workflow.yaml", lambda w: w["nodes"]["retro"]["triggers"].append({"label": "factory:retro-now"}))
        self.edit("workflow.yaml", lambda w: w["nodes"]["retro"]["outcomes"].update(proposed={"effects": [{"comment": "hi"}]}))
        self.assertFalse([e for e in self.errors() if e.startswith("routine-effects")])

    def test_scheduled_nodes_are_routines(self):
        n = definition.to_project(definition.load(self.root))[1]["nodes"]
        self.assertTrue(all(n[k].get("routine") for k in ("retro", "reconcile", "doctor-engine", "doctor-team")))
        self.assertFalse(n["intake"].get("routine"))

    def test_doctor_reads_skills_from_the_definition(self):
        import config
        import doctor
        import nodes as nodes_mod
        before = config._team
        config.set_team(self.root)
        try:
            tools, skills = doctor.env_needs("engine", nodes_mod.load())
            self.assertIn("tdd", skills)
            self.assertIn("factory-step", skills)
            self.assertNotIn("retro", skills)        # retro runs in `knowledge`
        finally:
            config.set_team(before)

    def test_old_command_templates_never_match_for_a_definition_team(self):
        import config
        import vocab
        before = config._team
        config.set_team(self.root)
        try:
            for name in ("implement-<tier>", "fix-<tier>", "update-<tier>", "review-<focus>", "rereview-<focus>"):
                self.assertFalse("factory:fix".startswith(vocab.prefix(name)), name)
                self.assertTrue(vocab.cmd(name, "x").startswith(vocab.UNDECLARED))
            self.assertEqual(vocab.cmd("review-low"), "factory:review-low")   # a declared one still resolves
        finally:
            config.set_team(before)

    def test_cli_validate(self):
        run = subprocess.run(["uv", "run", "--quiet", str(ROOT / "cli.py"), "validate"], capture_output=True,
                             text=True, env={**os.environ, "FACTORY_TEAM": str(self.root)})
        self.assertEqual((run.returncode, ok_lines(run.stdout)), (0, "factory: ok"), run.stderr)

    def test_cli_validate_rejects_redundant_label_group_unlabel(self):
        self.edit("workflow.yaml", lambda w: w["nodes"]["intake"]["outcomes"]["ready"]["effects"].extend(
            [{"unlabel": "risk:high"}, {"label": "risk:low"}]))
        run = subprocess.run(["uv", "run", "--quiet", str(ROOT / "cli.py"), "validate"], capture_output=True,
                             text=True, env={**os.environ, "FACTORY_TEAM": str(self.root)})
        self.assertEqual(run.returncode, 1, run.stdout + run.stderr)
        self.assertIn("nodes.intake.ready", run.stdout)
        self.assertIn("keep only the label", run.stdout)


class SlopBugPathTest(unittest.TestCase):
    """#161: a bug report goes triage -> investigating, then to in-dev, not-actionable or needs-answers."""

    def run_flow(self, outcome, reason):
        import config
        import gate
        import nodes as nodes_mod
        from gate_port import MemorySystem
        before = config._team
        config.set_team(ROOT / "tests/fixtures/slop")
        try:
            n = nodes_mod.load()
            t = MemorySystem({"7": {"stage": "triage"}})
            self.assertTrue(gate.apply("intake", "bug", {"ticket": "7"}, t, n)[1]["applied"])
            self.assertEqual(t.facts("7")["stage"], "investigating")
            code, _ = gate.apply("investigate", outcome, {"ticket": "7", "reason": reason}, t, n)
            self.assertEqual(code, gate.GO)
            return t
        finally:
            config.set_team(before)

    def test_a_bug_with_a_failing_test_goes_to_in_dev(self):
        t = self.run_flow("bug", "[factory] Failing test: tests/test_x.py::test_empty; cause: x.py:10")
        self.assertEqual(t.facts("7")["stage"], "in-dev")
        self.assertIn("factory:implement-medium", t.facts("7")["attrs"])   # the developer starts on it
        self.assertIn("tests/test_x.py::test_empty", str(t.tickets["7"].get("log")))

    def test_intended_behaviour_is_not_actionable_with_the_evidence(self):
        t = self.run_flow("intended", "[factory] Intended: docs/x.md:12 says empty input is rejected")
        self.assertEqual(t.facts("7")["stage"], "not-actionable")
        self.assertIn("docs/x.md:12", str(t.tickets["7"].get("log")))

    def test_cannot_reproduce_asks_the_reporter(self):
        t = self.run_flow("cannot-reproduce", "[factory] Not reproduced. 1. Which version?")
        self.assertEqual(t.facts("7")["stage"], "needs-answers")


class YouTrackRepositoryScopesTest(unittest.TestCase):
    """#1259: one YouTrack project, one environment per repository, each repository narrowed by its Subsystem."""
    TEAM = "zaharchenko-evgeny/slop-eval-factory"
    SCOPES = {"JetBrains/jbeval": "JBeval", "JetBrains/harbor-infra": "Harbor"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "team"
        shutil.copytree(FIXTURE, self.root)
        f = yaml.safe_load((self.root / "factory.yaml").read_text())
        w = yaml.safe_load((self.root / "workflow.yaml").read_text())
        f["tracker"] = {"adapter": "youtrack", "url": "https://yt.example", "project": "LME", "label_prefix": "factory:"}
        base = {k: v for k, v in f["environments"]["engine"].items() if k != "repositories"}
        scoped = lambda v: {"tracker": {"scope": {"field": "Subsystem", "value": v}}}  # noqa: E731
        f["environments"] = {
            "jbeval": {**base, "ci": {"workflow": "ci"},
                       "repositories": [{"repo": "JetBrains/jbeval", "write": True, **scoped("JBeval")}]},
            "harbor": {**base, "ci": {"workflow": "teamcity"}, "repositories": [
                {"repo": "JetBrains/harbor-infra", "write": True, "default_branch": "master", **scoped("Harbor")}]},
            "knowledge": {**base, "repositories": [{"repo": self.TEAM, "write": True, "tracker": "none"}]},
            "team": {**base, "repositories": [{"repo": self.TEAM, "write": True, "tracker": "none"}]}}
        w["settings"]["environment"] = "jbeval"
        for name, n in w["nodes"].items():
            if n.pop("environment", "engine") == "engine" and name not in ("human", "accept-knowledge"):
                n["environments"] = ["jbeval", "harbor"]
            elif name not in ("human", "accept-knowledge"):
                n["environment"] = "knowledge" if name in ("retro", "knowledge-review") else "team"
        w["nodes"]["fix"]["triggers"].append({"ci": "failed"})
        (self.root / "factory.yaml").write_text(yaml.safe_dump(f, sort_keys=False))
        (self.root / "workflow.yaml").write_text(yaml.safe_dump(w, sort_keys=False))
        import config
        self.previous = config._team
        config.set_team(self.root)
        self.config = config.load()

    def tearDown(self):
        import config
        config.set_team(self.previous)
        shutil.rmtree(self.tmp)

    def plan(self):
        import nodes
        from factory_plugins.air import deploy as air
        return {a["name"]: a for a in air.plan_air(nodes.load(), self.config)["automations"]}

    def test_validates_without_a_code_host_repository(self):
        self.assertEqual(definition.errors(definition.load(self.root)), [])
        self.assertNotIn("code_host", self.config)
        self.assertEqual([(p["repository"], p["tracker"]["scope"]["value"]) for p in self.config["ticket_places"]],
                         list(self.SCOPES.items()))

    def test_deploy_plans_one_copy_per_repository_with_its_scope_filter(self):
        plan = self.plan()
        for full, value in self.SCOPES.items():
            intake = plan[f"[Slop factory] intake {full.split('/')[1]}"]
            self.assertEqual(intake["repository"]["fullName"], full)
            self.assertEqual(intake["triggers"][0]["filters"],
                             {"tagAdded": "factory:triage", "projectShortName": "LME", "Subsystem": value})

    def test_the_harbor_copies_merge_and_update_on_master_with_their_own_ci(self):
        plan = self.plan()
        for node in ("merge", "update"):
            self.assertEqual(plan[f"[Slop factory] {node} harbor-infra"]["branch"], "master")
            self.assertNotIn("branch", plan[f"[Slop factory] {node} jbeval"])  # Air's default: main
        ci = {repo: [t["filters"] for t in plan[f"[Slop factory] fix {repo}"]["triggers"] if t["eventType"] == "workflow_run.failed"]
              for repo in ("jbeval", "harbor-infra")}
        self.assertEqual(ci, {"jbeval": [{"workflow_name": "ci"}], "harbor-infra": [{"workflow_name": "teamcity"}]})

    def test_each_place_tracker_knows_the_other_repositories_scopes(self):
        import gate
        tracker, _ = gate.place_systems(self.config, "place:JetBrains/jbeval", yt_run=lambda *a, **k: [], run=lambda *a, **k: "")
        store = tracker.tickets
        self.assertEqual(store.scope, {"field": "Subsystem", "value": "JBeval"})
        self.assertEqual(store.scope_of("harbor-infra"), {"field": "Subsystem", "value": "Harbor"})
        self.assertEqual(type(gate.tracker_of(self.config)).__name__, "ScopedTracker")  # no code_host.repository needed


class PlanTest(unittest.TestCase):
    """`definition.plan`: the sweep's start, merge, finish, reopen and join decisions from the node graph."""
    ON = {"enabled": True, "max_risk": "high"}

    def setUp(self):
        import nodes as nodes_mod
        self.graph = nodes_mod.load(ROOT.parent / "engine/nodes.yaml")
        self.base = {"ticket": 1, "pr": 5, "pr_state": "open", "head": "h1", "ci": "green", "round_complete": True,
                     "round_verdict": "approve", "round_head": "h1", "risk": "low", "blockers_open": 0,
                     "attrs": ["complexity:medium"], "pr_labels": [], "reviews": []}

    def plan(self, **facts):
        return definition.plan(self.graph, {**self.base, **facts}, self.ON, "mid")

    def test_start_a_ready_ticket_with_its_complexity_tier(self):
        self.assertEqual(self.plan(stage="ready", pr_state="none"), ("start", "started", {"tier": "mid"}))
        self.assertIsNone(self.plan(stage="ready", pr_state="none", blockers_open=1))
        self.assertIsNone(self.plan(stage="ready", pr_state="none", issue_state="closed"))

    def test_merge_an_approved_green_head_and_update_a_behind_one(self):
        self.assertEqual(self.plan(stage="approved"), ("merge", "merged", {}))
        self.assertEqual(self.plan(stage="approved", behind=True), ("merge", "updated", {}))
        self.assertIsNone(self.plan(stage="approved", ci="pending"))

    def test_finish_a_merged_pr_a_conflict_an_answer_and_done_children(self):
        self.assertEqual(self.plan(stage="approved", pr_state="merged"), ("finish", "merged", {}))
        self.assertIsNone(self.plan(stage="done", pr_state="merged"))  # a final stage has nothing to finish
        self.assertEqual(self.plan(stage="approved", conflict=True), ("finish", "conflict", {}))
        self.assertEqual(self.plan(stage="needs-answers", pr_state="none", answered=True), ("finish", "answered", {}))
        kids = [{"number": 2, "state": "closed", "blocked": False, "not_planned": False}]
        self.assertEqual(self.plan(stage="split", pr_state="none", children=kids), ("finish", "children-done", {}))
        kids.append({"number": 3, "state": "open", "blocked": True, "not_planned": False})
        self.assertEqual(self.plan(stage="split", pr_state="none", children=kids)[:2], ("finish", "child-blocked"))

    def test_reopen_an_approved_ticket_with_a_new_head_or_red_ci(self):
        self.assertEqual(self.plan(stage="approved", head="h2"), ("reopen", "reopened", {}))
        self.assertEqual(self.plan(stage="approved", ci="red"), ("reopen", "reopened", {}))

    def test_join_a_complete_round_and_ask_waiting_reviewers_again(self):
        self.assertEqual(self.plan(stage="in-review")[:2], ("join", "approved"))
        self.assertEqual(self.plan(stage="in-review", round_verdict="changes")[:2], ("join", "changes"))
        self.assertEqual(self.plan(stage="in-review", waiting_reviewers=["behaviour"], round_complete=False),
                         ("join", "waiting", {}))
        self.assertIsNone(self.plan(stage="in-review", round_complete=False))

    def test_a_decision_needs_its_outcome_in_the_graph(self):
        del self.graph["nodes"]["merge"]["outcomes"]["merged"]
        self.assertIsNone(self.plan(stage="approved"))

    def test_decisions_do_not_read_ci_labels(self):
        labels = ["factory:ci-red", "factory:ci-green"]
        self.assertEqual(self.plan(stage="approved", pr_labels=labels), ("merge", "merged", {}))
        self.assertEqual(self.plan(stage="approved", ci="pending", pr_labels=labels, ci_label="green"), None)


class NodesLoadTest(unittest.TestCase):
    def test_nodes_load_reads_the_definition(self):
        import config
        import nodes
        config.set_team(FIXTURE)
        try:
            read = nodes.load()
        finally:
            config.set_team(None)
        self.assertEqual(read["nodes"]["dispatch"]["triggers"], ["issues.labeled factory:ready"])
        self.assertEqual(read["nodes"]["dispatch"]["outcomes"]["large"]["effects"],
                         [{"label": "complexity:large"}, {"label": "factory:implement-large"}, {"stage": "in-dev"}])
        self.assertEqual(read["nodes"]["automerge"]["triggers"], ["pull_request.labeled factory:automerge"])
        self.assertEqual(read["nodes"]["automerge"]["when"], {"stage": "approved"})


if __name__ == "__main__":
    unittest.main()

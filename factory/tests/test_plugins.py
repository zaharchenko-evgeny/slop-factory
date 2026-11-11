#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Tests for the plugin registry (ADR-0026): selection, `requires`, ownership, laziness and the module-off format.
Run: uv run factory/tests/test_plugins.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import plugins  # noqa: E402

FIXTURES = ROOT / "tests/fixtures"


class FakePlugins(unittest.TestCase):
    """Plugins written to a temporary package `fakeplug`, listed in KNOWN for the test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "fakeplug").mkdir()
        (self.tmp / "fakeplug/__init__.py").write_text("")
        sys.path.insert(0, str(self.tmp))
        self.known = patch.dict(plugins.KNOWN)
        self.known.start()
        plugins.reset()

    def tearDown(self):
        self.known.stop()
        plugins.reset()
        sys.path.remove(str(self.tmp))
        for name in [m for m in sys.modules if m == "fakeplug" or m.startswith("fakeplug.")]:
            del sys.modules[name]
        shutil.rmtree(self.tmp)

    def plugin(self, name, declaration, code=""):
        """Package `fakeplug.<name>`: `PLUGIN = Plugin(name, <declaration>)`, and `code` in its `impl` module."""
        pkg = self.tmp / "fakeplug" / name
        pkg.mkdir()
        (pkg / "__init__.py").write_text(f"from plugins import Plugin\nPLUGIN = Plugin({name!r}, {declaration})\n")
        (pkg / "impl.py").write_text(textwrap.dedent(code))
        plugins.KNOWN[name] = f"fakeplug.{name}"


class SelectionTest(FakePlugins):
    def test_plugins_and_the_modules_alias(self):
        self.assertIn("comments", plugins.selected({"plugins": ["comments"]}))
        self.assertIn("comments", plugins.selected({"modules": ["comments"]}))
        self.assertNotIn("comments", plugins.selected({}))

    def test_systems_surfaces_and_runners_turn_on_implicitly(self):
        self.assertEqual(plugins.selected({}), ("github",))  # github tracker and vcs when none is named
        config = {"tracker": {"adapter": "youtrack"}, "code_host": {"adapter": "space"}, "surface": "local",
                  "environments": {"infra": {"vcs": "github"}},
                  "models": {"sonnet": {"harness": "claude", "model": "m"}, "luna": {"harness": "codex"}}}
        self.assertEqual(plugins.selected(config), ("claude", "codex", "github", "local", "space", "youtrack"))
        self.assertEqual(plugins.selected({"vcs": "space", "tracker": {"adapter": "github-issues"}}), ("github", "space"))

    def test_live_hooks_exist_for_every_tracker_and_vcs_combination(self):
        """A YouTrack team on Space selects no github plugin; the live commands and the doctor still find their hooks."""
        for config in ({}, {"tracker": {"adapter": "youtrack"}, "vcs": "space"},
                       {"tracker": {"adapter": "youtrack"}}, {"vcs": "space", "tracker": {"adapter": "github-issues"}}):
            with self.subTest(config=config):
                registry = plugins.registry(config)
                self.assertEqual([h() for h in registry.hook("gate.default_place")][:1], ["github"])
                self.assertTrue(list(registry.hook("doctor.run")))
                self.assertTrue(list(registry.hook("doctor.frontier")))

    def test_unknown_names_are_errors_and_select_nothing(self):
        config = {"plugins": ["comments", "chat"]}
        self.assertNotIn("chat", plugins.selected(config))
        self.assertEqual(plugins.errors(config), ["unknown-plugin: plugins: chat"])
        self.assertEqual(plugins.errors({"modules": ["chat"]}), ["unknown-plugin: modules: chat"])

    def test_the_list_must_be_a_list(self):
        self.assertEqual(plugins.errors({"plugins": "comments"}),
                         ["bad-plugins: factory.yaml: `plugins` must be a list, for example [comments]"])
        self.assertNotIn("comments", plugins.selected({"plugins": "comments"}))
        self.assertEqual(plugins.errors({}), [])

    def test_a_planned_plugin_without_a_package_turns_on_nothing(self):
        """KNOWN lists every planned plugin, so `plugins: [junie]` validates before its slice lands."""
        registry = plugins.registry({"plugins": ["junie"]})
        self.assertTrue(registry.is_on("junie"))
        self.assertEqual(plugins.errors({"plugins": ["junie"]}), [])
        self.assertEqual((registry.hook("definition.load"), registry.impl("notifier", "email")), ([], None))

    def test_requires_is_closed_transitively(self):
        self.plugin("aa", "requires=('bb',)")
        self.plugin("bb", "requires=('cc',)")
        self.plugin("cc", "")
        self.assertLessEqual({"aa", "bb", "cc"}, set(plugins.selected({"plugins": ["aa"]})))
        self.assertNotIn("aa", plugins.selected({"plugins": ["bb"]}))

    def test_a_requires_outside_known_is_a_registry_error(self):
        self.plugin("aa", "requires=('zz',)")
        with self.assertRaisesRegex(ValueError, "aa: requires unknown plugin zz"):
            plugins.owners()


class OwnershipTest(FakePlugins):
    def test_the_comments_keys(self):
        self.assertEqual({plugins.owner(k, of) for k, of in [("mention", "guard"), ("mention", "trigger"),
                          ("route", "effect"), ("reply", "effect"), ("skip", "effect"), ("ask", "outcome"),
                          ("mentionable", "agent")]}, {"comments"})
        self.assertIsNone(plugins.owner("comment", "effect"))  # `comment` `on: mention`: the target is the plugin's
        self.assertIsNone(plugins.owner("label"))
        self.assertIsNone(plugins.owner("route", "outcome"))  # categories are apart: an outcome may be called `route`

    def test_the_notify_keys(self):
        self.assertEqual({plugins.owner("notify", of) for of in ("effect", "command", "config")}, {"notify"})
        self.assertIsNone(plugins.owner("notify", "agent"))  # an agent or node may be called `notify`

    def test_a_key_with_two_owners_is_a_registry_error(self):
        self.plugin("aa", "guards={'mention': 'x:y'}")
        with self.assertRaisesRegex(ValueError, "plugin-clash: guard mention: owned by comments and aa"):
            plugins.owners()

    def test_the_one_module_off_format(self):
        self.assertEqual(plugins.refusal("nodes.comment.route", "route"),
                         "module-off: nodes.comment.route: route needs plugins: [comments]")
        self.assertEqual(plugins.registry({}).refusal("guard", "mention", "guard"),
                         "module-off: guard: mention needs plugins: [comments]")


class RegistryTest(FakePlugins):
    def test_comments_on_and_off(self):
        on, off = plugins.registry({"plugins": ["comments"]}), plugins.registry({})
        self.assertEqual(on.effect_kinds(), {"route", "reply", "skip", "wake"})
        self.assertEqual(set(on.guards()), {"mention"})
        self.assertEqual((on.triggers(), on.outcomes()), ({"mention"}, {"ask"}))
        self.assertEqual(on.effect("comment", "mention"), "factory_plugins.comments.mentions:reply_target")
        self.assertIsNone(on.effect("comment"))
        self.assertEqual((off.effect_kinds(), off.guards(), off.triggers(), off.outcomes()), (set(), {}, set(), set()))
        self.assertEqual([f.__name__ for f in on.hook("gate.outcome", "comments")], ["outcome"])
        self.assertEqual(on.hook("gate.outcome", "notify"), [])

    def test_cached_per_selection(self):
        self.assertIs(plugins.registry({"plugins": ["comments"]}), plugins.registry({"modules": ["comments"]}))
        self.assertIsNot(plugins.registry({"plugins": ["comments"]}), plugins.registry({}))

    def test_impl_command_and_hook_load_lazily(self):
        self.plugin("aa", "provides={'notifier': {'pigeon': 'fakeplug.aa.impl:Pigeon'}}, "
                          "commands={'coo': 'fakeplug.aa.impl:coo'}, hooks={'deploy.files': 'fakeplug.aa.impl:files'}",
                    """
                    class Pigeon:
                        pass
                    def coo(argv):
                        return len(argv)
                    def files(config):
                        return ("a",)
                    """)
        registry = plugins.registry({"plugins": ["aa"]})
        self.assertNotIn("fakeplug.aa.impl", sys.modules)  # the declaration only
        self.assertEqual(registry.impl("notifier", "pigeon").__name__, "Pigeon")
        self.assertIn("fakeplug.aa.impl", sys.modules)
        self.assertEqual(registry.command("coo")(["x"]), 1)
        self.assertEqual([f({}) for f in registry.hook("deploy.files", "aa")], [("a",)])
        self.assertIsNone(registry.command("bark"))
        self.assertIsNone(plugins.registry({}).command("coo"))

    def test_cli_runs_a_plugin_word_before_it_prints_help(self):
        import cli
        import config
        self.plugin("aa", "commands={'coo': 'fakeplug.aa.impl:coo'}", "def coo(argv):\n    return 7 + len(argv)\n")
        with patch.object(config, "load", return_value={"plugins": ["aa"]}):
            self.assertEqual(cli._main(["coo", "x"]), 8)
        with patch.object(config, "load", return_value={}), patch("builtins.print"):
            self.assertEqual(cli._main(["coo"]), 2)  # off: the help, as for any unknown word

    def test_cli_surfaces_a_plugin_command_import_error(self):
        import cli
        import config
        self.plugin("aa", "commands={'broken': 'fakeplug.aa.impl:broken'}", "raise ImportError('broken plugin command')\n")
        with patch.object(config, "load", return_value={"plugins": ["aa"]}):
            with self.assertRaisesRegex(ImportError, "broken plugin command"):
                cli._main(["broken"])

    def test_the_declaration_names_itself(self):
        self.plugin("aa", "")
        (self.tmp / "fakeplug/aa/__init__.py").write_text("from plugins import Plugin\nPLUGIN = Plugin('bb')\n")
        with self.assertRaisesRegex(ValueError, "declares the name 'bb'"):
            plugins.declaration("aa")


class LazinessTest(unittest.TestCase):
    """A plugin's code is imported only when the core calls it; an off plugin costs the core its declaration."""

    def imported(self, team, script):
        code = "\n".join([
            "import json, sys", f"sys.path.insert(0, {str(ROOT)!r})", "import config, definition, gate, nodes, deploy, doctor",
            f"config.set_team({str(team)!r})", textwrap.dedent(script),
            'print(json.dumps(sorted(m for m in sys.modules if m.startswith("factory_plugins"))))'])
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              env={**os.environ, "FACTORY_TEAM": str(team)})
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout.splitlines()[-1])

    def test_off_imports_the_declaration_only(self):
        with tempfile.TemporaryDirectory() as d:
            team = Path(d) / "team"
            shutil.copytree(FIXTURES / "v2", team)
            path = team / "factory.yaml"
            path.write_text(path.read_text().replace("plugins: [comments]", ""))
            mods = self.imported(team, """
                defn = definition.load(config.team_root())
                definition.errors(defn); definition.warnings(defn)
                nodes.validate(nodes.load(), config.load())
                try:
                    gate.guard_holds({"mention": "dev"}, {})
                except gate.Bad:
                    pass
                """)
        self.assertEqual(mods, ["factory_plugins",
                                *(f"factory_plugins.{p}" for p in ("air", "chart", "claude", "codex", "comments", "facts", "github", "junie",
                                                                    "local", "notify", "notify_email", "pi", "space", "youtrack"))])

    def test_notify_on_imports_its_code_only_when_called(self):
        with tempfile.TemporaryDirectory() as d:
            team = Path(d) / "team"
            shutil.copytree(FIXTURES / "v2", team)
            path = team / "factory.yaml"
            path.write_text(path.read_text().replace("plugins: [comments]", "plugins: [comments, notify_email]"))
            loaded = self.imported(team, "definition.load(config.team_root()); gate.plugin_registry()")
            checked = self.imported(team, "definition.errors(definition.load(config.team_root()))")
        self.assertFalse([m for m in loaded if m.startswith(("factory_plugins.notify.", "factory_plugins.notify_email."))])
        self.assertLessEqual({"factory_plugins.notify.checks", "factory_plugins.notify.notifiers"}, set(checked))
        self.assertFalse({"factory_plugins.notify.effect", "factory_plugins.notify.command"} & set(checked))

    def test_on_imports_what_the_core_calls(self):
        mods = self.imported(FIXTURES / "v2", "defn = definition.load(config.team_root())")
        self.assertEqual(mods, ["factory_plugins", "factory_plugins.claude", "factory_plugins.codex",  # models.<alias>.harness
                                "factory_plugins.comments", "factory_plugins.comments.mentions", "factory_plugins.github"])

    def test_github_declaration_only_until_a_system_is_used(self):
        """#1068: the github plugin is on by default, yet the registry, `gate.plugin_registry()` and importing the
        core modules load its declaration only."""
        mods = self.imported(FIXTURES / "v2", """
            import plugins
            registry = plugins.registry(config.load())
            assert registry.is_on("github"), registry.names
            gate.plugin_registry()
            registry.impls("tracker"); registry.impls("vcs")
            """)
        self.assertIn("factory_plugins.github", mods)
        self.assertEqual([m for m in mods if m.startswith("factory_plugins.github.")], [])

    def test_air_code_only_when_deploy_uses_it(self):
        """#1072: the core modules, the registry with the air surface on and `factory validate`'s output check load the
        air declaration and its surface class at most; the Air deploy code and the contracts load when deploy runs."""
        core = self.imported(FIXTURES / "v2", """
            import plugins
            registry = plugins.registry(config.load(), use=(("surface", "air"),))
            assert registry.is_on("air"), registry.names
            registry.impls("surface"); plugins.owners()
            deploy.labels(nodes.load(), config.load())
            """)
        validated = self.imported(FIXTURES / "v2", "assert deploy.output_errors(nodes.load(), config.load()) == []")
        deployed = self.imported(FIXTURES / "v2", """
            import plugins
            plugins.registry({}, use=(("surface", "air"),)).command("deploy --target air")
            """)
        self.assertIn("factory_plugins.air", core)
        self.assertEqual([m for m in core if m.startswith("factory_plugins.air.")], [])
        self.assertEqual([m for m in validated if m.startswith("factory_plugins.air.")], ["factory_plugins.air.surface"])
        self.assertIn("factory_plugins.air.deploy", deployed)
        self.assertNotIn("factory_plugins.air.contracts", deployed)

    def test_youtrack_code_only_when_selected_and_used(self):
        """#1069: a GitHub team, and the deploy plan, doctor probes and registry of any team, load the youtrack
        declaration at most; a youtrack team loads its provisioning when the deploy plans, its tracker when the gate
        builds one."""
        github = self.imported(FIXTURES / "v2", """
            import plugins
            registry = plugins.registry(config.load())
            assert not registry.is_on("youtrack"), registry.names
            registry.hook("deploy.plan"); registry.hook("doctor.probes"); registry.impls("tracker")
            plugins.owners()
            """)
        self.assertEqual([m for m in github if m.startswith("factory_plugins.youtrack.")], [])
        with tempfile.TemporaryDirectory() as d:
            team = Path(d) / "team"
            shutil.copytree(FIXTURES / "v2", team)
            path = team / "factory.yaml"
            path.write_text(path.read_text().replace("plugins: [comments]", "plugins: [comments, youtrack]"))
            listed = self.imported(team, """
                import plugins
                registry = plugins.registry(config.load())
                assert registry.is_on("youtrack"), registry.names
                registry.impls("tracker")
                """)
            used = self.imported(team, """
                import plugins
                plugins.registry(config.load()).impl("tracker", "youtrack")
                """)
        self.assertIn("factory_plugins.youtrack", listed)
        self.assertEqual([m for m in listed if m.startswith("factory_plugins.youtrack.")], [])
        self.assertLessEqual({"factory_plugins.youtrack.gate", "factory_plugins.youtrack.store"}, set(used))
        self.assertNotIn("factory_plugins.youtrack.provision", used)

    def test_space_code_only_when_selected_and_used(self):
        """#1070: a GitHub team's gate, validation and registry load the space declaration at most (also when a
        payload is read); a space team loads its code when the gate reads a payload or builds its vcs."""
        github = self.imported(FIXTURES / "v2", """
            import plugins
            registry = plugins.registry(config.load())
            assert not registry.is_on("space"), registry.names
            definition.errors(definition.load(config.team_root()))
            registry.hook("gate.trigger"); registry.impls("vcs"); plugins.owners()
            gate.from_trigger({"action": "opened", "issue": {"number": 3}})
            """)
        self.assertEqual([m for m in github if m.startswith("factory_plugins.space.")], [])
        with tempfile.TemporaryDirectory() as d:
            team = Path(d) / "team"
            shutil.copytree(FIXTURES / "v2", team)
            path = team / "factory.yaml"
            path.write_text(path.read_text().replace("plugins: [comments]", "plugins: [comments, space]"))
            listed = self.imported(team, """
                import plugins
                registry = plugins.registry(config.load())
                assert registry.is_on("space"), registry.names
                registry.impls("vcs"); plugins.impl_error(config.load(), "vcs", "space", "vcs")
                """)
            used = self.imported(team, """
                gate.from_trigger({"eventType": "space.merge_request.opened",
                                   "data": {"merge_request": {"number": 7, "branch": "ez/5-x"}}})
                """)
        self.assertIn("factory_plugins.space", listed)
        self.assertEqual([m for m in listed if m.startswith("factory_plugins.space.")], [])
        self.assertIn("factory_plugins.space.gate", used)

    def test_local_and_runner_code_only_when_used(self):
        """#1071: the core, the CLI, validation, doctor probes and a registry naming `surface: local` and runner
        harnesses load the local and runner declarations at most; `deploy --target local` or `run` loads the
        surface, and a role's run loads its harness's runner only."""
        prefixes = tuple(f"factory_plugins.{p}." for p in ("local", "claude", "codex", "junie", "pi"))
        idle = self.imported(FIXTURES / "v2", """
            import cli, plugins
            settings = {**config.load(), "surface": "local", "models": {"m": {"harness": "junie", "model": "x"}}}
            registry = plugins.registry(settings)
            assert registry.is_on("local") and registry.is_on("junie"), registry.names
            registry.impls("surface"); registry.impls("agent"); registry.hook("doctor.probes"); plugins.owners()
            definition.errors(definition.load(config.team_root()))
            """)
        self.assertEqual([m for m in idle if m.startswith(prefixes)], [])
        used = self.imported(FIXTURES / "v2", """
            import cli
            cli.surface_command("local", "run")
            from factory_plugins.local import surface
            surface.runner("codex")
            """)
        self.assertIn("factory_plugins.local.surface", used)
        self.assertIn("factory_plugins.codex.runner", used)
        self.assertFalse({"factory_plugins.claude.runner", "factory_plugins.junie.runner", "factory_plugins.pi.runner"} & set(used))
        self.assertFalse([m for m in used if m.startswith("factory_plugins.github.")])  # no tracker until a poll

    def test_chart_code_only_when_the_chart_command_is_used(self):
        """#1073: the core, the CLI, `nodes.py validate` and the registry load the chart declaration at most, and no
        config turns the plugin on; `factory chart` and `nodes.py chart` turn it on and load its renderer."""
        idle = self.imported(FIXTURES / "v2", """
            import cli, plugins
            registry = plugins.registry(config.load())
            assert not registry.is_on("chart"), registry.names
            plugins.owners(); registry.command("chart")
            nodes.validate(nodes.load(), config.load())
            """)
        self.assertIn("factory_plugins.chart", idle)
        self.assertEqual([m for m in idle if m.startswith("factory_plugins.chart.")], [])
        resolved = self.imported(FIXTURES / "v2", 'import cli; assert cli.tool_command("chart") is not None')
        self.assertIn("factory_plugins.chart.render", resolved)
        drawn = self.imported(FIXTURES / "v2", """
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()) as out:
                assert nodes.main(["chart"]) == 0
            assert out.getvalue().startswith("```mermaid"), out.getvalue()[:80]
            """)
        self.assertIn("factory_plugins.chart.render", drawn)


class SurfaceTest(unittest.TestCase):
    """#1072: a node's `output:` against its surface's `outputs()`, and `factory deploy` routed to the team's surface."""

    GRAPH = {"nodes": {"implement": {"role": "dev", "output": "push-to-new-branch"},
                       "review": {"role": "reviewer", "output": "read-only"},
                       "approve": {"role": "human", "output": "anything"},
                       "old": {"role": "dev", "enabled": False, "output": "anything"}}}

    def setUp(self):
        sys.path.insert(0, str(ROOT))
        import deploy
        self.deploy = deploy

    def test_air_serves_every_output_of_its_contract(self):
        from factory_plugins.air.surface import AirSurface
        schema = json.loads((ROOT / "tests/contracts/air-mcp.json").read_text())["servers"]["air-automations"]["tools"]
        create = next(t for t in schema if t["name"] == "create_automation")["inputSchema"]
        enum = create["properties"]["triggerVcs"]["items"]["properties"]["outputBehaviour"]["enum"]
        self.assertEqual(AirSurface().outputs(), set(enum))

    def test_output_checked_against_the_surface(self):
        air = {"targets": {"air": {"models": {}}}}
        self.assertEqual(self.deploy.output_errors(self.GRAPH, air), [])
        errs = self.deploy.output_errors(self.GRAPH, {**air, "surface": "local"})
        self.assertEqual(len(errs), 1)
        self.assertIn("nodes.implement: output 'push-to-new-branch' is not supported by surface local", errs[0])
        self.assertIn("unknown-surface", self.deploy.output_errors(self.GRAPH, {"surface": "moon"})[0])

    def test_no_surface_no_check(self):
        self.assertEqual(self.deploy.surface_name({}), None)
        self.assertEqual(self.deploy.output_errors({"nodes": {"x": {"role": "dev", "output": "bogus"}}}, {}), [])

    def test_deploy_runs_the_configured_surface(self):
        calls = []
        with patch.object(self.deploy.config_mod, "load", return_value={"surface": "local"}), \
                patch("factory_plugins.local.surface.deploy", lambda argv: calls.append(("local", argv)) or 0), \
                patch("factory_plugins.air.deploy.main", lambda argv: calls.append(("air", argv)) or 0):
            self.assertEqual(self.deploy.main(["--apply"]), 0)
            self.assertEqual(self.deploy.main(["--target", "air", "--dry-run"]), 0)
            self.assertEqual(self.deploy.main(["plan", "--target", "air"]), 0)
        with patch.object(self.deploy.config_mod, "load", return_value={}), \
                patch("factory_plugins.air.deploy.main", lambda argv: calls.append(("air", argv)) or 0):
            self.assertEqual(self.deploy.main(["--dry-run"]), 0)  # no `surface:`: Air, as before
        self.assertEqual(calls, [("local", ["--apply"]), ("air", ["--dry-run"]), ("air", ["plan", "--target", "air"]),
                                 ("air", ["--dry-run"])])


if __name__ == "__main__":
    unittest.main()

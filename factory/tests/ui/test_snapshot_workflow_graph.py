"""`ui_supported_workflow_graph`: the graph from fixtures of every supported definition format."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import snapshot_support as s  # noqa: E402
from ui import graph  # noqa: E402

CONFIG = {"tracker": {"stages": {"alpha": "x:alpha", "beta": "x:beta", "gamma": "x:gamma", "end": "x:end"},
                      "stage_kinds": {"alpha": "active", "beta": "active", "gamma": "waiting", "end": "final"}}}
NODES = {"schedules": {"nightly": {"cron": "0 2 * * *"}}, "nodes": {
    "plan": {"triggers": ["issues.labeled x:alpha"], "when": {"stage": "alpha"}, "role": "dev", "env": "e1",
             "outcomes": {"go": {"effects": [{"stage": "beta"}, {"label": "x:build-<tier>"}]},
                          "ask": {"effects": [{"comment": "?"}]},
                          "park": {"if": {"blockers": "open"}, "effects": [{"stage": "gamma"}], "else": "go"}}},
    "build": {"triggers": ["issues.labeled x:build-<tier>"], "when": {"stage": "beta"}, "role": "dev", "env": "e1",
              "outcomes": {"back": {"effects": [{"stage": "alpha"}]}, "ship": {"effects": [{"stage": "end"}]},
                           "talk": {"effects": [{"comment": "hi"}, {"review": "x"}]}}},
    "other": {"triggers": ["issues.labeled x:beta"], "when": {"stage": ["beta", "gamma"]}, "role": "dev", "env": "e2",
              "outcomes": {"note": {"effects": [{"comment": "only"}]}}},
    "sweep": {"triggers": ["schedule nightly"], "when": {"always": True}, "role": "script", "routine": True,
              "outcomes": {"swept": {}}}}}


def transitions(nodes):
    """(source, target, node, outcome) the compiled nodes establish: an outcome's stage effect."""
    out = set()
    for name, n in nodes["nodes"].items():
        for oname, o in n["outcomes"].items():
            target = next((e["stage"] for e in o.get("effects") or [] if "stage" in e), None)
            when = (n.get("when") or {}).get("stage")
            sources = o.get("from") or (when if isinstance(when, list) else [when])
            if target:
                out |= {(src, target, name, oname) for src in sources}
    return out


class RealFormats(unittest.TestCase):
    def test_every_format_has_exactly_the_compilers_transitions(self):
        for fixture in ("team", "v2", "slop"):  # the old layout, a v2 factory.yaml, a full project
            with self.subTest(fixture), s.team(fixture) as (config, nodes):
                g = graph.build(nodes, config)
                got = {(e["source"], e["target"], e["node"], e["outcome"]) for e in g["edges"] if e["kind"] == "transition"}
                self.assertEqual(got, transitions(nodes))
                self.assertEqual({n["id"] for n in g["nodes"]}, set(nodes["nodes"]))
                self.assertLessEqual(set(config["tracker"]["stages"]), {x["id"] for x in g["stages"]})

    def test_background_nodes_are_the_scheduled_ones(self):
        with s.team("slop") as (config, nodes):
            g = graph.build(nodes, config)
            background = {n["id"] for n in g["nodes"] if n["background"]}
            self.assertEqual(background, {"retro", "reconcile", "doctor-engine", "doctor-team"})
            reconcile = next(n for n in g["nodes"] if n["id"] == "reconcile")
            self.assertEqual(reconcile["triggers"][0]["cron"], nodes["schedules"]["reconcile"]["cron"])

    def test_old_layout_keeps_its_shared_stage_nodes(self):
        with s.team("team") as (config, nodes):
            g = graph.build(nodes, config)
            sharing = [n["id"] for n in g["nodes"] if n["stages"] == ["in-review"]]
            self.assertGreaterEqual(len(sharing), 2)  # review and join read one stage
            self.assertTrue(g["loops"] and "in-dev" in g["loops"][0])


class CustomNames(unittest.TestCase):
    def setUp(self):
        self.g = graph.build(NODES, CONFIG)

    def edges(self, kind):
        return {(e["source"], e["target"], e["node"], e.get("outcome")) for e in self.g["edges"] if e["kind"] == kind}

    def test_transitions_come_from_stage_effects_only(self):
        self.assertEqual(self.edges("transition"), {("alpha", "beta", "plan", "go"), ("alpha", "gamma", "plan", "park"),
                                                    ("beta", "alpha", "build", "back"), ("beta", "end", "build", "ship")})

    def test_comment_only_outcomes_make_no_edge(self):
        outcomes = {(n["id"], o["name"]): o for n in self.g["nodes"] for o in n["outcomes"]}
        for key in (("plan", "ask"), ("other", "note"), ("build", "talk")):
            self.assertTrue(outcomes[key]["comment_only"], key)
        self.assertFalse(any(e["outcome"] in ("ask", "note", "talk") for e in self.g["edges"]))
        self.assertFalse(outcomes[("sweep", "swept")]["comment_only"])

    def test_command_routing_wake_loops_conditions_and_branches(self):
        self.assertEqual(self.edges("route"), {("plan", "build", "plan", "go")})  # label x:build-<tier> wakes build
        self.assertEqual(self.edges("wake"), {("alpha", "plan", "plan", None), ("beta", "other", "other", None)})
        self.assertEqual(self.g["loops"], [["alpha", "beta"]])
        self.assertEqual([e["in_loop"] for e in self.g["edges"] if e["kind"] == "transition" and e["outcome"] in ("go", "back")], [True, True])
        park = next(o for n in self.g["nodes"] for o in n["outcomes"] if o["name"] == "park")
        self.assertEqual(park["condition"], {"if": {"blockers": "open"}})
        self.assertIn({"kind": "else", "node": "plan", "outcome": "park", "else": "go"}, self.g["branches"])
        stage_branch = next(b for b in self.g["branches"] if b["kind"] == "stage")
        self.assertEqual((stage_branch["stage"], stage_branch["targets"]), ("alpha", ["beta", "gamma"]))

    def test_several_nodes_share_a_stage_and_background_is_kept(self):
        beta = [n["id"] for n in self.g["nodes"] if "beta" in n["stages"]]
        self.assertEqual(sorted(beta), ["build", "other"])
        self.assertEqual([n["id"] for n in self.g["nodes"] if n["background"]], ["sweep"])

    def test_a_stage_less_outcome_has_no_invented_source(self):
        nodes = {"nodes": {"x": {"triggers": ["manual"], "when": {"always": True}, "role": "dev",
                                 "outcomes": {"go": {"effects": [{"stage": "beta"}]}}}}}
        edge = next(e for e in graph.build(nodes, CONFIG)["edges"])
        self.assertIsNone(edge["source"])
        self.assertEqual(graph.build(nodes, CONFIG)["loops"], [])


if __name__ == "__main__":
    unittest.main()

"""Tests for the read-only UI graph and eligibility projections."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))

import config  # noqa: E402
import nodes as nodes_mod  # noqa: E402
from factory_ui.eligibility import project_eligibility  # noqa: E402
from factory_ui.graph import project_graph  # noqa: E402


def test_generic_graph_keeps_custom_names_loops_and_scoped_effects():
    cfg = {"tracker": {"stages": {"queued": "flow:queued", "working": "flow:working",
                                    "done": "flow:done"},
                       "stage_kinds": {"queued": "waiting", "working": "active", "done": "final"}}}
    graph = {"nodes": {
        "dispatch": {"role": "script", "triggers": ["issues.labeled flow:queued"],
                     "when": {"stage": "queued", "blockers": "closed"},
                     "outcomes": {"begin": {"from": ["queued"], "effects": [
                         {"label": "flow:build"}, {"stage": "working"}]}}},
        "builder": {"role": "developer", "triggers": ["issues.labeled flow:build"],
                    "when": {"stage": "working"}, "outcomes": {
                        "retry": {"from": ["working"], "effects": [{"stage": "working"}]},
                        "finish": {"from": ["working"], "effects": [{"stage": "done"}]},
                        "comment-only": {"effects": [{"comment": "status", "on": "pr"}]},
                        "route-ticket": {"effects": [{"label": "flow:review"}]}}},
        "reviewer": {"role": "reviewer", "triggers": ["issues.labeled flow:review"],
                     "when": {"stage": ["working", "done"], "ci": ["green"]}, "outcomes": {}},
        "nightly": {"role": "ops", "triggers": ["schedule nightly"], "when": {"always": True},
                    "outcomes": {}},
        "person": {"role": "human", "triggers": ["manual"], "when": {"actor": "human"}, "outcomes": {}},
        "hook": {"role": "script", "triggers": ["webhook deploy"], "when": {"always": True},
                 "outcomes": {}},
    }}

    projected = project_graph(cfg, graph)
    by_id = {node["id"]: node for node in projected["nodes"]}
    assert projected["stages"] == [
        {"id": "queued", "name": "queued", "kind": "waiting"},
        {"id": "working", "name": "working", "kind": "active"},
        {"id": "done", "name": "done", "kind": "final"},
    ]
    assert by_id["reviewer"]["stages"] == ["working", "done"]
    assert by_id["nightly"]["background"] is True
    assert by_id["person"]["background"] is True
    assert by_id["hook"]["background"] is True
    assert by_id["builder"]["outcomes"][2]["name"] == "comment-only"
    assert by_id["builder"]["outcomes"][2]["targets"] == []
    assert "on pr" in by_id["builder"]["outcomes"][2]["effects"][0]

    edge_keys = {(edge["source"], edge["target"], edge["kind"]) for edge in projected["edges"]}
    assert ("stage:queued", "stage:working", "stage") in edge_keys
    assert ("stage:working", "stage:working", "stage") in edge_keys  # explicit loop
    assert ("node:dispatch", "node:builder", "command") in edge_keys
    assert ("node:builder", "node:reviewer", "command") in edge_keys
    assert not any(edge["source"] == "node:builder" and edge["kind"] == "stage"
                   and edge["label"].endswith("comment-only") for edge in projected["edges"])


def test_stages_follow_nonhuman_workflow_order_then_kind_groups():
    cfg = {"tracker": {"stages": {"finished": "flow:finished", "waiting": "flow:waiting",
                                    "build": "flow:build", "intake": "flow:intake",
                                    "triage": "flow:triage", "other": "flow:other"},
                       "stage_kinds": {"finished": "final", "waiting": "waiting",
                                       "build": "active", "intake": "active",
                                       "triage": "active", "other": "custom"}}}
    graph = {"nodes": {
        "human": {"role": "human", "triggers": ["manual"], "when": {"stage": "finished"},
                  "outcomes": {}},
        "wait": {"role": "script", "triggers": ["issues.labeled flow:waiting"],
                 "when": {"stage": "waiting"}, "outcomes": {}},
        "route": {"role": "router", "triggers": ["issues.labeled flow:build"],
                  "when": {"stage": "triage"}, "outcomes": {}},
    }}

    stages = project_graph(cfg, graph)["stages"]

    assert [stage["id"] for stage in stages] == ["waiting", "triage", "build", "intake", "finished", "other"]


def test_stage_edge_uses_node_stage_when_outcome_has_no_from():
    cfg = {"tracker": {"stages": {"queued": "flow:queued", "working": "flow:working"},
                       "stage_kinds": {"queued": "waiting", "working": "active"}}}
    graph = {"nodes": {"worker": {"role": "developer", "triggers": ["manual"],
                                    "when": {"stage": "queued"},
                                    "outcomes": {"start": {"effects": [{"stage": "working"}]}}}}}

    edges = project_graph(cfg, graph)["edges"]

    assert {"source": "stage:queued", "target": "stage:working",
            "label": "worker: start", "kind": "stage"} in edges


def test_v2_workflow_states_and_wakes_use_project_loader_shape():
    cfg = {"states": {"intake": {"kind": "waiting", "store": {"tag": "stage:intake"}},
                       "done": {"kind": "final", "store": {"pr": "merged"}}}}
    graph = {"nodes": {"triage": {"role": "router", "wakes_on": ["state intake"],
                                    "when": {"state": "intake"},
                                    "outcomes": {"close": {"from": ["intake"],
                                                              "effects": [{"state": "done"}]}}}}}

    projected = project_graph(cfg, graph)
    assert projected["nodes"][0]["stages"] == ["intake"]
    assert projected["nodes"][0]["outcomes"][0]["targets"] == ["done"]
    assert {edge["kind"] for edge in projected["edges"]} == {"trigger", "outcome", "stage"}


def test_only_added_labels_wake_the_same_scoped_command():
    cfg = {"tracker": {"stages": {}}, "commands": {
        "ticket-review": {"store": {"label": "flow:review"}},
        "pr-review": {"store": {"pr_label": "flow:review"}},
    }}
    graph = {"nodes": {
        "ticket-writer": {"role": "script", "triggers": ["manual"], "when": {"always": True},
                           "outcomes": {"add": {"effects": [{"label": "flow:review"}]},
                                        "remove": {"effects": [{"unlabel": "flow:review"}]}}},
        "pr-writer": {"role": "script", "triggers": ["manual"], "when": {"always": True},
                      "outcomes": {"add": {"effects": [{"label": "flow:review", "on": "pr"}]},
                                   "remove": {"effects": [{"unlabel": "flow:review", "on": "pr"}]}}},
        "ticket-reader": {"role": "reviewer", "triggers": ["issues.labeled flow:review"],
                          "when": {"always": True}, "outcomes": {}},
        "pr-reader": {"role": "reviewer", "triggers": ["pull_request.labeled flow:review"],
                      "when": {"always": True}, "outcomes": {}},
        "project-ticket-reader": {"role": "reviewer", "triggers": ["command ticket-review"],
                                  "when": {"always": True}, "outcomes": {}},
        "project-pr-reader": {"role": "reviewer", "triggers": ["command pr-review"],
                              "when": {"always": True}, "outcomes": {}},
    }}

    edges = project_graph(cfg, graph)["edges"]
    commands = {(edge["source"], edge["target"]) for edge in edges if edge["kind"] == "command"}

    assert ("node:ticket-writer", "node:ticket-reader") in commands
    assert ("node:pr-writer", "node:pr-reader") in commands
    assert ("node:ticket-writer", "node:pr-reader") not in commands
    assert ("node:pr-writer", "node:ticket-reader") not in commands
    assert ("node:ticket-writer", "node:project-ticket-reader") in commands
    assert ("node:pr-writer", "node:project-pr-reader") in commands
    assert not any(edge["source"] in ("node:ticket-writer", "node:pr-writer")
                   and edge["label"].startswith("flow:review (remove)") for edge in edges)
    assert {edge["label"] for edge in edges if edge["kind"] == "command"} >= {
        "flow:review on ticket (add)", "flow:review on pr (add)"}


def test_real_definition_compiler_outputs_project_through_loader():
    config.set_team(ROOT / "factory/tests/fixtures/slop")
    try:
        cfg = config.load()
        graph = nodes_mod.load()
        projected = project_graph(cfg, graph)
    finally:
        config.set_team(None)

    assert {stage["id"] for stage in projected["stages"]} >= {"draft", "ready", "in-dev"}
    assert {node["id"] for node in projected["nodes"]} >= {"intake", "dispatch", "implement-medium"}
    assert all(edge["source"].startswith(("stage:", "node:"))
               and edge["target"].startswith(("stage:", "node:")) for edge in projected["edges"])


def test_real_nodes_with_list_stage_and_ci_conditions_do_not_raise():
    config.set_team(ROOT / "factory/tests/fixtures/team")
    try:
        compiled = nodes_mod.load()["nodes"]
        ci_node = compiled["review"]
        result = project_eligibility("review", ci_node, {}, [])
        assert result["eligibility"] == "unknown"
        assert any(condition["key"] == "ci" and condition["result"] == "unknown"
                   for condition in result["conditions"])
    finally:
        config.set_team(None)

    config.set_team(ROOT / "factory/tests/fixtures/slop")
    try:
        compiled = nodes_mod.load()["nodes"]
        stage_node = compiled["ticket-reply"]
        result = project_eligibility("ticket-reply", stage_node, {}, [])
        assert result["eligibility"] == "unknown"
        assert any(condition["key"] == "stage" and condition["result"] == "unknown"
                   for condition in result["conditions"])
    finally:
        config.set_team(None)


def test_eligibility_uses_poll_event_and_unknown_for_missing_evidence():
    node = {"role": "router", "triggers": ["issues.labeled flow:queued"],
            "when": {"stage": "queued", "blockers": "closed"}, "outcomes": {}}
    facts = {"stage": "queued", "blockers_open": 0}
    event = ("labeled:12", {"node": "dispatch"}, {"eventType": "issues.labeled", "action": "labeled"})

    matched = project_eligibility("dispatch", node, facts, [event])
    assert matched["trigger"] == "matched"
    assert matched["eligibility"] == "matched"
    assert matched["execution"] == "unknown"

    missing = project_eligibility("dispatch", node, {"stage": "queued"}, [event])
    assert missing["eligibility"] == "unknown"
    assert missing["conditions"][1]["result"] == "unknown"

    no_event = project_eligibility("dispatch", node, facts, [])
    assert no_event["trigger"] == "unknown"
    assert no_event["eligibility"] == "unknown"

    completed_poll = project_eligibility("dispatch", node, {**facts, "_poll_complete": True}, [])
    assert completed_poll["trigger"] == "not-matched"
    assert completed_poll["eligibility"] == "not-matched"

    unmet = project_eligibility("dispatch", node, {**facts, "stage": "done"}, [event])
    assert unmet["trigger"] == "matched"
    assert unmet["eligibility"] == "not-matched"


def test_eligibility_handles_v2_state_and_never_treats_manual_as_a_run():
    node = {"role": "human", "wakes_on": ["manual"], "when": {"state": "intake"}, "outcomes": {}}
    result = project_eligibility("person", node, {"stage": "intake"}, [])

    assert result["trigger"] == "unknown"
    assert result["conditions"] == [{"key": "stage", "value": "intake", "result": "matched",
                                      "reason": "condition holds"}]
    assert result["eligibility"] == "unknown"
    assert result["execution"] == "unknown"

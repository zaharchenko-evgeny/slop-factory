"""Read-only projection of the compiled factory workflow for UI clients."""

from __future__ import annotations

import json
import re


def _text(value):
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _conditions(mapping):
    return [{"key": str(key), "value": _text(value)} for key, value in (mapping or {}).items()]


def _stage_data(config):
    tracker = config.get("tracker") or {}
    stages = tracker.get("stages")
    kinds = tracker.get("stage_kinds") or {}
    if isinstance(stages, dict):
        return [(str(stage), str(stage), kinds.get(stage, "active")) for stage in stages]

    # The v2 project loader exposes states instead of the engine tracker's stage map.
    states = config.get("states") or {}
    out = []
    for stage, spec in states.items():
        spec = spec if isinstance(spec, dict) else {}
        kind = spec.get("kind", kinds.get(stage, "active"))
        out.append((str(stage), str(stage), kind))
    return out


def _stage_labels(config):
    tracker = config.get("tracker") or {}
    stages = tracker.get("stages")
    if isinstance(stages, dict):
        return {str(label): str(stage) for stage, label in stages.items()}
    labels = {}
    for stage, spec in (config.get("states") or {}).items():
        store = (spec or {}).get("store") or {} if isinstance(spec, dict) else {}
        label = store.get("label", store.get("tag"))
        if label is not None:
            labels[str(label)] = str(stage)
    return labels


def _node_parts(raw):
    """Normalize engine nodes and v2 workflow nodes without compiling either a second time."""
    raw = raw if isinstance(raw, dict) else {}
    return (raw.get("role", ""), raw.get("triggers", raw.get("wakes_on", [])) or [],
            raw.get("when") or {}, raw.get("outcomes") or {})


def _stage_names(when, outcomes, triggers, stage_labels):
    found = []

    def add(value):
        for stage in value if isinstance(value, list) else [value]:
            if stage is not None and str(stage) not in found:
                found.append(str(stage))

    add(when.get("stage", when.get("state")))
    for outcome in outcomes.values():
        outcome = outcome if isinstance(outcome, dict) else {}
        add(outcome.get("from"))
    for trigger in triggers:
        match = re.match(r"^issues\.labeled\s+(.+)$", str(trigger))
        if match and match.group(1) in stage_labels:
            add(stage_labels[match.group(1)])
        match = re.match(r"^state\s+(.+)$", str(trigger))
        if match:
            add(match.group(1))
    return found


def _effect_text(effect):
    if not isinstance(effect, dict):
        return _text(effect)
    rendered = []
    for key, value in effect.items():
        if key == "on":
            continue
        scope = effect.get("on")
        suffix = f" on {scope}" if scope else ""
        rendered.append(f"{key}: {_text(value)}{suffix}")
    return "; ".join(rendered)


def _outcome_parts(outcomes):
    projected = []
    for name, raw in (outcomes or {}).items():
        raw = raw if isinstance(raw, dict) else {}
        conditions = []
        for key in ("from", "ci", "if", "enabled"):
            if key in raw:
                value = raw[key]
                if key == "if" and isinstance(value, dict):
                    conditions.extend(f"{k}: {_text(v)}" for k, v in value.items())
                else:
                    conditions.append(f"{key}: {_text(value)}")

        effects = raw.get("effects") or []
        targets = []
        for effect in effects:
            if not isinstance(effect, dict):
                continue
            target = effect.get("stage", effect.get("state"))
            if target is not None and str(target) not in targets:
                targets.append(str(target))
        explicit_to = raw.get("to")
        if explicit_to is not None:
            for target in explicit_to if isinstance(explicit_to, list) else [explicit_to]:
                if str(target) not in targets:
                    targets.append(str(target))
        projected.append({"name": str(name), "conditions": conditions,
                          "effects": [_effect_text(effect) for effect in effects], "targets": targets})
    return projected


def _trigger_token(trigger):
    """Return a comparable trigger token for an outcome event effect, when one exists."""
    trigger = str(trigger)
    aliases = {
        "pr reviewed": "pull_request_review.submitted",
        "pr pushed": "pull_request.synchronize",
        "pr opened": "pull_request.opened",
        "pr merged": "pull_request.merged",
        "pr commented": "issue_comment.created",
        "ticket closed": "issues.closed",
        "ticket commented": "issues.comment",
    }
    if trigger in aliases:
        return aliases[trigger]
    return trigger


def _emitted_triggers(outcome):
    """Events a compiled effect can wake when the compiler defines that relationship."""
    emitted = set()
    for effect in outcome.get("effects") or []:
        if not isinstance(effect, dict):
            continue
        if "review" in effect:
            emitted.add("pull_request_review.submitted")
    return emitted


def _command_refs(trigger, config):
    text = str(trigger)
    if text.startswith("command "):
        name = text[8:]
        return [("command", name, _command_scope(config, name))]
    match = re.match(r"^(?:issues|pull_request)\.labeled\s+(.+)$", text)
    if not match:
        return []
    scope = "pr" if text.startswith("pull_request.") else "ticket"
    return [("label", match.group(1), scope)]


def _command_scope(config, name):
    command = (config.get("commands") or {}).get(name) or {}
    store = command.get("store") or {}
    if not store:
        command = ((config.get("tracker") or {}).get("commands") or {}).get(name) or {}
        store = command.get("store") if isinstance(command.get("store"), dict) else command
    if "pr_label" in store:
        return "pr"
    if "label" in store or "tag" in store:
        return "ticket"
    return None


def _command_names(config, value, scope):
    names = []
    commands = {**((config.get("tracker") or {}).get("commands") or {}), **(config.get("commands") or {})}
    for name in commands:
        command = (config.get("commands") or {}).get(name) or {}
        store = command.get("store") or {}
        if not store:
            command = ((config.get("tracker") or {}).get("commands") or {}).get(name) or {}
            store = command.get("store") if isinstance(command.get("store"), dict) else command
        stored_value = store.get("pr_label", store.get("label", store.get("tag")))
        stored_scope = "pr" if "pr_label" in store else "ticket" if "label" in store or "tag" in store else None
        if stored_value == value and stored_scope == scope:
            names.append(("command", str(name), scope))
    return names


def _effect_commands(effect, config):
    if not isinstance(effect, dict):
        return []
    if "command" in effect:
        name = str(effect["command"])
        return [("command", name, _command_scope(config, name))]
    if "label" in effect:
        scope = "pr" if effect.get("on") == "pr" else "ticket"
        label = str(effect["label"])
        return [("label", label, scope), *_command_names(config, label, scope)]
    # Removing a label or command cannot wake a labeled trigger.
    return []


def _ordered_stages(stage_defs, raw_nodes, stage_labels):
    """Use node input order, then append remaining stages by kind."""
    by_id = {stage_id: (stage_id, name, kind) for stage_id, name, kind in stage_defs}
    ids = set(by_id)
    seen = []
    for raw in raw_nodes.values():
        role, triggers, when, outcomes = _node_parts(raw)
        if role == "human":
            continue
        for stage in _stage_names(when, outcomes, [str(t) for t in triggers], stage_labels):
            if stage in ids and stage not in seen:
                seen.append(stage)
    remaining = [stage_id for stage_id, _, _ in stage_defs if stage_id not in seen]
    order = [*seen]
    for kind in ("active", "waiting", "final"):
        order.extend(stage_id for stage_id in remaining if by_id[stage_id][2] == kind)
    order.extend(stage_id for stage_id in remaining if by_id[stage_id][2] not in ("active", "waiting", "final"))
    return [by_id[stage_id] for stage_id in order]


def _command_edge_label(command, outcome_name):
    kind, value, scope = command
    name = str(value) if kind == "label" else f"command {value}"
    if scope:
        name += f" on {scope}"
    return f"{name} ({outcome_name})"


def project_graph(config, graph):
    """Project compiled engine nodes or v2 workflow nodes into a generic graph."""
    graph = graph if isinstance(graph, dict) else {}
    raw_nodes = graph.get("nodes") or {}
    stage_defs = _ordered_stages(_stage_data(config), raw_nodes, _stage_labels(config))
    stage_ids = {stage_id for stage_id, _, _ in stage_defs}
    stage_labels = _stage_labels(config)
    stages = [{"id": stage_id, "name": name, "kind": kind} for stage_id, name, kind in stage_defs]

    nodes, edges = [], []
    normalized = {}
    for node_id, raw in raw_nodes.items():
        role, triggers, when, outcomes = _node_parts(raw)
        trigger_strings = [str(trigger) for trigger in triggers]
        stage_membership = [stage for stage in _stage_names(when, outcomes, trigger_strings, stage_labels)
                            if stage in stage_ids]
        background = bool(trigger_strings) and all(
            trigger == "manual" or trigger.startswith(("schedule ", "webhook ")) for trigger in trigger_strings
        )
        projected_outcomes = _outcome_parts(outcomes)
        nodes.append({"id": str(node_id), "role": str(role), "stages": stage_membership,
                      "triggers": trigger_strings, "conditions": _conditions(when),
                      "outcomes": projected_outcomes, "background": background})
        normalized[node_id] = (trigger_strings, when, outcomes)

    # An outcome may move from its declared source stage to an explicit stage effect.
    for node_id, (_, when, outcomes) in normalized.items():
        for outcome_name, raw_outcome in outcomes.items():
            outcome = raw_outcome if isinstance(raw_outcome, dict) else {}
            targets = []
            for effect in outcome.get("effects") or []:
                if isinstance(effect, dict):
                    target = effect.get("stage", effect.get("state"))
                    if target is not None and str(target) in stage_ids:
                        targets.append(str(target))
            if outcome.get("to") is not None:
                for target in outcome["to"] if isinstance(outcome["to"], list) else [outcome["to"]]:
                    if str(target) in stage_ids:
                        targets.append(str(target))
            sources = outcome.get("from") or when.get("stage", when.get("state")) or []
            sources = sources if isinstance(sources, list) else [sources]
            for source in sources:
                if str(source) in stage_ids:
                    for target in targets:
                        edges.append({"source": f"stage:{source}", "target": f"stage:{target}",
                                      "label": f"{node_id}: {outcome_name}", "kind": "stage"})
            for target in dict.fromkeys(targets):
                edges.append({"source": f"node:{node_id}", "target": f"stage:{target}",
                              "label": str(outcome_name), "kind": "outcome"})

    # Stage wakes and command/event handoffs connect only when the compiled graph names both ends.
    for node_id, (triggers, _, _) in normalized.items():
        for trigger in triggers:
            stage = None
            match = re.match(r"^state\s+(.+)$", trigger)
            if match and match.group(1) in stage_ids:
                stage = match.group(1)
            match = re.match(r"^issues\.labeled\s+(.+)$", trigger)
            if match and match.group(1) in stage_labels:
                stage = stage_labels[match.group(1)]
            if stage:
                edges.append({"source": f"stage:{stage}", "target": f"node:{node_id}",
                              "label": trigger, "kind": "trigger"})

    for source_id, (_, _, outcomes) in normalized.items():
        for outcome_name, raw_outcome in outcomes.items():
            outcome = raw_outcome if isinstance(raw_outcome, dict) else {}
            for effect in outcome.get("effects") or []:
                for command in _effect_commands(effect, config):
                    for target_id, (triggers, _, _) in normalized.items():
                        if any(command in _command_refs(trigger, config) for trigger in triggers):
                            edges.append({"source": f"node:{source_id}", "target": f"node:{target_id}",
                                          "label": _command_edge_label(command, outcome_name), "kind": "command"})
            emitted = _emitted_triggers(outcome)
            for target_id, (triggers, _, _) in normalized.items():
                if emitted.intersection(_trigger_token(trigger) for trigger in triggers):
                    edges.append({"source": f"node:{source_id}", "target": f"node:{target_id}",
                                  "label": str(outcome_name), "kind": "trigger"})

    # Keep output deterministic, including when input mappings come from YAML.
    return {"stages": stages, "nodes": nodes, "edges": edges}

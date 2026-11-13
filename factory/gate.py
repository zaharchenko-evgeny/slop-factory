#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The gate: decide whether a node runs (check) and apply an outcome's effects (apply).

Usage: gate.py check --node N --event FILE [--tracker github|memory:FILE]
       gate.py apply --node N --outcome O --payload FILE [--tracker github|memory:FILE]
       gate.py apply --node N --handoff FILE [--event FILE]   (outcome and payload derived from the handoff)
       --spec FILE (check, apply, sweep): the node spec of the automation (deploy plan); no engine/nodes.yaml is read
       gate.py sweep [--tracker github] [--dry-run] [--stale] [--event FILE | --issue N] (no agent: lists the tickets itself,
                                                                 one stage per ticket;
                                                                 --stale also re-fires nodes whose run died silently)
Exit 0 go / done, 10 no go (stale, duplicate or the node no longer matches), 2 bad event, payload, node
or outcome, 1 a failing effect (the stage is untouched: it is the last effect).
An event is JSON {id, ticket, head?, actor?}. A payload is JSON {ticket, event?, head?, actor?, ...}
plus one field per outcome effect that needs data (the error names the field).
`check --event` also takes the raw trigger payload (an `issues`, `pull_request` or `pull_request_review`
event): the ticket is the issue number or the `<n>` of the PR branch `ez/<n>-*` (none: exit 10).
`--tracker memory:FILE` runs on a JSON file {ticket: record} with the in-memory fake; `--tracker github`
uses factory/factory_plugins/github/gate.py (bot login from `targets.air.bot_login`).
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
if __name__ == "__main__":  # run as a script: `import gate` (a plugin's code) must get this module, and its `Bad`
    sys.modules.setdefault("gate", sys.modules[__name__])
import config as config_mod  # noqa: E402
import nodes as nodes_mod  # noqa: E402
from derive import clean_notes, review_at_head  # noqa: E402
import effects as effects_mod  # noqa: E402
from gate_tracker import OnVcs, hosts_prs, overlay  # noqa: E402
from gate_port import REF, MemorySystem, number_of, split_ticket, ticket_from_ref  # noqa: E402
import vocab  # noqa: E402
import definition  # noqa: E402
import plugins  # noqa: E402  (ADR-0026: a plugin's code is imported when the core calls it)
import scopes  # noqa: E402

ROOT = nodes_mod.ROOT
GO, NO_GO, BAD, FAILED = 0, 10, 2, 1
SKIP = "skip: {ticket} outside every environment scope"  # #1069: the one line of a step on no environment's ticket
AGENT_KEYS = ("agent", "persona", "mentions", "mentions_on")  # node spec fields of a project with `agents:`


class Bad(Exception):
    pass


def value(want, allowed):
    if want not in allowed:
        raise Bad(f"bad condition value {want!r}: allowed {', '.join(allowed)}")


def holds(when, facts, event):
    """Every key of `when` must hold (the vocabulary is nodes.WHEN_KEYS)."""
    def one(key, want):
        if key == "stage":
            return facts["stage"] in (want if isinstance(want, list) else [want])
        if key == "blockers":
            return (facts["blockers_open"] == 0) == (want == "closed")
        if key == "focus":
            miss = facts["focus_missing"]
            focus = event.get("focus", miss[0] if miss else None)
            return focus in miss
        if key == "round":
            value(want, ("complete", "complete-at-head"))
            return facts["round_complete"] and (want != "complete-at-head" or facts["round_head"] == facts["head"])
        if key == "fix-rounds":
            value(want, ("below-cap", "at-cap"))
            return (facts["fix_rounds"] < facts["fix_cap"]) == (want == "below-cap")
        if key == "ci":
            if isinstance(want, list):  # any of these
                return facts["ci"] in want
            value(want, ("green", "red", "pending", "none"))
            return facts["ci"] == want
        if key == "actor":
            return event.get("actor") == want
        if key == "pr-state":
            value(want, ("open", "merged", "closed", "none"))
            return facts.get("pr_state", "none") == want
        if key == "always":
            return bool(want)
        if key == "label":  # ADR-0021: every label listed is on the ticket or its PR (an `on: pr` label)
            have = set(facts.get("attrs") or []) | set(facts.get("pr_labels") or [])
            return all(x in have for x in (want if isinstance(want, list) else [want]))
        if key == "pr":
            value(want, ("open", "merged", "behind", "conflicting", "none"))
            return {"open": facts.get("pr_state") == "open", "merged": facts.get("pr_state") == "merged",
                    "behind": bool(facts.get("behind")), "conflicting": bool(facts.get("conflict")),
                    "none": facts.get("pr_state", "none") == "none"}[want]
        if key == "review":
            value(want, ("approved-at-head", "changes-at-head", "none-at-head"))
            return review_at_head(facts) == want.removesuffix("-at-head").replace("approved", "approve")
        if key == "ticket":
            value(want, ("open", "closed"))
            return facts.get("issue_state", "open") == want
        if key == "children":
            value(want, ("open", "closed"))
            return ("children_open" in facts) and (facts["children_open"] == 0) == (want == "closed")
        return bool(facts["due"]) == bool(want)  # due
    return all(one(k, v) for k, v in when.items())


def spec_nodes(path):
    """A `nodes` mapping from a node spec file: the spec's node and the script nodes it `runs`."""
    try:
        spec = json.loads(Path(path).read_text())
        out = {}
        for s in [spec, *(spec.get("runs") or {}).values()]:
            node = {k: s[k] for k in ("role", "when", "outcomes", "else_tier", "tiers", "routine", "output", *AGENT_KEYS) if k in s}
            node.update({"tier": s["tier"]} if s.get("tier") else {}, **({"focuses": [s["focus"]]} if s.get("focus") else {}))
            out[s["node"]] = node
    except (OSError, ValueError, KeyError, AttributeError, TypeError) as e:
        raise Bad(f"malformed spec: {e!r}")
    return {"nodes": out}


def load_node(name, nodes):
    node = (nodes.get("nodes") or {}).get(name)
    if node is None or node.get("enabled") is False:
        raise Bad(f"unknown node: {name}")
    return node


class NoTicket(Exception):
    pass


class MissingNumber(Exception):
    pass


def pr_ticket(code_host, number, repository=None):
    """The ticket of PR `number`, looked up in the payload's repository when it names one (ADR-0017)."""
    name = repository.get("full_name") if isinstance(repository, dict) else None
    return code_host.ticket_for_pr(number, name) if name else code_host.ticket_for_pr(number)


def vcs_trigger(d, code_host=None):
    """`{kind, number, action, repository, ticket}` of a payload a vcs plugin reads itself (the code host's own
    `trigger`, then the `gate.trigger` hook of the selected plugins: space's merge requests), else None."""
    own = getattr(code_host, "trigger", None) if code_host is not None else None
    for parse in [*([own] if callable(own) else []), *plugin_registry().hook("gate.trigger")]:
        ev = parse(d)
        if ev is not None:
            return ev
    return None


def from_trigger(d, code_host=None):
    """Extract the event kind and ticket number; tracker facts supply mutable metadata."""
    event_name = d.get("eventType") or d.get("event_name")
    ev = vcs_trigger(d, code_host)
    if ev is not None:
        ticket = ev["ticket"] or (code_host.ticket_for_pr(ev["number"], ev["repository"])
                                  if code_host and ev["repository"] else None)
        if ticket is None:
            raise NoTicket(f"{ev['kind'].replace('_', ' ')} {ev['number']} is not on an ez/<n>- branch")
        return {"id": f"{ev['kind']}:{ticket}:{ev['action']}", "ticket": ticket, "event_name": "pull_request",
                "action": ev["action"]}
    if not any(isinstance(d.get(key), dict) for key in ("pull_request", "issue", "review")):
        if isinstance(d.get("data"), dict):
            wrapper = d
            d = d["data"]
            event_name = event_name or wrapper.get("eventType") or wrapper.get("event_name")
    pr, issue, review = d.get("pull_request"), d.get("issue"), d.get("review")
    pr_comment = isinstance(issue, dict) and isinstance(issue.get("pull_request"), dict) and not isinstance(pr, dict)
    is_pr = isinstance(pr, dict) or pr_comment or str(event_name or "").startswith("pull_request")
    src = pr or issue or d
    number = src.get("number") if isinstance(src, dict) else None
    # a YouTrack ticket id (PROJECT-N) is the ticket itself: `factory begin --issue LME-7` (#1321)
    tracker_id = not is_pr and isinstance(number, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*-\d+", number)
    if not tracker_id and (not isinstance(number, int) or number < 1):
        raise MissingNumber("payload has no issue or PR number")
    name = "pull_request_review" if review and is_pr else "issue_comment" if pr_comment \
        else "pull_request" if is_pr else "issues"  # a comment on a PR arrives as an issue event of its number
    if is_pr:
        ticket = pr_ticket(code_host, number, d.get("repository")) if code_host else None
        if ticket is None and not live_pr_ticket(code_host):  # a live lookup is the only source for GitHub
            ticket = ticket_from_ref(((pr or {}).get("head") or {}).get("ref")) or ticket_from_ref((pr or {}).get("title"))
        if ticket is None:
            raise NoTicket(f"PR #{number} is not on an ez/<n>- branch")
    else:
        repository = (d.get("repository") or {}).get("full_name") if isinstance(d.get("repository"), dict) else None
        ref = getattr(code_host, "ticket_ref", None)  # ADR-0024: an issue of another ticket repository
        ticket = number if tracker_id else ref(repository, number) if repository and callable(ref) else number
    action = d.get("action") or "event"
    return {"id": f"{name}:{ticket}:{action}", "ticket": ticket, "event_name": name, "action": action}


def live_pr_ticket(code_host):
    """Whether the vcs of `code_host` only looks a PR's ticket up live (its `live_pr_ticket` flag, set by the github
    plugin's vcs)."""
    if isinstance(code_host, scopes.ScopedTracker):  # environments with their own trackers: the vcs a PR is looked up on
        code_host = code_host.vcs_for()[1]
    return bool(getattr(code_host, "live_pr_ticket", False))


def tracker_of(config, **runners):
    """The gate tracker of `config`, from the plugin that provides `tracker.adapter` (ADR-0026): github-issues from
    the github plugin, youtrack from the youtrack plugin (its tickets in YouTrack, its pull requests on the vcs).
    `runners` are passed to `from_config` (the local poll's `gh`, tests)."""
    if config.get("ticket_places") and not runners:  # ADR-0027: each place's tickets on its own tracker (#1259)
        return systems(config)[0]
    t = config.get("tracker")
    name = (t.get("adapter") if isinstance(t, dict) else None) or "github-issues"
    tracker_cls = plugins.registry(config).impl("tracker", name)
    if tracker_cls is None:
        raise ValueError(f"tracker.adapter {name}: no selected plugin provides this tracker")
    return tracker_cls.from_config(**runners)


def env_descriptor(config, env):
    """The descriptor of project environment `env` (#1069): its merged tracker block as `tracker`, its repositories
    (its main one the primary) as `repositories`, and its main repository as the code host of its pull requests."""
    by_name = {r["name"]: r for r in config.get("repositories") or []}
    names = [n for n in ((config.get("environments") or {}).get(env) or {}).get("repositories") or [] if n in by_name]
    repos = [{**by_name[n], "role": "primary" if i == 0 else "project"} for i, n in enumerate(names)] \
        or config.get("repositories") or []
    main = next((r["url"] for r in repos if r.get("role") == "primary"), None)
    out = {**{k: v for k, v in config.items() if k != "vcs"}, "tracker": scopes.project_envs(config)[env],
           "repositories": repos, "code_host": config_mod.vcs(config, env)}  # the environment's vcs (#1070)
    if main:
        out["code_host"] = {**out["code_host"], "repository": "/".join(main.rstrip("/").split("/")[-2:])}
    return out


def vcs_class(config, block, where="vcs"):
    """The class the selected plugin provides for vcs block `block` (github when it names none)."""
    name = block.get("adapter") or "github"
    why = plugins.impl_error(config, "vcs", name, where)
    if why:
        raise ValueError(why)
    return plugins.registry(config).impl("vcs", name)


def beside(tracker, vcs):
    """(tracker, vcs), the tracker an `OnVcs` when the vcs applies its own PR effects (gate_tracker.py)."""
    return (OnVcs.of(tracker, vcs) if hosts_prs(vcs) else tracker), vcs


def env_systems(config, env, **runners):
    """The (tracker, vcs) of project environment `env`, from the plugins that provide them; `runners` are passed to
    both `from_config` (tests stub them)."""
    c = env_descriptor(config, env)
    block = c["tracker"]
    tracker_cls = plugins.registry(config).impl("tracker", scopes.adapter(block))
    if tracker_cls is None:
        raise ValueError(f"environments.{env}.tracker.adapter {scopes.adapter(block)}: no selected plugin provides this tracker")
    tracker = tracker_cls.from_config(descriptor=c, **runners)
    vcs_cls = vcs_class(config, config_mod.vcs(c), f"environments.{env}.vcs")
    return beside(tracker, vcs_cls.from_config(descriptor=c, **({"run": tracker.run} if hasattr(tracker, "run") else {})))


def default_place(config):
    """The key of the place of the config's own tracker (hook `gate.default_place`), else None."""
    return next((key for hook in plugins.registry(config).hook("gate.default_place") if (key := hook())), None)


def place_keys(config):
    """({key: tracker block}, {key: [owner/repo, ...]}) of the ADR-0027 ticket places: the default place for the
    config's tracker's repositories (when it has any), then `place:<owner/repo>` per `ticket_places` entry."""
    t, envs, repos = config.get("tracker") or {}, {}, {}
    if t.get("repositories") and (key := default_place(config)):
        envs[key], repos[key] = t, list(t["repositories"])
    for p in config["ticket_places"]:
        key = place_key(config["ticket_places"], p)
        envs[key], repos[key] = scopes.merged(config, p["tracker"]), p["repositories"]
    return envs, repos


def place_key(places, place):
    """The stable key for a ticket place, including a second tracker for the same repository."""
    repository = place["repository"]
    duplicate = 0
    for candidate in places:
        if candidate is place:
            break
        duplicate += candidate["repository"] == repository
    return f"place:{repository}" if not duplicate else f"place:{repository}#{duplicate + 1}"


def place_systems(config, key, **runners):
    """The (tracker, vcs) of ticket place `key` (`place_keys`): the GitHub tracker of the config, or a place's own
    tracker on its repository's vcs."""
    if key == default_place(config):
        tracker = tracker_of({k: v for k, v in config.items() if k != "ticket_places"}, **runners)
        vcs_cls = vcs_class(config, config_mod.vcs(config))
        return beside(tracker, vcs_cls.from_config(**({"run": tracker.run} if hasattr(tracker, "run") else {})))
    place = next(p for p in config["ticket_places"] if place_key(config["ticket_places"], p) == key)
    by = {"/".join(r["url"].rstrip("/").split("/")[-2:]).lower(): r for r in config.get("repositories") or []}
    repos = [{**by[n.lower()], "role": "primary" if i == 0 else "project"} for i, n in enumerate(place["repositories"])
             if n.lower() in by]
    tracker_block = scopes.merged(config, place["tracker"])
    where = lambda b: (scopes.adapter(b), b.get("url"), b.get("project"))  # noqa: E731  (one tracker, another scope)
    if same := {p["repository"]: p["tracker"]["scope"] for p in config["ticket_places"]
                if p["tracker"].get("scope") and where(scopes.merged(config, p["tracker"])) == where(tracker_block)}:
        tracker_block = {**tracker_block, "repository_scopes": same}  # #1259: a child takes its repository's scope
    c = {**{k: v for k, v in config.items() if k not in ("vcs", "ticket_places")},
         "tracker": tracker_block, "repositories": repos,
         "code_host": {**(place.get("vcs") or {}), "repository": place["repository"]}}
    block = c["tracker"]
    tracker_cls = plugins.registry(config).impl("tracker", scopes.adapter(block))
    if tracker_cls is None:
        raise ValueError(f"{key}: tracker.adapter {scopes.adapter(block)}: no selected plugin provides this tracker")
    tracker = tracker_cls.from_config(descriptor=c, **runners)
    vcs_cls = vcs_class(config, c["code_host"], f"{key}.vcs")
    return beside(tracker, vcs_cls.from_config(descriptor=c, **({"run": tracker.run} if hasattr(tracker, "run") else {})))


def run_place(config, env, keys):
    """The ticket place the run's environment `env` owns: its writable repository's, when that is one of `keys`
    (#1330). An environment that writes nothing (intake) owns no place and sees every place's tickets."""
    writable = ((config.get("environments") or {}).get(env) or {}).get("writable") or []
    full = {r.get("name"): "/".join(r["url"].rstrip("/").split("/")[-2:]) for r in config.get("repositories") or []}
    for name in writable:
        prefix = f"place:{full.get(name, name)}"
        key = next((key for key, block in keys.items() if (key == prefix or key.startswith(f"{prefix}#"))
                    and block.get("scope")), f"place:{full.get(name, name)}")
        if key in keys:
            return key
    return None


def systems(config):
    """The (tracker, vcs) of `config`, from the plugins that provide them; the vcs is the run's environment's
    (FACTORY_ENV: its own `vcs:` over the top-level one, #1070). With environments that have their own trackers
    (#1069), both are one `scopes.ScopedTracker`: each ticket goes to its environment's tracker, the run's environment
    first."""
    env = os.environ.get("FACTORY_ENV")
    if config.get("ticket_places"):  # ADR-0027: the GitHub tickets on one tracker, each other place on its own
        envs, repos = place_keys(config)
        scoped = scopes.ScopedTracker(config, place_systems, run_place(config, env, envs), envs, repos)
        return scoped, scoped
    if scopes.project_envs(config):
        scoped = scopes.ScopedTracker(config, env_systems, env)
        return scoped, scoped
    tracker = tracker_of(config)
    block = config_mod.vcs(config, env)
    vcs_cls = vcs_class(config, block, f"environments.{env}.vcs" if block != config_mod.vcs(config) else "vcs")
    own = {} if block == config_mod.vcs(config) else \
        {"descriptor": {**{k: v for k, v in config.items() if k != "vcs"}, "code_host": block}}
    run = {"run": tracker.run} if hasattr(tracker, "run") else {}  # one runner, so github_calls counts both
    return beside(tracker, vcs_cls.from_config(**own, **run))


def read(path, what, raw=False, code_host=None):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise Bad(f"malformed {what}: {e}")
    if isinstance(data, dict) and raw and "ticket" not in data:
        return from_trigger(data, code_host)
    if not isinstance(data, dict) or "ticket" not in data:
        raise Bad(f"malformed {what}: needs an object with `ticket`")
    return data


NOTE_AUDIENCE = {"developer": ("developer",), "reviewer": ("reviewer",)}  # the `for` values a role reads, besides `any`
COMPLEXITY_TIER = {"small": "cheap", "medium": "mid", "large": "strong"}
FIX_TIERS = ("cheap", "mid", "strong")


def slices(split):
    """The proposed slices of a router `split` as {title, body} children: a list of {title, body} objects (a
    per-repository child also names its `repo`, `subsystem` and the repositories it is `after`), else
    lines (a string, or a list of strings) whose text is both title and body."""
    if isinstance(split, list) and all(isinstance(x, dict) for x in split):
        return [{"title": str(x["title"]).strip(), "body": str(x.get("body") or x["title"]).strip(),
                 **{k: x[k] for k in ("repo", "subsystem", "after") if x.get(k)}} for x in split]
    lines = split.splitlines() if isinstance(split, str) else [str(x) for x in split or []]
    out = [x.strip().lstrip("-").strip() for x in lines]
    return [{"title": x.split(" — ")[0].strip(), "body": x} for x in out if x and x != "none"]


def intake_fields(h):
    """Payload fields the outcomes need and the handoff names otherwise.
    With a `size_estimate` (the router's intake): complexity and tier from the size, `questions` from `reason`,
    `children` from `slices`."""
    if "size_estimate" not in h:
        return {}
    size = re.search(r"\d+", str(h["size_estimate"]))
    n = int(size[0]) if size else 0
    proposed = h.get("proposed_checks")
    proposed = "\n".join(f"- {x}" for x in proposed) if isinstance(proposed, list) else str(proposed or "")
    proposal = (f"\n\nProposed acceptance checks (accept by editing the ticket, or by `{vocab.stage('triage')}` with this pasted):\n"
                f"{proposed}" if proposed.strip() not in ("", "none") else "")
    return {"complexity": "small" if n <= 150 else "medium" if n <= 400 else "large",
            "tier": "cheap" if n <= 150 else "mid" if n <= 400 else "strong",
            "questions": effects_mod.tagged(f"Needs answers before work starts: {h.get('reason', '')}{proposal}"),
            "children": slices(h.get("slices"))}


def outcome_of(h):
    """The node outcome a handoff names: `outcome`, else a reviewer `verdict`."""
    if h.get("outcome"):
        return h["outcome"]
    if "verdict" in h:
        verdict = h["verdict"]
        verdicts = {"approve": "approve", "approved": "approve", "changes": "changes",
                    "changes-requested": "changes"}
        if not isinstance(verdict, str) or verdict not in verdicts:
            raise Bad(f"unknown reviewer verdict: {verdict!r}")
        return verdicts[verdict]
    return h.get("status")


LESSON_MARKER = "<!-- factory:lesson -->"


def lesson_effects(ticket, node, payload):
    """ADR-0021: a result's `lessons` as one marked comment on the ticket and the lesson label, which retro collects.
    Each lesson is `{text, scope: project|factory|engine, about, evidence}`; only `text` is required."""
    lessons = payload.get("lessons") or []
    if not isinstance(lessons, list) or not all(isinstance(x, dict) and str(x.get("text") or "").strip() for x in lessons):
        raise Bad("lessons must be a list of objects with `text`")
    if not lessons:
        return []
    lines = [f"- [{x.get('scope', 'project')}] {x.get('about') or 'general'}: {' '.join(str(x['text']).split())}"
             + (f" (evidence: {x['evidence']})" if x.get("evidence") else "") for x in lessons]
    body = effects_mod.tagged(f"Lessons from `{node}`\n{LESSON_MARKER}\n" + "\n".join(lines))
    if payload.get("run_id"):
        body += f"\n\nFactory-Run: {payload['run_id']}"
    return [{"kind": "comment", "ticket": ticket, "value": body}, {"kind": "label", "ticket": ticket, "value": vocab.lesson()}]


def agent_of(node):
    """The agent of workflow node `node` in a definition team's factory (ADR-0021), else the node's own name."""
    import definition
    defn = definition.load(config_mod.team_root())
    return ((defn["workflow"].get("nodes") or {}).get(node) or {}).get("agent", node)


def comment_marker(node):
    """`<!-- factory:from <agent>[ reply] -->`: which agent wrote a comment, so a mention in it can be checked."""
    import definition
    reply = definition.settings(definition.load(config_mod.team_root()), node).get("mode") == "reply"
    return f"<!-- factory:from {agent_of(node)}{' reply' if reply else ''} -->"


def node_mode(node):
    """A definition team's node `settings.mode` (fix, update, reply, ...), or None (ADR-0021)."""
    import definition
    return definition.settings(definition.load(config_mod.team_root()), node).get("mode")


def definition_team():
    """True when the team defines its factory in factory.yaml + workflow.yaml (ADR-0021)."""
    import definition
    return definition.present(config_mod.team_root())


def ci_state(tracker, ticket, wait_seconds=0, poll=30, sleep=None):
    """{ci, head, failing}: CI at the ticket's PR head, after waiting up to `wait_seconds` for it to conclude
    (ADR-0021: a reviewer runs `factory ci --wait` before its verdict, so no CI event has to wake anyone)."""
    import time
    sleep = sleep or time.sleep
    waited = 0
    while True:
        facts = tracker.facts(ticket)
        if facts.get("ci") in ("green", "red") or waited >= wait_seconds:
            break
        sleep(poll)
        waited += poll
    failing = tracker.ci_failure(facts["head"]) if facts.get("ci") == "red" and facts.get("head") \
        and hasattr(tracker, "ci_failure") else None
    return {"ci": facts.get("ci"), "head": facts.get("head"), "failing": failing}


def decide(name, ticket, tracker, nodes, dry_run=False):
    """ADR-0021 `factory decide`: apply the first outcome of node `name` whose guards hold (`from`, `ci`, `if`), for a
    node whose agent only runs this command. (status, {outcome, ...}); no outcome holding is NO_GO."""
    node = load_node(name, nodes)
    facts = tracker.facts(ticket)
    for outcome, out in node["outcomes"].items():
        if out.get("enabled") is False or (out.get("from") and facts["stage"] not in out["from"]) \
                or (out.get("ci") and facts["ci"] not in out["ci"]) \
                or (out.get("if") and not guard_holds(out["if"], facts, tracker, ticket)):
            continue
        if dry_run:  # the outcome whose guards hold, nothing written
            return GO, {"outcome": outcome, "applied": False, "dry_run": True}
        payload = {"ticket": ticket, "reason": effects_mod.tagged(f"{name}: {outcome}")}
        status, res = apply(name, outcome, payload, tracker, nodes)
        return status, {"outcome": outcome, **res}
    return NO_GO, {"outcome": None, "applied": False, "reason": "no outcome's guards hold"}


def merge_refused(effects, facts, tracker=None):
    """ADR-0021: a `merge` effect keeps its own invariants, whatever chose it: the current head is approved by the
    agent reviews at it, none asks for changes, CI is green there and the branch is not behind (the merge's `needs`,
    factory/effects.py). None when it may merge, else the reason. The old layout's merge node checks its gates before
    it gets here."""
    if not any(e["kind"] == "merge" for e in effects) or not definition_team():
        return None
    return effects_mod.refused(effects, facts, tracker)


def unblocked(effects, tracker, facts):
    """ADR-0021, automatic: when this outcome moves the ticket to a final stage, each ticket it blocks that is
    `ready` with no other open blocker gets `ready` removed and added again, which fires its trigger (re-adding a
    present label fires nothing). `reconcile` does the same for a ticket a person closed."""
    if not definition_team() or not hasattr(tracker, "blocking"):
        return []
    final = {s for s, k in (config_mod.load()["tracker"].get("stage_kinds") or {}).items() if k == "final"}
    to = next((e["value"] for e in effects if e["kind"] == "stage"), None)
    if to not in final:
        return []
    ticket, label, out = next(e["ticket"] for e in effects if e["kind"] == "stage"), vocab.stage("ready"), []
    for d in tracker.blocking(ticket):
        n = d["number"] if isinstance(d, dict) else d
        f = tracker.facts(n)
        others = f.get("blockers_open", 0) - (1 if facts.get("issue_state", "open") == "open" else 0)
        if f["stage"] == "ready" and others <= 0:
            out += [{"kind": "unlabel", "ticket": n, "value": label}, {"kind": "label", "ticket": n, "value": label}]
    return out


def group_unlabels(effects, facts):
    """ADR-0021: a label `<group>:<value>` outside the stage namespace holds one value per group; adding one removes
    the group's other values (`risk:high` removes `risk:low`) on the same ticket or PR."""
    labels = [e for e in effects if e["kind"] == "label" and ":" in str(e.get("value"))]
    if not labels:
        return []
    stages = config_mod.load()["tracker"].get("stages") or {}
    namespaces = {str(v).partition(":")[0] for v in stages.values() if ":" in str(v)}
    out = []
    for e in labels:
        group = str(e["value"]).partition(":")[0]
        if group in namespaces:
            continue
        have = facts.get("pr_labels" if e.get("on") == "pr" else "attrs") or []
        out += [{"kind": "unlabel", "ticket": e["ticket"], "value": x, **({"on": e["on"]} if e.get("on") else {})}
                for x in have if x.startswith(f"{group}:") and x != e["value"]]
    return out


def _resolved_ids(body):
    """Finding ids a review summary says are resolved (not the finding lines, not `not resolved`)."""
    ids = set()
    for line in body.splitlines():
        if line.startswith("**") or re.search(r"\b(not|un)\s?resolved\b", line, re.I) or not re.search(r"\bresolved\b", line, re.I):
            continue
        ids |= set(re.findall(r"\b([A-Z]\d+)\b", line))
    return ids


def _open_blocking(reviews, focus):
    """Blocking finding ids still open in the agent reviews of `focus`: a `B<n>` stays open until a later review of
    the focus says it is resolved or approves."""
    open_ids = set()
    for r in reviews:
        if not r.get("agent") or (r.get("focus") or "review") != (focus or "review"):
            continue
        open_ids -= _resolved_ids(r.get("body") or "")
        if r.get("verdict") == "approve":
            open_ids.clear()
        open_ids |= {str(i) for i in r.get("findings") or [] if str(i).startswith("B")}
    return open_ids


def later_review_scope(effects, payload, facts, focus, tracker, ticket):
    """Why a re-review's change request is refused, or None (#1048). On a re-review (the focus has an agent review at
    an earlier head), each blocking finding must continue an open earlier finding (its id), sit on a file whose PR
    patch changed since the last reviewed head (within the fix's hunks when the line is known), or say why the earlier round could not see it (`unseen`). Anything
    else belonged in the earlier round: report it as minor. Skipped when the patches cannot be compared."""
    if not any(e["kind"] == "review" and str(e["value"]).startswith("request-changes") for e in effects):
        return None
    focus = focus or payload.get("focus")
    head = facts.get("head")
    blocking = [f for f in payload.get("findings") or [] if isinstance(f, dict) and f.get("severity") == "blocking"]
    earlier = [r for r in facts.get("reviews") or [] if r.get("agent") and (r.get("focus") or "review") == (focus or "review")
               and r.get("commit_id") and r.get("commit_id") != head]
    if not blocking or not earlier or not head or not hasattr(tracker, "changed_since"):
        return None
    changed = tracker.changed_since(ticket, earlier[-1]["commit_id"])
    if changed is None:
        return None
    open_ids = _open_blocking(facts.get("reviews") or [], focus)
    def touched(location):  # `path[:line]` within a changed hunk (3 lines of margin), or anywhere in a changed file
        path, _, line = str(location or "").partition(":")
        if path not in changed:
            return False
        hunks, line = changed[path] if isinstance(changed, dict) else None, line.split("-")[0].split(":")[0]
        if hunks is None or not line.isdigit():
            return True
        return any(a - 3 <= int(line) <= b + 3 for a, b in hunks)
    out = [f for f in blocking if str(f.get("id")) not in open_ids and not f.get("unseen") and not touched(f.get("file"))]
    if not out:
        return None
    named = ", ".join(f"{f.get('id')} ({str(f.get('file') or '').split(':')[0]})" for f in out)
    return (f"re-review scope: {named}: new blocking findings on code the fix did not change since "
            f"{earlier[-1]['commit_id'][:12]}. The earlier round should have raised them. Report each as `--minor` "
            f"(a follow-up), keep the id of the open earlier finding it continues, or append "
            f"`| unseen: <why the earlier round could not see it>`.")


def status(facts, limit=2000, cut=160):
    """The short computed `status` of `factory begin`: stage, PR, CI, fix rounds and, per review focus, the latest
    review with its open blocking findings (a `B<n>` finding stays open until a later review of the focus says it is resolved)."""
    failure = facts.get("ci_failure")
    failure = failure if isinstance(failure, dict) else {}
    ci = {"state": facts.get("ci"), **({"failing": [failure["job"]]} if failure.get("job") else {})}
    focuses = {}
    for r in facts.get("reviews") or []:
        if not r.get("agent"):
            continue
        cur = focuses.setdefault(r.get("focus") or "review", {"open": {}})
        for i in _resolved_ids(r.get("body") or ""):
            cur["open"].pop(i, None)
        if r.get("verdict") == "approve":
            cur["open"].clear()
        for m in re.finditer(r"^\*\*(B\d+)\*\*\s*(.*)$", r.get("body") or "", re.M):
            cur["open"][m[1]] = m[2]
        cur.update(round=r.get("round"), verdict=r.get("verdict"), head=r.get("commit_id"))
    reviews = {f: {**{k: v for k, v in c.items() if k != "open"},
                   "open": [{"id": i, "text": t if len(t) <= cut else t[:cut - 1] + "…"} for i, t in c["open"].items()]}
               for f, c in focuses.items()}
    out = {"stage": facts.get("stage"), "pr": facts.get("pr"), "head": facts.get("head"), "ci": ci,
           "fix_rounds": facts.get("fix_rounds"), "fix_cap": facts.get("fix_cap"), "reviews": reviews}
    while len(json.dumps(out)) > limit and cut > 20:  # keep the whole status under `limit`: cut finding lines further
        cut //= 2
        for c in reviews.values():
            for f in c["open"]:
                f["text"] = f["text"][:cut]
    while len(json.dumps(out)) > limit:  # still too long (many findings): drop the last open findings and count them
        c = max(reviews.values(), key=lambda c: len(c["open"]))
        if not c["open"]:
            break
        c["open"].pop()
        c["omitted"] = c.get("omitted", 0) + 1
    return out


def guard_holds(guard, facts, tracker=None, ticket=None):
    """True when the ticket holds an outcome's `if` guard (ADR-0021): the same facts as `when`."""
    if "children" in guard and "children_open" not in facts and tracker is not None and hasattr(tracker, "children"):
        kids = tracker.children(ticket) or []
        facts = {**facts, "children_open": sum(1 for c in kids if c.get("state") == "open")}
    unknown = set(guard) - GUARD_KEYS
    if not unknown:
        return holds(guard, facts, {})
    if unknown - {k for k in unknown if plugins.owner(k, "guard")}:
        raise Bad(f"unknown guard: {', '.join(sorted(k for k in unknown if not plugins.owner(k, 'guard')))}")
    registry = plugin_registry()  # read the config only for a guard a plugin owns
    off = sorted(k for k in unknown if not registry.is_on(plugins.owner(k, "guard")))
    if off:
        raise Bad("; ".join(registry.refusal("guard", k, "guard") for k in off))
    for k in sorted(unknown):
        if not plugins.load(registry.guards()[k])(guard[k], facts, tracker, ticket):
            return False
    return holds({k: v for k, v in guard.items() if k not in unknown}, facts, {})


def plugin_registry():
    """The registry of the plugins the team turns on (`plugins:`, ADR-0026)."""
    return plugins.registry(config_mod.load())


GUARD_KEYS = {"stage", "label", "pr", "ci", "review", "blockers", "children", "ticket"}  # and the plugins' guards
FROM = re.compile(r"<!-- factory:from (\S+?)( reply)? -->")


def fix_tier(facts, default="mid"):
    """The tier of the ticket's `complexity:*` label (`default` without one)."""
    for label in facts.get("attrs", []):
        if label.startswith("complexity:"):
            return COMPLEXITY_TIER.get(label[11:], default)
    return default


def branch_of(facts, config):
    """The ticket's branch: the PR's head, else `branch_pattern` from the issue title."""
    if facts.get("branch"):
        return facts["branch"]
    body = facts.get("body") or ""
    explicit = re.search(r"^slug:[ \t]*([a-z0-9][a-z0-9-]*)[ \t]*$", body, re.M)
    title = facts.get("title") or ""
    words = [word for word in re.findall(r"[a-z0-9]+", title.lower()) if word not in {"a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "with", "from", "by"}]
    slug = explicit.group(1) if explicit else "-".join(words[:4]) or "ticket"
    return config["branch_pattern"].format(ticket=number_of(facts["ticket"]), slug=slug)


def matches(node, facts, event, outcome=None):
    """None when the node matches, else the reason it does not."""
    if event.get("head") is not None and event["head"] != facts["head"]:
        return "stale head"
    if event.get("heads") is not None and event["heads"] != facts.get("heads"):
        return "stale head"  # a ticket with PRs in several repositories: any of them moved
    if event.get("id") is not None and event["id"] in facts["seen"]:
        return "duplicate event"
    if outcome and outcome.get("from") and facts["stage"] not in outcome["from"]:
        return f"stage {facts['stage']} is not in the outcome's from"
    return None if holds(node["when"], facts, event) else "condition does not hold"


def outside_node_repository(ticket, tracker_repo):
    """The reason a run of an automation copy (`FACTORY_NODE_REPO`, ADR-0024) must not act on `ticket`, else None:
    the ticket is in another repository (a bare number is the tracker's). A run without it acts on any ticket."""
    node_repo = os.environ.get("FACTORY_NODE_REPO")
    repo = split_ticket(ticket, tracker_repo)[0]
    if node_repo and repo and (isinstance(ticket, int) or REF.fullmatch(str(ticket))) and repo.lower() != node_repo.lower():
        return f"ticket {ticket} is in {repo}, not in {node_repo}"
    return None


def draft_pr_commands(node, ticket, branch):
    """{outcome: command} for an `as-instructed` node, which opens its PR itself (#1261): per outcome that labels
    the PR, the `gh pr create --draft` that opens the branch's PR with those labels, so the factory review starts
    first and the project's own review waits for `ready-for-review`."""
    if node.get("output") != "as-instructed" or not branch:
        return {}
    out = {}
    for o, spec in (node.get("outcomes") or {}).items():
        labels = [e["label"] for e in (spec or {}).get("effects") or [] if "label" in e and e.get("on") == "pr"]
        if labels:
            out[o] = (f"gh pr create --draft --head {branch} " + " ".join(f"--label {x}" for x in labels)
                      + f' --title "#{ticket} <subject>" --body-file .factory/pr-body.md')
    return out


def check(name, event, tracker, nodes, config, code_host=None):
    node = load_node(name, nodes)
    if "id" not in event:
        raise Bad("malformed event: needs `id`")
    if (why := outside_node_repository(event["ticket"], getattr(tracker, "repo", None))):
        return NO_GO, {"go": False, "reason": why}
    facts = tracker.facts(event["ticket"])
    if isinstance(code_host, scopes.ScopedTracker):  # the vcs of the ticket's environment
        code_host = code_host.vcs_for()[1]
    if code_host is not tracker and getattr(tracker, "vcs", None) is not code_host:
        facts = overlay(facts, code_host, event["ticket"])  # a vcs that owns its merge requests (space): its facts win
    if "event_name" in event:
        event["id"] = f"{event['event_name']}:{event['ticket']}:{event['action']}:{facts.get('updated_at', '')}"
        event["head"] = facts.get("head")
        if facts.get("heads"):
            event["heads"] = facts["heads"]
        event["actor"] = facts.get("event_actor")
    why = matches(node, facts, event)
    branch = branch_of({**facts, "ticket": event["ticket"]}, config)
    notes = facts.get("agent_notes", [])
    extra = {}  # what the plugins add to the step (comments: the comment node's input, a reply's `mention`)
    for step_context in plugin_registry().hook("gate.step_context"):
        extra.update(step_context(name, node, facts, tracker, event["ticket"]) or {})
    facts = {k: v for k, v in facts.items() if k not in ("body", "agent_notes")}
    role, roles = node["role"], config["roles"]
    tiers = node.get("tiers") or [node.get("tier") or roles.get(role, {}).get("tier")]
    if node.get("focuses"):  # one review tier per focus (roles.<role>.modes.<focus>.tier)
        tiers = [roles[role]["modes"][f]["tier"] for f in node["focuses"]]
    skills = config_mod.resolve(f"roles/{role}/skills.txt")
    fixing = definition_team() and node_mode(name) in ("fix", "update")
    return (NO_GO if why else GO), {
        "go": why is None, "reason": why, "ticket": event["ticket"], "node": name, "role": role,
        "tier": event.get("tier") if event.get("tier") in tiers else tiers[0],
        "focus": event.get("focus"),
        "role_file": config_mod.shown(f"roles/{role}/role.md") if role in roles else None,
        "skills": skills.read_text().split() if skills.exists() else [],
        "context": facts, "status": status(facts), "outcomes": list(node["outcomes"]),
        "agent_notes": [n for n in notes if n["for"] in ("any", *NOTE_AUDIENCE.get(role, ()), *(("fixer",) if fixing else ()))],
        **extra,
        "merge_main": bool(node["role"] == "developer" and facts.get("conflict") and fixing),
        "branch": branch, "handoff_path": f".factory/handoff/{name}.json",
        # an `as-instructed` node with no PR yet opens it as a draft (#1261), the body written with the `pr` skill
        **({"pr_create": cmds} if not facts.get("pr") and (cmds := draft_pr_commands(node, event["ticket"], branch)) else {}),
        # one entry per repository of a ticket that changes several (ADR-0017), all on the ticket's one branch
        "checkouts": [{"repo": p["repo"], "branch": p["branch"] or branch} for p in facts.get("prs") or []],
        **{k: node[k] for k in AGENT_KEYS if k in node},  # who the role is and whom it may address (ADR-0020)
    }


def check_routine(name, nodes, config):
    """A routine (a scheduled node outside any ticket, ADR-0015): no tracker facts, so it always matches."""
    node = load_node(name, nodes)
    role, roles = node["role"], config["roles"]
    skills = config_mod.resolve(f"roles/{role}/skills.txt")
    return GO, {
        "go": True, "reason": None, "ticket": None, "node": name, "role": role,
        "tier": node.get("tier") or roles.get(role, {}).get("tier"), "focus": None,
        "role_file": config_mod.shown(f"roles/{role}/role.md") if role in roles else None,
        "skills": skills.read_text().split() if skills.exists() else [],
        "context": {}, "outcomes": list(node["outcomes"]), "merge_main": False, "branch": None,
        "handoff_path": f".factory/handoff/{name}.json", **{k: node[k] for k in AGENT_KEYS if k in node},
    }


def apply_routine(name, outcome, nodes):
    """A routine writes through its role (a branch, a PR): the gate only checks the outcome and changes nothing."""
    outcomes = load_node(name, nodes)["outcomes"]
    if outcome == "done" and "done" not in outcomes:  # the role handoff's `status: done` means the routine's one outcome
        outcome = ""
    if not outcome and len(outcomes) == 1:
        outcome = next(iter(outcomes))
    if outcome not in outcomes:
        raise Bad(f"unknown outcome: {outcome}")
    return GO, {"applied": True, "written": []}


def payload_logins(value, name):
    """The logins an agent wrote in payload field `name`: a list, or one string of logins split at commas or spaces
    (`factory end --field reviewers=a,b`). Bad when there is none."""
    if isinstance(value, str):
        value = re.split(r"[,\s]+", value.strip())
    if not (isinstance(value, list) and value and all(isinstance(v, str) and v.strip() for v in value)):
        raise Bad(f"payload: field `{name}` must be a non-empty login or list of logins")
    return [v.strip() for v in value]


def resolve(effect, payload, ticket):
    """(kind, value) of one effect; a missing payload field raises Bad naming it."""
    (kind, arg), = [(k, v) for k, v in effect.items() if k != "on"]
    on = effect.get("on")

    def field(f):
        if f not in payload:
            raise Bad(f"payload: missing field `{f}`")
        return payload[f]
    if kind == "set-field":
        return {"kind": kind, "ticket": ticket, "value": {arg: field(arg)}}
    if kind in ("label", "unlabel"):
        out = {"kind": kind, "ticket": ticket, "value": re.sub(r"<([^>]+)>", lambda m: str(field(m[1])), arg)}
        return {**out, "on": on} if on else out
    if kind in ("assign", "unassign", "request-reviewers"):
        if kind == "assign" and arg == "owner":
            arg = config_mod.owner()
        elif kind in ("assign", "request-reviewers") and isinstance(arg, dict):  # `{field: assignee}`: the agent's logins
            arg = payload_logins(field(arg["field"]), arg["field"])
            arg = arg[0] if kind == "assign" and len(arg) == 1 else arg  # one assignee stays a login
        out = {"kind": kind, "ticket": ticket, "value": arg}
        return {**out, "on": on} if on else out
    if kind in ("comment", "create-issues"):
        out = {"kind": kind, "ticket": ticket, "value": field(arg)}
        return {**out, "on": on} if on else out
    if kind in ("park-behind-main-red", "route", "reply"):
        return {"kind": kind, "ticket": ticket, "value": field(arg)}
    if plugins.owner(kind, "effect"):  # a plugin's effect (notify): its handler reads the payload (ADR-0026)
        out = {"kind": kind, "ticket": ticket, "value": arg}
        return {**out, "on": on} if on else out
    return {"kind": kind, "ticket": ticket, "value": arg}  # stage, review, merge, join


def apply(name, outcome, payload, tracker, nodes, focus=None, tier=None):
    node = load_node(name, nodes)
    if outcome not in node["outcomes"]:
        raise Bad(f"unknown outcome: {outcome}" + (" (a reply cannot ask)" if outcome == "ask" and name.startswith("reply-") else ""))
    if outcome == "done" and any(c.get("result") == "fail" for c in payload.get("checks", [])):
        raise Bad("done with a failed check is refused")
    out = node["outcomes"][outcome]
    if out.get("enabled") is False:
        raise Bad(f"disabled outcome: {outcome}")
    ticket = payload["ticket"]
    payload = {**{k: v for k, v in (("focus", focus), ("tier", tier)) if v}, **payload}  # the automation's own
    tiers = node.get("tiers") or []
    if tiers and payload.get("tier") in tiers and "next tier" not in payload:  # the successor comes from the trusted tier list
        i = tiers.index(payload["tier"]) + 1
        if i < len(tiers):
            payload = {**payload, "next tier": tiers[i]}
        elif outcome == "escalate" and "unresolvable" in node["outcomes"]:  # no higher tier: a person decides
            outcome = "unresolvable"
            payload = {**payload, "reason": payload.get("reason") or effects_mod.tagged(f"Blocked: the {payload['tier']} tier could not resolve it and no higher tier exists. A person decides.")}
    out = node["outcomes"][outcome]
    if out.get("if") and not guard_holds(out["if"], tracker.facts(ticket), tracker, ticket):
        if out.get("else") not in node["outcomes"]:
            raise Bad(f"outcome {outcome}: its guard does not hold")
        out = node["outcomes"][out["else"]]  # ADR-0021: e.g. automatic merge refused for a high-risk ticket
    if out.get("ci") and tracker.facts(ticket)["ci"] not in out["ci"] and out.get("else") in node["outcomes"]:
        out = node["outcomes"][out["else"]]  # e.g. all reviews approve but CI is red: a fix round
        fix_tiers = ((nodes.get("nodes") or {}).get("fix") or {}).get("tiers") or []
        tier = node.get("else_tier") or (fix_tiers[0] if fix_tiers else None)
        if "tier" not in payload and tier:  # the fix label needs the cheapest fix tier; a run loads only its own
            payload = {**payload, "tier": tier}  # node (#1288), and a definition team's fix label names no tier
    if node.get("output") == "as-instructed" and not tracker.facts(ticket).get("pr") and \
            any("label" in e and e.get("on") == "pr" for e in out.get("effects") or []):
        cmd = draft_pr_commands({**node, "outcomes": {outcome: out}}, ticket, branch_of({**tracker.facts(ticket), "ticket": ticket},
                                                                                      config_mod.load())).get(outcome)
        raise Bad(f"outcome {outcome}: no PR for the ticket's branch: open it as a draft first" + (f": `{cmd}`" if cmd else ""))
    if any("set-field" in e and e["set-field"] == "risk" for e in out.get("effects") or []):
        payload = {"risk": tracker.facts(ticket)["risk"], **payload}  # the gate's fact: the ticket's risk label
    effects = [resolve(e, payload, ticket) for e in out.get("effects") or []]  # validates the payload first
    # ADR-0026: the effects, `on:` targets and outcome a plugin owns; the config is read only when there are some
    owns = plugins.owner(outcome, "outcome")
    used = {(outcome, owns)} | {(e["kind"], plugins.owner(e["kind"], "effect")) for e in effects
                                if plugins.owner(e.get("on"), "trigger") is None}  # `on: pr` too, not `on: mention`
    used |= {(f"`on: {e['on']}`", plugins.owner(e["on"], "trigger")) for e in effects if isinstance(e.get("on"), str)}
    used = sorted((k, p) for k, p in used if p)
    registry = plugin_registry() if used else plugins.registry({})
    if off := [(k, p) for k, p in used if not registry.is_on(p)]:
        raise Bad("; ".join(plugins.refusal(f"nodes.{name}.{outcome}", k, p) for k, p in off))
    for hook in registry.hook("gate.outcome", owns) if owns else []:  # comments: the `ask` outcome's comment
        effects = hook(name, outcome, node, out, payload, tracker, ticket, effects)
    if payload.get("agent_notes"):  # hidden notes for later agents ride on the review, the comments or the PR text
        try:
            notes = clean_notes(payload["agent_notes"])
        except ValueError as err:
            raise Bad(str(err))
        for e in effects:
            if e["kind"] in ("review", "comment"):
                e["notes"] = notes
    facts = tracker.facts(ticket)
    if scope := later_review_scope(effects, payload, facts, focus, tracker, ticket):
        raise Bad(scope)
    comment_after = []  # what a plugin's effects must write after the stage change (the comment node's reply)
    # A plugin's effects, by handler: it turns them into core effects (comments: route, reply, skip) or reads their
    # payload before anything is written (notify on the ticket or its PR)
    for spec, mine in effects_mod.plugin_handlers(registry, effects).items():
        moved, later = plugins.load(spec)(mine, name, payload, facts, tracker, nodes, ticket)
        effects = [e for e in effects if all(e is not m for m in mine)] + moved
        comment_after += later
    after = []  # effects that must follow the stage change
    starts = [e for e in effects if e["kind"] == "label" and str(e["value"]).startswith(commands())]
    stop = starts and stop_reason(facts, fix=any(str(e["value"]).startswith(vocab.prefix("fix-<tier>")) for e in starts))
    if stop:
        # The transition that would start a developer run it may not start blocks the ticket instead; a check
        # that refused later would stop it with no comment and no event to wake it.
        effects = [{"kind": "comment", "ticket": ticket, "value": stop},
                   {"kind": "stage", "ticket": ticket, "value": "blocked"}]
        # The route did not happen: no success reply and no cursor move, so a person's comment stays unread.
        comment_after = [e for e in comment_after if e["kind"] == "unlabel"]
    if any(e["kind"] == "stage" and e["value"] == "blocked" for e in effects):
        effects = clear_commands(ticket) + effects
    stage_effect = next((e for e in effects if e["kind"] == "stage"), None)
    if stage_effect and not after:  # the join reads the new stage (nothing-to-fix: in-review)
        after = [{**e, "tier": payload.get("tier") or fix_tier(facts)} for e in effects if e["kind"] == "join"]
        effects = [e for e in effects if e["kind"] != "join"]
    if stage_effect:
        target = stage_effect["value"]
        human_effects = []
        human_state = target in ("blocked", "needs-answers")
        if not human_state and vocab.needs_human() in facts.get("attrs", []):
            human_effects += [{"kind": "unlabel", "ticket": ticket, "value": vocab.needs_human()},
                              {"kind": "unassign", "ticket": ticket, "value": None}]
        if human_state:
            human_effects += [{"kind": "label", "ticket": ticket, "value": vocab.needs_human()},
                              {"kind": "label", "ticket": ticket, "value": vocab.needs_human(), "on": "pr"}]
        elif vocab.needs_human() in facts.get("attrs", []) or target in ("ready", "triage", "approved", "done"):
            human_effects += [{"kind": "unlabel", "ticket": ticket, "value": vocab.needs_human(), "on": "pr"},
                              {"kind": "unassign", "ticket": ticket, "value": None, "on": "pr"}]
        effects[:0] = human_effects
    after = [*after, *comment_after, *unblocked(effects, tracker, facts)]
    effects[:0] = group_unlabels(effects, facts)
    effects = effects_mod.order(tracker, effects)  # remote writes first, the stage change last (#998)
    for e in effects:
        if e["kind"] == "stage":
            e["event"] = payload.get("event")
        if e["kind"] == "review":
            # ADR-0021: a definition team has no focuses; its review is named after the node that wrote it
            e["focus"] = payload.get("focus") or name
            e["round"] = payload.get("round")
            e["findings"], e["summary"], e["run_id"] = payload.get("findings"), payload.get("summary"), payload.get("run_id")
            e["outcome"] = outcome  # the marker keeps it: a replay of a split approval needs the one that was reported
        if e["kind"] == "join":
            e["tier"] = payload.get("tier")
        if e["kind"] in ("merge", "update-branch"):
            e["head"] = facts.get("head")  # the head the gates judged
            if facts.get("heads"):
                e["heads"] = facts["heads"]  # one per repository of a multi-repository ticket
        if e["kind"] == "comment" and payload.get("run_id"):
            e["run_id"] = payload["run_id"]
        if spec := effects_mod.plugin_target(registry, e):  # a plugin's target
            plugins.load(spec)(e, facts, tracker, ticket)  # #590 `on: mention`: answer where, and whom, it was asked
        if e["kind"] in ("comment", "review") and definition_team():
            effects_mod.mark(e, comment_marker(name))
    if not any(e["kind"] == "stage" for e in effects) and payload.get("event") is not None:
        effects.append({"kind": "seen", "ticket": ticket, "value": payload["event"]})
    replay = (payload.get("review_replay") and facts.get("stage") == "in-review"
              and stale_review_plan(facts, nodes) == (name, outcome, {"focus": payload.get("focus"),
                                                               "round": payload.get("round"),
                                                               "review_replay": True}))
    supports = getattr(tracker, "supports", lambda e: effects_mod.spec(tracker, e) is not None)
    # `notify` effects are not written as such: gate.send replaces each with a comment, so check that comment
    unsupported = next((e["kind"] for e in effects + after
                        if not supports({**e, "kind": "comment"} if e["kind"] == "notify" else e)), None)
    why = (None if replay else matches(node, facts, {**payload, "id": payload.get("event")}, out)) \
        or (f"unknown effect: {unsupported}" if unsupported else None) or merge_refused(effects, facts, tracker)
    if why:
        return NO_GO, {"applied": False, "reason": why}
    after = [*after, *lesson_effects(ticket, name, payload)]  # after the outcome's effects: never its notification text
    failures, posted = [], []
    for send in registry.hook("gate.send"):  # notify: the sends, each effect replaced by its `[factory]` comment
        effects, failed, sent = send(effects, tracker, ticket, name)
        failures, posted = failures + failed, posted + sent
    if failures:  # keep the markers of the sends that went out; skip the stage change and the other effects
        for e in posted:
            tracker.apply(e)
        raise RuntimeError("notify failed: " + "; ".join(failures))
    written, skipped = [], []  # skipped: what an effect left out and why (request-reviewers: the PR author)
    for e in effects + after:
        if payload.get("review_replay") and e["kind"] == "review":
            continue  # the marker proves this review already landed
        try:
            if tracker.apply(e):
                written.append(e["kind"])
            skipped += [f"{e['kind']}: {x}" for x in e.get("skipped") or []]
        except Exception as error:
            if written:
                report = {"kind": "comment", "ticket": ticket, "on": "pr",
                          "value": effects_mod.tagged(f"Outcome stopped after these effects landed: {', '.join(written)}."),
                          "run_id": payload.get("run_id") or "gate:partial-outcome"}
                try:
                    tracker.apply(report)
                except Exception as report_error:
                    raise RuntimeError(f"{error}; partial outcome report also failed: {report_error}") from error
            raise
    parent = parent_decision(name, ticket, effects, tracker, nodes)
    return GO, {"applied": True, "written": written, **({"skipped": skipped} if skipped else {}),
                **({"parent": parent} if parent else {})}


def review_outcome(node, review):
    """The outcome of a definition node whose review effect wrote `review`: the one its marker names, else the only
    outcome with a matching review effect. None when it cannot be told."""
    want = "approve" if review["verdict"] == "approve" else "request-changes"
    writes = [o for o, spec in (node.get("outcomes") or {}).items()
              if any(str(e.get("review", "")).startswith(want) for e in (spec or {}).get("effects") or [])]
    named = review.get("outcome")
    return named if named in writes else writes[0] if len(writes) == 1 and not named else None


def stale_review_plan(facts, nodes=None):
    """The review outcome whose marker landed at this head but whose command label remains. A definition team's
    review is named after its node (ADR-0021) and waits on that node's trigger labels; `nodes` is its workflow."""
    labels = set(facts.get("pr_labels") or [])
    for review in reversed(facts.get("reviews") or []):
        focus, verdict = review.get("focus"), review.get("verdict")
        if not (review.get("agent") and review.get("commit_id") == facts.get("head")
                and focus and verdict in ("approve", "changes")):
            continue
        plan = {"focus": focus, "round": review.get("round"), "review_replay": True}
        if not definition_team():
            if vocab.cmd("review-<focus>", focus) in labels:
                return "review", verdict, plan
            continue
        import definition
        node = ((nodes or definition.load(config_mod.team_root())["workflow"]).get("nodes") or {}).get(focus)
        if node and (outcome := review_outcome(node, review)) and any(
                isinstance(t, dict) and t.get("label") in labels for t in node.get("triggers") or []):
            return focus, outcome, plan
    return None


def parent_decision(name, ticket, effects, tracker, nodes):
    """ADR-0021, automatic: when this outcome moves a ticket to a final stage, the same node decides on its parent
    (a split ticket's `children` guard), since nothing else wakes the parent when its last child closes."""
    if not definition_team() or not hasattr(tracker, "parent"):
        return None
    final = {s for s, k in (config_mod.load()["tracker"].get("stage_kinds") or {}).items() if k == "final"}
    if next((e["value"] for e in effects if e["kind"] == "stage"), None) not in final:
        return None
    p = tracker.parent(ticket)
    if not p or tracker.facts(p)["stage"] in final:
        return None
    if not any("children" in (o.get("if") or {}) for o in load_node(name, nodes)["outcomes"].values()):
        return None
    return {"ticket": p, **decide(name, p, tracker, nodes)[1]}


SWEEP_STAGES = ("ready", "approved", "split", "in-review", "needs-answers")


def commands():
    """Label prefixes of the developer commands: each starts a developer run."""
    declared = config_mod.load()["tracker"].get("commands") or {}  # a definition team has none of these (ADR-0021)
    return tuple(vocab.prefix(c) for c in ("implement-<tier>", "fix-<tier>", "update-<tier>") if c in declared)


def clear_commands(ticket):
    """Effects that remove every command label. A stopped ticket keeps none, so the first command after a
    person's factory:ready is a new label and fires its node (re-adding a present label fires nothing)."""
    declared = config_mod.load()["tracker"].get("commands") or {}
    if "implement-<tier>" not in declared:  # a definition team (ADR-0021): its routing labels on ticket and PR
        return [{"kind": "unlabel", "ticket": ticket, "value": next(iter(store.values())),
                 **({"on": "pr"} if "pr_label" in store else {})}
                for name, store in declared.items() if ":" not in name]
    return [{"kind": "unlabel", "ticket": ticket, "value": vocab.cmd("implement-<tier>", "*")},
            {"kind": "unlabel", "ticket": ticket, "value": vocab.cmd("fix-<tier>", "*"), "on": "pr"},
            {"kind": "unlabel", "ticket": ticket, "value": vocab.cmd("update-<tier>", "*"), "on": "pr"}]


def stop_reason(facts, fix=False):
    """Why no further developer run may start for this ticket, else None: the spend guard, and for a fix
    round the fix-round cap (`limits.fix_rounds`)."""
    if facts.get("agent_runs", 0) >= facts.get("run_cap", 8):
        return effects_mod.tagged(f"Stopped: {facts['agent_runs']} developer runs since the last {vocab.stage('ready')} reached "
                                  f"limits.agent_runs_per_ticket. A person decides, then sets {vocab.stage('ready')}.")
    if fix and facts.get("fix_rounds", 0) >= facts.get("fix_cap", 3):
        return effects_mod.tagged(f"Stopped: {facts['fix_rounds']} fix rounds reached limits.fix_rounds and the review "
                                  f"still requests changes. A person decides, then sets {vocab.stage('ready')}.")
    return None


def join_decision(facts):
    """The join's outcome and payload, or None while the round is open. Uses the adapter's `round_verdict`,
    which counts every required agent focus, human change requests and CI. A fix label takes the ticket's
    complexity tier (mid when unlabelled)."""
    if facts.get("handed_to_fix") or not facts.get("round_complete") or facts.get("round_verdict") is None:
        return None  # an open round, or a head whose round already went to a fix (its push comes next)
    return ("changes" if facts["round_verdict"] == "changes" else "approved"), {"tier": fix_tier(facts)}


RISK_RANK = {"low": 0, "high": 1}


def merge_decision(facts, auto_merge):
    """(outcome, payload) of the `merge` node, else None while a gate fails: the current head approved by every required
    focus, green CI at it, no conflict. Then `waiting` (auto-merge off or risk above `max_risk`), `updated` (behind) or `merged`."""
    if auto_merge is None or facts.get("stage") != "approved" or facts.get("pr_state") != "open" or facts.get("conflict") \
            or facts.get("ci") != "green" or not facts.get("round_complete") or facts.get("round_verdict") != "approve" \
            or facts.get("round_head") is None or facts["round_head"] != facts.get("head"):
        return None
    if not auto_merge.get("enabled") or RISK_RANK.get(facts.get("risk", "high"), 1) > RISK_RANK.get(auto_merge.get("max_risk"), -1):
        next_step = f"Merge PR #{facts['pr']}" if facts.get("pr") else "waiting for a person to merge"
        return "waiting", {"reason": effects_mod.tagged(f"{next_step}: auto-merge is off or this PR exceeds its risk limit.")}
    return ("updated" if facts.get("behind") else "merged"), {}


AFTER_MERGE_CHECK = re.compile(r"(?im)^\s*[-*] \[ \] Operator check after the merge \(not part of the PR\):\s*(.+?)\s*$")
AFTER_MERGE_MARKER = "<!-- factory:after-merge-checks -->"


def after_merge_checks(body):
    return AFTER_MERGE_CHECK.findall(body or "")


def after_merge_comment(comments):
    return next((c.get("body", "") for c in comments if AFTER_MERGE_MARKER in c.get("body", "")), "")


def after_merge_complete(comments):
    checks = re.findall(r"(?m)^- \[([ xX])\] .+$", after_merge_comment(comments))
    return bool(checks) and all(x.lower() == "x" for x in checks)


def blockers_mode(nodes):
    """The workflow's `blockers` key: `start` (default) waits for every blocker before the work starts, `merge`
    starts the work at once and waits at the merge (a node's own `when.blockers: closed` still applies)."""
    mode = (nodes or {}).get("blockers", "start")
    if mode not in ("start", "merge"):
        raise Bad(f"bad blockers mode {mode!r}: allowed start, merge")
    return mode


def sweep(tracker, nodes, dry_run=False, closed_ticket=None, auto_merge=None, default_tier="mid", progress=None,
          issue=None):
    """Move every listed ticket through each step its facts allow (a merged PR goes approved to done in one
    step). A ticket that reaches done or blocked also lets its split parent finish. Returns (results, failed)."""
    results, failed, done_tickets = [], False, []
    stages = config_mod.load()["tracker"]["stages"]
    mode = blockers_mode(nodes)
    start_waits = ((nodes.get("nodes", {}).get("start") or {}).get("when") or {}).get("blockers") == "closed"
    queue, seen, merged = ([issue] if issue is not None else list(tracker.sweep_tickets(stages))), set(), False
    prefetch(tracker, queue)
    while queue:
        n = queue.pop(0)
        for _ in range(4):  # a ticket's longest walk: in-review, approved, done
            try:
                facts = tracker.facts(n)
                plan = definition.plan(nodes, facts, auto_merge, default_tier, mode, start_waits)
                if plan is None or (n, facts["stage"]) in seen:
                    if progress:
                        progress({"ticket": n, "stage": facts["stage"], "outcome": "skipped"})
                    break
                if plan[:2] == ("merge", "merged") and merged:
                    if progress:
                        progress({"ticket": n, "stage": facts["stage"], "outcome": "skipped",
                                  "why": "one merge at a time"})
                    break  # one merge at a time; the next sweep reads the rest against the new main
                seen.add((n, facts["stage"]))
                node, outcome, payload = plan
                r = {"ticket": n, "stage": facts["stage"], "node": node, "outcome": outcome}
                results.append(r)
                if dry_run:
                    if progress:
                        progress(r)
                    break
                code, out = apply(node, outcome, {"ticket": n, **payload}, tracker, nodes)
                r.update(applied=out["applied"], written=out.get("written", []))
                if progress:
                    progress(r)
                if not out["applied"]:
                    break
                if node == "finish" and outcome == "merged" and hasattr(tracker, "config_change_merged"):
                    try:  # a reminder only: its failure must not stop the parent's step below
                        tracker.config_change_merged(facts.get("pr"))
                    except Exception as e:
                        failed = True
                        results.append({"ticket": n, "error": f"deploy check: {type(e).__name__}: {e}"})
                merged = merged or (node, outcome) == ("merge", "merged")
                if node == "finish" and outcome in ("merged", "children-done"):
                    done_tickets.append(n)
                if node == "finish" and outcome in ("merged", "children-done") and hasattr(tracker, "parent") and (p := tracker.parent(n)):
                    queue.append(p)
            except scopes.OutOfScope:  # no environment's ticket: nothing to do (#1069)
                if progress:
                    progress({"ticket": n, "outcome": "skipped", "why": SKIP.format(ticket=n)})
                break
            except Exception as e:  # one ticket failing does not stop the others
                failed = True
                r = {"ticket": n, "error": f"{type(e).__name__}: {e}", **({"kind": e.kind} if hasattr(e, "kind") else {})}
                results.append(r)
                if progress:
                    progress(r)
                break
    wake_sources = list(dict.fromkeys(([closed_ticket] if closed_ticket is not None else []) + done_tickets))
    woken = set()
    if hasattr(tracker, "blocking"):
        for source in wake_sources:
            try:
                dependents = tracker.blocking(source)
                not_planned = hasattr(tracker, "not_planned") and tracker.not_planned(source)
            except scopes.OutOfScope:  # no environment's ticket wakes nothing (#1069)
                continue
            except Exception as e:  # a tracker lookup must not fail the whole sweep
                results.append({"ticket": source, "error": f"{type(e).__name__}: {e}"})
                continue
            for dependent in dependents:
                n = dependent["number"] if isinstance(dependent, dict) else dependent
                try:
                    if n in woken:
                        if progress:
                            progress({"ticket": n, "outcome": "skipped", "why": "already inspected"})
                        continue
                    facts = tracker.facts(n)
                    if mode == "merge" and facts["stage"] == "approved" and not facts["blockers_open"]:
                        woken.add(n)  # its last blocker closed: the merge it waited on may run now
                        if merged:  # one merge at a time; the next sweep reads the rest against the new main
                            continue
                        sub, sub_failed = sweep(tracker, nodes, dry_run, None, auto_merge, default_tier, progress, issue=n)
                        results += sub
                        merged = merged or any((r.get("node"), r.get("outcome")) == ("merge", "merged") for r in sub)
                        failed = failed or sub_failed
                        continue
                    if facts["stage"] != "ready" or facts["blockers_open"]:
                        if progress:
                            progress({"ticket": n, "stage": facts["stage"], "outcome": "skipped"})
                        continue
                    facts = tracker.stale_facts(n)
                    since = facts.get("stage_at")  # when this ticket last got its stage label, i.e. the re-fire
                    recent = since and datetime.now(timezone.utc) - datetime.fromisoformat(since.replace("Z", "+00:00")) <= timedelta(minutes=5)
                    if facts.get("issue_state", "open") != "open" or recent:  # closed, or this event's re-fire already landed
                        if progress:
                            progress({"ticket": n, "outcome": "skipped", "why": "closed" if facts.get("issue_state", "open") != "open" else "re-fired within 5 minutes"})
                        continue
                    woken.add(n)
                    outcome = "re-triage" if not_planned else "unblocked"
                    results.append({"ticket": n, "stage": "ready", "node": "start", "outcome": outcome})
                    if progress:
                        progress(results[-1])
                    if dry_run:
                        continue
                    effects = ({"kind": "comment", "ticket": n, "run_id": "finish:re-triage",
                                "value": effects_mod.tagged(f"Blocker #{source} was closed as not planned; re-triage this ticket")},
                               {"kind": "stage", "ticket": n, "value": "triage"}) if not_planned else ({"kind": "unlabel", "ticket": n, "value": vocab.stage("ready")},
                                   {"kind": "label", "ticket": n, "value": vocab.stage("ready")},
                                   {"kind": "comment", "ticket": n, "value": effects_mod.tagged(f"unblocked by #{source}"),
                                    "run_id": "finish:unblocked"})
                    for effect in effects:
                        tracker.apply(effect)
                except Exception as e:
                    failed = True
                    r = {"ticket": n, "error": f"{type(e).__name__}: {e}"}
                    results.append(r)
                    if progress:
                        progress(r)
    return results, failed


def prefetch(tracker, numbers):
    """Let a tracker that batches its reads (GitHub: GraphQL) read `numbers` at once; the others read per ticket."""
    if numbers and hasattr(tracker, "prefetch"):
        tracker.prefetch(numbers)


STALE_STAGES = ("triage", "ready", "in-dev", "in-review", "changes-requested", "approved")
RECOVER = "recover:"


def recovered(facts, stage):
    """Re-fires already recorded for `stage` (the counter is the `[factory] recover: stage=<stage> re-fired ...` comment; comment-only notes do not count)."""
    return sum(f"stage={stage} re-fired" in c for c in facts.get("recover_comments", []))


def stale_plan(facts, now, hours, command_minutes=30):
    """(kind, label, on, text) when the ticket's waking event is visibly missing after `hours` in its stage:
    kind `refire` (remove, then add `label` on `on`), `comment` (no label to re-fire), `finish` (the PR merged)
    or `conflict` (an approved PR conflicts: the sweep's update-cheap),
    else None. `since` is the last label or commit event (ISO); an unknown one never makes a ticket stale."""
    stage, since = facts["stage"], facts.get("since")
    if facts.get("issue_state", "open") != "open" or not since:
        return None
    age = timedelta(minutes=command_minutes) if facts.get("command_pending") else timedelta(hours=hours)
    if now - datetime.fromisoformat(since.replace("Z", "+00:00")) <= age:
        return None
    cmd, cmd_at, commit_at = facts.get("command"), facts.get("command_at") or "", facts.get("commit_at") or ""
    if stage == "changes-requested" and facts.get("conflict") and cmd and cmd.startswith(vocab.prefix("update-<tier>")) \
            and cmd_at and cmd_at < since:
        return "refire", cmd, "pr", "conflicting PR has an update label with no later run"
    if stage == "triage":
        return "refire", vocab.stage("triage"), "issue", "no intake outcome"
    if stage == "ready" and not facts.get("blockers_open"):
        return "refire", vocab.stage("ready"), "issue", "no start outcome"
    if stage == "in-dev" and cmd and (not facts.get("pr") or commit_at < cmd_at):
        return "refire", cmd, "issue", "no PR or commit since the command label"
    if stage == "in-review" and facts.get("pr"):
        head = facts.get("head")
        if facts.get("ci_label") == "green" and cmd and cmd.startswith(vocab.prefix("review-<focus>")) \
                and cmd.removeprefix(vocab.prefix("review-<focus>")) in facts.get("focus_missing", []):
            return "refire", cmd, "pr", "no review for this focus at the head"
        if facts.get("ci_label") == "green" and not any(r["agent"] and r["commit_id"] == head for r in facts.get("reviews", [])):
            missing = facts.get("focus_missing", [])
            if missing:
                return "refire", vocab.cmd("review-<focus>", missing[0]), "pr", "no review for this focus at the head"
        if not facts.get("ci_label"):
            if facts.get("conflict"):
                return "comment", None, None, f"no CI label at head {head}: the PR conflicts with main (GitHub starts no CI on a conflict); the update runs after the join"
            return "comment", None, None, f"no CI label at head {head}: CI's job"
    if stage == "changes-requested" and cmd and commit_at < cmd_at:
        return "refire", cmd, "pr", "no commit since the fix label"
    if stage == "approved" and facts.get("pr_state") == "merged":
        return "finish", None, None, "the PR merged"
    if stage == "approved" and facts.get("pr_state") == "open" and facts.get("conflict"):
        return "conflict", None, None, "the approved PR conflicts with main"
    if stage == "ready" and facts.get("blockers_open"):
        return None
    if stage == "changes-requested" and cmd and commit_at >= cmd_at:
        return None
    return "human", None, None, f"no recovery rule re-fired after {hours} hours in {stage}"


def sweep_stale(tracker, nodes, hours, now=None, dry_run=False, command_minutes=30, progress=None):
    """Recover tickets whose Air run died: one re-fire per ticket and stage, a second stale finding blocks it.
    Returns (results, failed)."""
    now, results, failed = now or datetime.now(timezone.utc), [], False
    stale = tracker.sweep_tickets(STALE_STAGES)
    prefetch(tracker, stale)
    for n in stale:
        try:
            facts = tracker.stale_facts(n)
            plan = stale_plan(facts, now, hours, command_minutes)
            if plan is None:
                if progress:
                    progress({"ticket": n, "stage": facts["stage"], "recover": "skipped"})
                continue
            kind, label, on, why = plan
            stage = facts["stage"]
            r = {"ticket": n, "stage": stage, "recover": kind, "why": why}
            results.append(r)
            if dry_run:
                if progress:
                    progress(r)
                continue
            run = "recover:stale"
            if kind == "finish":
                _, out = apply("finish", "merged", {"ticket": n}, tracker, nodes)
                r["applied"] = out["applied"]
            elif kind == "conflict":
                _, out = apply("finish", "conflict", {"ticket": n}, tracker, nodes)
                r["applied"] = out["applied"]
            elif kind == "comment":
                r["applied"] = tracker.apply({"ticket": n, "kind": "comment", "run_id": run,
                                              "value": effects_mod.tagged(f"{RECOVER} stage={stage} {why}")})
            elif kind == "human":
                if vocab.needs_human() in facts.get("attrs", []):
                    if progress:
                        progress({**r, "recover": "skipped"})
                    continue  # preserve an existing human wait and its assignee
                tracker.apply({"ticket": n, "kind": "label", "value": vocab.needs_human()})
                tracker.apply({"ticket": n, "kind": "label", "value": vocab.needs_human(), "on": "pr"})
                text = effects_mod.tagged(f"{RECOVER} stage={stage}: {why}. A person decides, then sets {vocab.stage('ready')}.")
                r["applied"] = tracker.apply({"ticket": n, "kind": "comment", "run_id": run, "value": text})
            elif recovered(facts, stage):
                r["recover"] = "blocked"
                _, out = apply("recover", "blocked", {"ticket": n, "run_id": run,
                                                          "reason": effects_mod.tagged(f"Stopped: the node did not respond twice in {stage}. A person decides, then sets {vocab.stage('ready')}.")},
                               tracker, nodes)
                r["applied"] = out["applied"]
            else:  # the comment last: for `ready` the re-added stage label starts the stage, and the comment must follow it
                tracker.apply({"ticket": n, "kind": "unlabel", "value": label, "on": on})
                r["applied"] = tracker.apply({"ticket": n, "kind": "label", "value": label, "on": on})
                tracker.apply({"ticket": n, "kind": "comment", "run_id": run,
                               "value": effects_mod.tagged(f"{RECOVER} stage={stage} re-fired {label} ({why})")})
            if progress:
                progress(r)
        except Exception as e:  # one ticket failing does not stop the others
            failed = True
            r = {"ticket": n, "error": f"{type(e).__name__}: {e}"}
            results.append(r)
            if progress:
                progress(r)
    return results, failed


def main(argv):
    p = argparse.ArgumentParser(prog="gate.py")
    p.add_argument("cmd", choices=["check", "apply", "sweep", "decide", "ci"])
    p.add_argument("--wait", action="store_true")
    p.add_argument("--node", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--stale", action="store_true")
    p.add_argument("--event", nargs="?", const="-")
    p.add_argument("--issue", type=lambda t: t if REF.fullmatch(t) else int(t))  # N, or owner/repo#N (ADR-0024)
    p.add_argument("--pr", type=int)  # decide: the PR that woke the node; the gate finds its ticket
    p.add_argument("--outcome")
    p.add_argument("--payload")
    p.add_argument("--handoff")
    p.add_argument("--tracker", default="")
    p.add_argument("--nodes")
    p.add_argument("--spec")
    p.add_argument("--focus")  # the automation's focus and tier: the trigger payload carries neither
    p.add_argument("--tier")
    try:
        a = p.parse_args(config_mod.pop_team(argv))
    except SystemExit:
        return BAD
    path = ""
    code_host = None
    try:
        live = default_place(config_mod.load())  # the CLI word of the config's own tracker
        kind, _, path = (a.tracker or live or "").partition(":")
        if kind == live and live and not path:
            tracker, code_host = systems(config_mod.load())
        elif kind == "memory" and path:
            try:
                tracker = MemorySystem(dict(json.loads(Path(path).read_text())))
                code_host = tracker  # the memory tracker also maps a PR number to its ticket
            except (OSError, ValueError, TypeError) as e:
                raise Bad(f"malformed --tracker file: {e}")
        else:
            raise Bad(f"--tracker is {live or 'a tracker'} or memory:FILE")
        nodes = spec_nodes(a.spec) if a.spec else nodes_mod.load(a.nodes)
        config = config_mod.load()
        if a.cmd == "ci":
            if not a.issue:
                raise Bad("ci needs --issue TICKET")
            minutes = (config_mod.load().get("limits") or {}).get("ci_wait_minutes", 30)
            out = ci_state(tracker, a.issue, minutes * 60 if a.wait else 0)
            print(json.dumps(out))
            return GO if out["ci"] == "green" else FAILED if out["ci"] == "red" else NO_GO
        if a.cmd == "decide":
            ticket = a.issue or (pr_ticket(code_host, a.pr) if a.pr and code_host else None)
            if not (a.node and ticket):
                raise Bad("decide needs --node NODE and --issue TICKET or --pr PR")
            status, out = decide(a.node, ticket, tracker, nodes, dry_run=a.dry_run)
            print(json.dumps(out))
            return status
        if a.cmd == "sweep":
            if not hasattr(tracker, "sweep_tickets"):
                raise Bad("sweep needs --tracker github")
            issue, closed_ticket = a.issue, a.issue
            if a.event and issue is None:
                try:
                    raw = json.loads(sys.stdin.read() if a.event == "-" else Path(a.event).read_text())
                    if not isinstance(raw, dict):
                        raise ValueError("expected an event object")
                    event_name = raw.get("eventType") or raw.get("event_name")
                    raw = raw.get("data", raw)
                    if not isinstance(raw, dict) or not (event_name or isinstance(raw.get("action"), str)):
                        raise ValueError("expected an event name and issue or PR number")
                    issue_payload, pr_payload = raw.get("issue"), raw.get("pull_request")
                    is_pr = bool(pr_payload) or str(event_name or "").startswith("pull_request")
                    if not is_pr:
                        issue = (issue_payload or raw).get("number")
                        if raw.get("action") == "closed":
                            closed_ticket = issue
                    else:
                        pr_number = (pr_payload or raw).get("number")
                        ref_ticket = pr_ticket(code_host, pr_number, raw.get("repository")) if code_host else None
                        if ref_ticket is None:
                            raise ValueError("PR head ref does not identify an ez/<ticket> branch")
                        issue = ref_ticket
                    if not issue or (isinstance(issue, int) and issue < 1):
                        raise ValueError("event does not identify a ticket number")
                except (OSError, ValueError, TypeError) as e:
                    raise Bad(f"invalid sweep event: {e}")
            if issue is None and not a.stale:
                raise Bad("sweep needs --issue or --event; only stale recovery may be unscoped")
            progress = lambda item: print(json.dumps(item), file=sys.stderr, flush=True)
            results, failed = ([], False) if a.stale and issue is None else sweep(
                tracker, nodes, a.dry_run, closed_ticket, config["gates"].get("auto_merge"),
                config.get("defaults", {}).get("tier", "mid"), progress, issue)
            out = {"dry_run": a.dry_run, "sweep": results}
            if a.stale:
                out["stale"], stale_failed = sweep_stale(tracker, nodes, config["limits"].get("stale_hours", 2),
                                                         dry_run=a.dry_run,
                                                         command_minutes=config["limits"].get("stale_command_minutes", 30),
                                                         progress=progress)
                failed = failed or stale_failed
            print(json.dumps(out))
            print(json.dumps({"github_calls": getattr(getattr(tracker, "run", None), "calls", 0)}))  # last line: the load on the rate limit
            return FAILED if failed else GO
        if not a.node:
            raise Bad("--node is required")
        routine = bool(((nodes.get("nodes") or {}).get(a.node) or {}).get("routine"))
        if routine and a.cmd == "check":
            code, out = check_routine(a.node, nodes, config)
        elif routine:
            try:
                h = json.loads(Path(a.handoff).read_text()) if a.handoff else {}
            except (OSError, ValueError) as e:
                raise Bad(f"malformed handoff: {e}")
            if not isinstance(h, dict):
                raise Bad("malformed handoff: needs an object")
            code, out = apply_routine(a.node, a.outcome or h.get("outcome") or h.get("status") or "", nodes)
        elif a.cmd == "check":
            event = read(a.event or "", "event", raw=True, code_host=code_host)
            event.update({k: v for k, v in (("focus", a.focus), ("tier", a.tier)) if v})
            code, out = check(a.node, event, tracker, nodes, config, code_host)
        else:
            if a.handoff:
                try:
                    h = json.loads(Path(a.handoff).read_text())
                except (OSError, ValueError) as e:
                    raise Bad(f"malformed handoff: {e}")
                if not isinstance(h, dict):
                    raise Bad("malformed handoff: needs an object")
                ev = read(a.event, "event", raw=True, code_host=code_host) if a.event else {}
                if "ticket" not in {**h, **ev}:
                    raise Bad("apply --handoff needs `ticket` in the handoff or --event")
                payload = {**intake_fields(h), **h, **{k: v for k, v in ev.items() if v is not None and k != "id"},
                           **({"event": ev["id"]} if "id" in ev else {})}
                out_name = a.outcome or outcome_of(h)
            else:
                payload, out_name = read(a.payload or "", "payload"), a.outcome or ""
            code, out = apply(a.node, out_name, payload, tracker, nodes, a.focus, a.tier)
            path and Path(path).write_text(json.dumps(tracker.tickets))
    except scopes.OutOfScope as e:  # #1069: a ticket of no environment is left alone; a check must not go
        reason = SKIP.format(ticket=e.args[0])
        print(json.dumps({"go": False, "reason": reason} if a.cmd == "check" else {"skipped": True, "reason": reason}))
        return NO_GO if a.cmd == "check" else GO
    except NoTicket as e:
        print(json.dumps({"go": False, "reason": str(e)}))
        return NO_GO
    except MissingNumber as e:
        print(json.dumps({"go": False, "why": str(e)}))
        return 11
    except Bad as e:
        print(json.dumps({"error": str(e)}))
        return BAD
    except Exception as e:  # a failing effect: later effects and the stage did not run
        print(json.dumps({"error": f"{type(e).__name__}: {e}"}))
        if path:
            Path(path).write_text(json.dumps(tracker.tickets))
        return FAILED
    print(json.dumps(out))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

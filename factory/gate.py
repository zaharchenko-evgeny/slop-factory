#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The gate: decide whether a node runs (check) and apply an outcome's effects (apply).

Usage: gate.py check --node N --event FILE [--tracker github|memory:FILE]
       gate.py apply --node N --outcome O --payload FILE [--tracker github|memory:FILE]
       gate.py apply --node N --handoff FILE [--event FILE]   (outcome and payload derived from the handoff)
       --spec FILE (check, apply, sweep): the node spec of the automation (deploy plan); no workflows/nodes.yaml is read
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
uses factory/gate_github.py (bot login from `targets.air.bot_login`).
"""
import argparse
from collections import Counter
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as config_mod  # noqa: E402
import nodes as nodes_mod  # noqa: E402
from gate_github import GitHubGateTracker  # noqa: E402
from gate_port import MemoryTracker  # noqa: E402

ROOT = nodes_mod.ROOT
GO, NO_GO, BAD, FAILED = 0, 10, 2, 1


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
            return focus in miss or event.get("rereview") == focus
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
        return bool(facts["due"]) == bool(want)  # due
    return all(one(k, v) for k, v in when.items())


def spec_nodes(path):
    """A `nodes` mapping from a node spec file: the spec's node and the script nodes it `runs`."""
    try:
        spec = json.loads(Path(path).read_text())
        out = {}
        for s in [spec, *(spec.get("runs") or {}).values()]:
            node = {k: s[k] for k in ("role", "when", "outcomes", "else_tier", "tiers") if k in s}
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


def from_trigger(d):
    """{id, ticket, head, actor} of a raw webhook payload (issues, pull_request, pull_request_review)."""
    if not any(isinstance(d.get(key), dict) for key in ("pull_request", "issue", "review")):
        if isinstance(d.get("data"), dict):
            d = d["data"]
    pr, issue, review = d.get("pull_request"), d.get("issue"), d.get("review")
    if not isinstance(pr, dict) and not isinstance(issue, dict):
        raise Bad("malformed event: needs `ticket` or a trigger payload")
    name = "pull_request_review" if review and pr else "pull_request" if pr else "issues"
    src = pr or issue
    if pr:
        m = re.match(r"ez/(\d+)-", (pr.get("head") or {}).get("ref") or "")
        if not m:
            raise NoTicket(f"PR #{pr.get('number')} is not on an ez/<n>- branch")
        ticket = int(m[1])
    else:
        ticket = issue.get("number")
    when = d.get("delivery") or d.get("delivery_id") or (review or {}).get("submitted_at") or src.get("updated_at")
    label = (d.get("label") or {}).get("name", "")
    rereview = label.removeprefix("factory:rereview-") if label.startswith("factory:rereview-") else None
    return {"id": f"{name}:{when}", "ticket": ticket, "head": ((pr or {}).get("head") or {}).get("sha"),
            "actor": (d.get("sender") or {}).get("login"), "rereview": rereview}


def read(path, what, raw=False):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise Bad(f"malformed {what}: {e}")
    if isinstance(data, dict) and raw and "ticket" not in data:
        return from_trigger(data)
    if not isinstance(data, dict) or "ticket" not in data:
        raise Bad(f"malformed {what}: needs an object with `ticket`")
    return data


COMPLEXITY_TIER = {"small": "cheap", "medium": "mid", "large": "strong"}
FIX_TIERS = ("cheap", "mid", "strong")


def slices(split):
    """The proposed slices of a router `split` as {title, body} children: a list of {title, body} objects, else
    lines (a string, or a list of strings) whose text is both title and body."""
    if isinstance(split, list) and all(isinstance(x, dict) for x in split):
        return [{"title": str(x["title"]).strip(), "body": str(x.get("body") or x["title"]).strip()} for x in split]
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
    return {"complexity": "small" if n <= 150 else "medium" if n <= 400 else "large",
            "tier": "cheap" if n <= 150 else "mid" if n <= 400 else "strong",
            "questions": f"[factory] Needs answers before work starts: {h.get('reason', '')}",
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


def fix_tier(facts):
    """The fix tier of the ticket's `complexity:*` label (mid without one)."""
    for label in facts.get("attrs", []):
        if label.startswith("complexity:"):
            return COMPLEXITY_TIER.get(label[11:], "mid")
    return "mid"


def branch_of(facts, config):
    """The ticket's branch: the PR's head, else `branch_pattern` from the issue title."""
    if facts.get("branch"):
        return facts["branch"]
    body = facts.get("body") or ""
    explicit = re.search(r"^slug:[ \t]*([a-z0-9][a-z0-9-]*)[ \t]*$", body, re.M)
    title = facts.get("title") or ""
    words = [word for word in re.findall(r"[a-z0-9]+", title.lower()) if word not in {"a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "with", "from", "by"}]
    slug = explicit.group(1) if explicit else "-".join(words[:4]) or "ticket"
    return config["branch_pattern"].format(ticket=facts["ticket"], slug=slug)


def matches(node, facts, event, outcome=None):
    """None when the node matches, else the reason it does not."""
    if event.get("head") is not None and event["head"] != facts["head"]:
        return "stale head"
    if event.get("id") is not None and event["id"] in facts["seen"]:
        return "duplicate event"
    if outcome and outcome.get("from") and facts["stage"] not in outcome["from"]:
        return f"stage {facts['stage']} is not in the outcome's from"
    return None if holds(node["when"], facts, event) else "condition does not hold"


def check(name, event, tracker, nodes, config):
    node = load_node(name, nodes)
    if "id" not in event:
        raise Bad("malformed event: needs `id`")
    facts = tracker.facts(event["ticket"])
    if name == "fix" and facts.get("ci") == "red" and facts.get("head") and hasattr(tracker, "ci_failure"):
        failure = tracker.ci_failure(facts["head"])
        if failure:
            facts["ci_failure"] = failure
    why = matches(node, facts, event)
    branch = branch_of({**facts, "ticket": event["ticket"]}, config)
    facts = {k: v for k, v in facts.items() if k != "body"}
    role, roles = node["role"], config["roles"]
    tiers = node.get("tiers") or [node.get("tier") or roles.get(role, {}).get("tier")]
    if node.get("focuses"):  # one review tier per focus (roles.<role>.modes.<focus>.tier)
        tiers = [roles[role]["modes"][f]["tier"] for f in node["focuses"]]
    skills = config_mod.resolve(f"roles/{role}/skills.txt")
    return (NO_GO if why else GO), {
        "go": why is None, "reason": why, "ticket": event["ticket"], "node": name, "role": role,
        "tier": event.get("tier") if event.get("tier") in tiers else tiers[0],
        "focus": event.get("focus"),
        "role_file": config_mod.shown(f"roles/{role}/role.md") if role in roles else None,
        "skills": skills.read_text().split() if skills.exists() else [],
        "context": facts, "outcomes": list(node["outcomes"]),
        "merge_main": bool(node["role"] == "developer" and facts.get("conflict") and name in ("fix", "update")),
        "branch": branch, "handoff_path": f".factory/handoff/{name}.json",
    }


def check_state_path(ticket):
    return Path(".factory") / "check" / f"{ticket}.json"


def save_check_state(ticket, head):
    path = check_state_path(ticket)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ticket": ticket, "head": head}))


def checked_head(ticket):
    try:
        state = json.loads(check_state_path(ticket).read_text())
        return state.get("head") if state.get("ticket") == ticket else None
    except (OSError, ValueError, AttributeError):
        return None


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
    if kind in ("comment", "create-issues"):
        out = {"kind": kind, "ticket": ticket, "value": field(arg)}
        return {**out, "on": on} if on else out
    return {"kind": kind, "ticket": ticket, "value": arg}  # stage, review, merge, join


def apply(name, outcome, payload, tracker, nodes, focus=None, tier=None, checked_pr_head=None, auto_merge=None):
    node = load_node(name, nodes)
    if outcome not in node["outcomes"]:
        raise Bad(f"unknown outcome: {outcome}")
    if name == "review":
        if "verdict" in payload and outcome_of({"verdict": payload["verdict"]}) != outcome:
            raise Bad(f"review verdict {payload['verdict']!r} does not match outcome {outcome!r}")
        blocking = any(f.get("severity") == "blocking" for f in payload.get("findings", []))
        if outcome == "approve" and blocking:
            raise Bad("review verdict approve cannot include a blocking finding")
        if outcome == "changes" and not blocking:
            raise Bad("review verdict changes requires a blocking finding")
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
            payload = {**payload, "reason": payload.get("reason") or f"[factory] Blocked: the {payload['tier']} tier could not resolve it and no higher tier exists. A person decides."}
    out = node["outcomes"][outcome]
    if out.get("ci") and tracker.facts(ticket)["ci"] not in out["ci"] and out.get("else") in node["outcomes"]:
        out = node["outcomes"][out["else"]]  # e.g. all reviews approve but CI is red: a fix round
        if "tier" not in payload:  # the agent saw `approved`; the fix label needs the cheapest fix tier
            payload = {**payload, "tier": node.get("else_tier") or nodes["nodes"]["fix"]["tiers"][0]}
    if any("set-field" in e and e["set-field"] == "risk" for e in out.get("effects") or []):
        payload = {"risk": tracker.facts(ticket)["risk"], **payload}  # the gate's fact: the ticket's risk label
    effects = [resolve(e, payload, ticket) for e in out.get("effects") or []]  # validates the payload first
    facts = tracker.facts(ticket)
    if name == "join" and outcome == "changes" and facts.get("handed_to_fix") and has_dispute_rereview(facts):
        effects.insert(0, {"kind": "unlabel", "ticket": ticket, "value": "factory:fix-*", "on": "pr"})
    if name in ("implement", "fix") and outcome == "done":
        estimate = size_estimate(facts.get("body", ""))
        if estimate is not None:
            config = config_mod.load()
            base = next((r["default_branch"] for r in config["repositories"] if r.get("role") == "primary"), "main")
            size, files = branch_size(f"origin/{base}")
            limit = config.get("limits", {}).get("max_diff_lines", 400)
            if size * 2 > estimate * 3 and size > limit:
                largest = ", ".join(f"{path} ({count})" for path, count in files[:5]) or "none"
                reason = (f"[factory] Blocked: branch size is {size} lines against a {estimate}-line estimate "
                          f"(1.5× estimate: {estimate * 1.5:g}; max_diff_lines: {limit}). "
                          f"Largest files: {largest}. Edit the ticket's size line or split it, then set factory:ready.")
                effects = [{"kind": "comment", "ticket": ticket, "value": reason},
                           {"kind": "stage", "ticket": ticket, "value": "blocked"}]
    if name == "fix" and outcome == "done" and facts.get("handed_to_fix") and local_head() == facts.get("head"):
        addressed = payload.get("findings_addressed", [])
        focuses = payload.get("disputed_focuses", [])
        expected = [finding for r in facts.get("reviews", []) if r.get("agent") and r.get("verdict") == "changes"
                    and r.get("commit_id") == facts.get("head") for finding in r.get("findings", [])]
        expected_focuses = {r.get("focus") for r in facts.get("reviews", []) if r.get("agent")
                            and r.get("verdict") == "changes" and r.get("commit_id") == facts.get("head")
                            and r.get("findings")}
        disputed = [m.group(1) for x in addressed if (m := re.match(r"([A-Z]\d+): disputed — .+", str(x)))]
        all_disputed = isinstance(addressed, list) and isinstance(focuses, list) \
            and all(isinstance(f, str) for f in focuses) and bool(expected) and Counter(expected) == Counter(disputed) \
            and set(focuses) == expected_focuses \
            and all(": disputed — " in str(x) for x in addressed)
        if all_disputed and focuses and not any(
                r.get("focus") in focuses and r.get("rereview") for r in facts.get("reviews", [])):
            disputes = "\n".join(str(x) for x in addressed)
            effects = [{"kind": "comment", "ticket": ticket, "value": f"[factory] Disputed findings:\n{disputes}", "on": "pr"}]
            effects += [{"kind": "label", "ticket": ticket, "value": f"factory:rereview-{focus}", "on": "pr"}
                        for focus in focuses]
            effects.append({"kind": "stage", "ticket": ticket, "value": "in-review"})
        else:
        # The platform pushes after the run; an unchanged HEAD means the fix made no commit, and no event
        # would ever start the next round. A person decides.
            effects = [{"kind": "comment", "ticket": ticket, "value": (
                           "[factory] Stopped: the fix round ended without a commit, so nothing starts the next "
                           "review. A person decides, then sets factory:ready.")},
                       {"kind": "stage", "ticket": ticket, "value": "blocked"}]
    if name == "merge":  # the plan may be seconds old
        decision = merge_decision(facts, auto_merge if auto_merge is not None else config_mod.load()["gates"].get("auto_merge"))
        if decision is None or decision[0] != outcome:
            return NO_GO, {"applied": False, "reason": "a merge gate no longer holds"}
    after = []  # effects that must follow the stage change
    if name == "implement" and outcome == "done" and facts.get("pr") and facts.get("ci") in ("green", "red") \
            and checked_pr_head is not None and facts.get("head") == checked_pr_head and local_head() == facts.get("head"):
        # A resumed ticket implemented on its open PR with no new commit: no push, so no CI label would come.
        # Re-fire it, and run the join once the stage is in-review: reviews already at this head decide.
        effects = refire_ci(ticket, facts["ci"]) + effects
        after = [{"kind": "join", "ticket": ticket, "value": "all-approve", "tier": fix_tier(facts)}]
    starts = [e for e in effects if e["kind"] == "label" and str(e["value"]).startswith(COMMANDS)]
    stop = starts and stop_reason(facts, fix=any(str(e["value"]).startswith("factory:fix-") for e in starts))
    if stop:
        # The transition that would start a developer run it may not start blocks the ticket instead; a check
        # that refused later would stop it with no comment and no event to wake it.
        effects = [{"kind": "comment", "ticket": ticket, "value": stop},
                   {"kind": "stage", "ticket": ticket, "value": "blocked"}]
    if any(e["kind"] == "stage" and e["value"] == "blocked" for e in effects):
        effects = clear_commands(ticket) + effects
    effects.sort(key=lambda e: e["kind"] == "stage")  # the stage change goes last
    for e in effects:
        if e["kind"] == "stage":
            e["event"] = payload.get("event")
        if e["kind"] == "review":
            e["focus"], e["round"] = payload.get("focus"), payload.get("round")
            e["findings"], e["summary"], e["run_id"] = payload.get("findings"), payload.get("summary"), payload.get("run_id")
        if e["kind"] == "join":
            e["tier"] = payload.get("tier")
        if e["kind"] in ("merge", "update-branch"):
            e["head"] = facts.get("head")  # the head the gates judged
        if e["kind"] == "comment" and payload.get("run_id"):
            e["run_id"] = payload["run_id"]
    if not any(e["kind"] == "stage" for e in effects) and payload.get("event") is not None:
        effects.append({"kind": "seen", "ticket": ticket, "value": payload["event"]})
    why = matches(node, facts, {**payload, "id": payload.get("event")}, out)
    if why:
        return NO_GO, {"applied": False, "reason": why}
    return GO, {"applied": True, "written": [e["kind"] for e in effects + after if tracker.apply(e)]}


SWEEP_STAGES = ("approved", "split", "in-review", "needs-answers")
COMMANDS = ("factory:implement-", "factory:fix-", "factory:update-")


def clear_commands(ticket):
    """Effects that remove every command label. A stopped ticket keeps none, so the first command after a
    person's factory:ready is a new label and fires its node (re-adding a present label fires nothing)."""
    return [{"kind": "unlabel", "ticket": ticket, "value": "factory:implement-*"},
            {"kind": "unlabel", "ticket": ticket, "value": "factory:fix-*", "on": "pr"},
            {"kind": "unlabel", "ticket": ticket, "value": "factory:update-*", "on": "pr"}]


def refire_ci(ticket, ci):
    """Effects that write the PR's CI label again (remove, then add), so its node starts for an unchanged head."""
    return [{"kind": "unlabel", "ticket": ticket, "value": "factory:ci-green", "on": "pr"},
            {"kind": "unlabel", "ticket": ticket, "value": "factory:ci-red", "on": "pr"},
            {"kind": "label", "ticket": ticket, "value": f"factory:ci-{ci}", "on": "pr"}]


def stop_reason(facts, fix=False):
    """Why no further developer run may start for this ticket, else None: the spend guard, and for a fix
    round the fix-round cap (`limits.fix_rounds`)."""
    if facts.get("agent_runs", 0) >= facts.get("run_cap", 8):
        return (f"[factory] Stopped: {facts['agent_runs']} developer runs since the last factory:ready reached "
                f"limits.agent_runs_per_ticket. A person decides, then sets factory:ready.")
    if fix and facts.get("fix_rounds", 0) >= facts.get("fix_cap", 3):
        return (f"[factory] Stopped: {facts['fix_rounds']} fix rounds reached limits.fix_rounds and the review "
                f"still requests changes. A person decides, then sets factory:ready.")
    return None


def local_head():
    """The checkout's HEAD, else None (no git checkout)."""
    import subprocess
    r = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def size_estimate(body):
    """The current estimate recorded on an intake ticket, else None."""
    m = re.search(r"(?im)^\s*(?:size_estimate:\s*~?|Size:\s*about\s+)(\d+)\b", body or "")
    return int(m[1]) if m else None


def branch_size(base, checkout=None):
    """Changed lines in the delivery checkout against the default branch, with whole-file deletions counted once."""
    checkout = Path(checkout or Path.cwd())
    result = subprocess.run(
        ["git", "diff", "--no-renames", "--numstat", f"{base}...HEAD", "--", ".", ":(exclude)skills/vendor/**"],
        cwd=checkout, capture_output=True, text=True,
    )
    if result.returncode:
        raise Bad(f"could not measure branch diff against {base}: {result.stderr.strip()}")
    files = []
    for line in result.stdout.splitlines():
        added, deleted, path = line.split("\t", 2)
        if path.startswith("skills/vendor/"):
            continue
        if added == deleted == "-":  # binary files have no line count
            continue
        added, deleted = int(added), int(deleted)
        count = 1 if added == 0 and deleted and not (checkout / path).exists() else added + deleted
        if count:
            files.append((path, count))
    files.sort(key=lambda item: (-item[1], item[0]))
    return sum(count for _, count in files), files


def join_decision(facts):
    """The join's outcome and payload, or None while the round is open. Uses the adapter's `round_verdict`,
    which counts every required agent focus, human change requests and CI. A fix label takes the ticket's
    complexity tier (mid when unlabelled)."""
    rereviewed = has_dispute_rereview(facts)
    if (facts.get("handed_to_fix") and not rereviewed) or facts.get("pending_rereviews") \
            or not facts.get("round_complete") or facts.get("round_verdict") is None:
        return None  # an open round, or a head whose round already went to a fix (its push comes next)
    return ("changes" if facts["round_verdict"] == "changes" else "approved"), {"tier": fix_tier(facts)}


def has_dispute_rereview(facts):
    return any(r.get("agent") and r.get("rereview") and r.get("commit_id") == facts.get("head")
               for r in facts.get("reviews", []))


RISK_RANK = {"low": 0, "high": 1}


def merge_decision(facts, auto_merge):
    """(outcome, payload) of the `merge` node, else None while a gate fails: the current head approved by every required
    focus, green CI at it, no conflict. Then `waiting` (auto-merge off or risk above `max_risk`), `updated` (behind) or `merged`."""
    if auto_merge is None or facts.get("stage") != "approved" or facts.get("pr_state") != "open" or facts.get("conflict") \
            or facts.get("ci") != "green" or not facts.get("round_complete") or facts.get("round_verdict") != "approve" \
            or facts.get("round_head") is None or facts["round_head"] != facts.get("head"):
        return None
    if not auto_merge.get("enabled") or RISK_RANK.get(facts.get("risk", "high"), 1) > RISK_RANK.get(auto_merge.get("max_risk"), -1):
        return "waiting", {"reason": "[factory] waiting for a person to merge: auto-merge is off or the risk is above its limit."}
    return ("updated" if facts.get("behind") else "merged"), {}


def retro_payload(facts):
    """The `finish` retro comment of a merged ticket, from its facts."""
    agent = [r for r in facts["reviews"] if r["agent"]]
    pr = f"PR #{facts['pr']} {facts['pr_state']}" if facts["pr"] else "no PR found"
    text = (f"Retro: {pr}; fix rounds {facts['fix_rounds']}; "
            f"reviews {len(agent)} ({sum(r['verdict'] == 'approve' for r in agent)} approve, "
            f"{sum(r['verdict'] == 'changes' for r in agent)} changes)")
    return {"retro": text, "run_id": "finish:retro"}


def sweep_plan(facts, auto_merge=None):
    """(node, outcome, payload) of the first outcome whose facts hold, else None."""
    stage = facts["stage"]
    if stage == "needs-answers" and facts.get("answered"):
        return "finish", "answered", {}
    if stage == "approved" and facts.get("pr_state") == "merged":
        return "finish", "merged", retro_payload(facts)
    if stage == "approved" and facts.get("pr_state") == "open" and facts.get("conflict"):
        return "finish", "conflict", {}  # the cheap updater merges main; the reviewers then check the resolution
    if stage == "approved" and facts.get("pr_state") == "open" \
            and ((facts.get("round_head") is not None and facts.get("head") != facts.get("round_head"))
                 or facts.get("ci") == "red"):
        return "reopen", "reopened", {}
    if stage == "approved" and (m := merge_decision(facts, auto_merge)):
        return "merge", *m
    if stage == "split" and (kids := facts.get("children")):
        if bad := [c for c in kids if c["blocked"] or c["not_planned"]]:
            what = ", ".join(f"#{c['number']} ({'closed as not planned' if c['not_planned'] else 'blocked'})" for c in bad)
            return "finish", "child-blocked", {"reason": f"[factory] Blocked: child tickets need a person: {what}. "
                                                         "Fix them, then set factory:ready on this ticket."}
        if all(c["state"] == "closed" for c in kids):
            return "finish", "children-done", {}
    if stage == "in-review" and facts.get("waiting_reviewers") and (
            not facts.get("handed_to_fix") or has_dispute_rereview(facts)):
        return "join", "waiting", {}  # ask them again; nothing moves until they decide
    if stage == "in-review" and (j := join_decision(facts)):
        return "join", *j
    return None


def sweep(tracker, nodes, dry_run=False, closed_ticket=None, auto_merge=None):
    """Move every listed ticket through each step its facts allow (a merged PR goes approved to done in one
    step). A ticket that reaches done or blocked also lets its split parent finish. Returns (results, failed)."""
    results, failed, done_tickets = [], False, []
    queue, seen, merged = list(tracker.sweep_tickets(SWEEP_STAGES)), set(), False
    while queue:
        n = queue.pop(0)
        for _ in range(4):  # a ticket's longest walk: in-review, approved, done
            try:
                facts = tracker.facts(n)
                plan = sweep_plan(facts, auto_merge)
                if plan is None or (n, facts["stage"]) in seen:
                    break
                if plan[:2] == ("merge", "merged") and merged:
                    break  # one merge at a time; the next sweep reads the rest against the new main
                seen.add((n, facts["stage"]))
                node, outcome, payload = plan
                r = {"ticket": n, "stage": facts["stage"], "node": node, "outcome": outcome}
                results.append(r)
                if dry_run:
                    break
                code, out = apply(node, outcome, {"ticket": n, **payload}, tracker, nodes, auto_merge=auto_merge)
                r.update(applied=out["applied"], written=out.get("written", []))
                if not out["applied"]:
                    break
                merged = merged or (node, outcome) == ("merge", "merged")
                if node == "finish" and outcome in ("merged", "children-done"):
                    done_tickets.append(n)
                if node == "finish" and outcome in ("merged", "children-done") and hasattr(tracker, "parent") and (p := tracker.parent(n)):
                    queue.append(p)
            except Exception as e:  # one ticket failing does not stop the others
                failed = True
                results.append({"ticket": n, "error": f"{type(e).__name__}: {e}"})
                break
    wake_sources = list(dict.fromkeys(([closed_ticket] if closed_ticket is not None else []) + done_tickets))
    woken = set()
    if hasattr(tracker, "blocking"):
        for source in wake_sources:
            try:
                dependents = tracker.blocking(source)
            except Exception as e:  # a tracker lookup must not fail the whole sweep
                results.append({"ticket": source, "error": f"{type(e).__name__}: {e}"})
                continue
            for dependent in dependents:
                n = dependent["number"] if isinstance(dependent, dict) else dependent
                try:
                    if n in woken:
                        continue
                    facts = tracker.facts(n)
                    if facts["stage"] != "ready" or facts["blockers_open"]:
                        continue
                    woken.add(n)
                    results.append({"ticket": n, "stage": "ready", "node": "intake", "outcome": "unblocked"})
                    if dry_run:
                        continue
                    for effect in ({"kind": "unlabel", "ticket": n, "value": "factory:ready"},
                                   {"kind": "label", "ticket": n, "value": "factory:ready"},
                                   {"kind": "comment", "ticket": n, "value": f"[factory] unblocked by #{source}",
                                    "run_id": "finish:unblocked"}):
                        tracker.apply(effect)
                except Exception as e:
                    failed = True
                    results.append({"ticket": n, "error": f"{type(e).__name__}: {e}"})
    return results, failed


STALE_STAGES = ("ready", "in-dev", "in-review", "changes-requested", "approved")
RECOVER = "[factory] recover:"


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
    if stage == "ready" and not facts.get("blockers_open"):
        return "refire", "factory:ready", "issue", "no intake outcome"
    if stage == "in-dev" and cmd and (not facts.get("pr") or commit_at < cmd_at):
        return "refire", cmd, "issue", "no PR or commit since the command label"
    if stage == "in-review" and facts.get("pr"):
        head = facts.get("head")
        if facts.get("ci_label") == "green" and not any(r["agent"] and r["commit_id"] == head for r in facts.get("reviews", [])):
            return "refire", "factory:ci-green", "pr", "no agent review at the head"
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
    return None


def sweep_stale(tracker, nodes, hours, now=None, dry_run=False, command_minutes=30):
    """Recover tickets whose Air run died: one re-fire per ticket and stage, a second stale finding blocks it.
    Returns (results, failed)."""
    now, results, failed = now or datetime.now(timezone.utc), [], False
    for n in tracker.sweep_tickets(STALE_STAGES):
        try:
            facts = tracker.stale_facts(n)
            plan = stale_plan(facts, now, hours, command_minutes)
            if plan is None:
                continue
            kind, label, on, why = plan
            stage = facts["stage"]
            r = {"ticket": n, "stage": stage, "recover": kind, "why": why}
            results.append(r)
            if dry_run:
                continue
            run = "recover:stale"
            if kind == "finish":
                _, out = apply("finish", "merged", {"ticket": n, **retro_payload(facts)}, tracker, nodes)
                r["applied"] = out["applied"]
            elif kind == "conflict":
                _, out = apply("finish", "conflict", {"ticket": n}, tracker, nodes)
                r["applied"] = out["applied"]
            elif kind == "comment":
                r["applied"] = tracker.apply({"ticket": n, "kind": "comment", "run_id": run,
                                              "value": f"{RECOVER} stage={stage} {why}"})
            elif recovered(facts, stage):
                r["recover"] = "blocked"
                tracker.apply({"ticket": n, "kind": "comment", "run_id": run,
                               "value": f"[factory] Stopped: the node did not respond twice in {stage}. "
                                        "A person decides, then sets factory:ready."})
                for e in clear_commands(n):
                    tracker.apply(e)
                r["applied"] = tracker.apply({"ticket": n, "kind": "stage", "value": "blocked"})
            else:  # the comment last: for `ready` the re-added stage label starts the stage, and the comment must follow it
                tracker.apply({"ticket": n, "kind": "unlabel", "value": label, "on": on})
                r["applied"] = tracker.apply({"ticket": n, "kind": "label", "value": label, "on": on})
                tracker.apply({"ticket": n, "kind": "comment", "run_id": run,
                               "value": f"{RECOVER} stage={stage} re-fired {label} ({why})"})
        except Exception as e:  # one ticket failing does not stop the others
            failed = True
            results.append({"ticket": n, "error": f"{type(e).__name__}: {e}"})
    return results, failed


def main(argv):
    p = argparse.ArgumentParser(prog="gate.py")
    p.add_argument("cmd", choices=["check", "apply", "sweep"])
    p.add_argument("--node", default="")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--stale", action="store_true")
    p.add_argument("--event")
    p.add_argument("--issue", type=int)
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
    try:
        kind, _, path = (a.tracker or "github").partition(":")
        if kind == "github" and not path:
            tracker = GitHubGateTracker.from_config()
        elif kind == "memory" and path:
            try:
                tracker = MemoryTracker(dict(json.loads(Path(path).read_text())))
            except (OSError, ValueError, TypeError) as e:
                raise Bad(f"malformed --tracker file: {e}")
        else:
            raise Bad("--tracker is github or memory:FILE")
        nodes = spec_nodes(a.spec) if a.spec else nodes_mod.load(a.nodes)
        config = config_mod.load()
        if a.cmd == "sweep":
            if not hasattr(tracker, "sweep_tickets"):
                raise Bad("sweep needs --tracker github")
            closed_ticket = a.issue
            if a.event and a.issue is None:
                try:
                    raw = json.loads(Path(a.event).read_text())
                    if not isinstance(raw, dict):
                        raise ValueError("expected an event object")
                    raw = raw.get("data", raw)
                    if (not isinstance(raw, dict) or not isinstance(raw.get("action"), str)
                            or not any(isinstance(raw.get(key), dict) for key in ("issue", "pull_request"))):
                        raise ValueError("expected an event with an action and issue or pull request")
                    if raw["action"] == "closed" and isinstance(raw.get("issue"), dict):
                        closed_ticket = raw["issue"].get("number")
                except (OSError, ValueError) as e:
                    print(f"warning: ignoring invalid sweep event; running full sweep: {e}", file=sys.stderr)
            results, failed = sweep(tracker, nodes, a.dry_run, closed_ticket, config["gates"].get("auto_merge"))
            out = {"dry_run": a.dry_run, "sweep": results}
            if a.stale:
                out["stale"], stale_failed = sweep_stale(tracker, nodes, config["limits"].get("stale_hours", 2),
                                                         dry_run=a.dry_run,
                                                         command_minutes=config["limits"].get("stale_command_minutes", 30))
                failed = failed or stale_failed
            print(json.dumps(out))
            return FAILED if failed else GO
        if not a.node:
            raise Bad("--node is required")
        if a.cmd == "check":
            event = read(a.event or "", "event", raw=True)
            event.update({k: v for k, v in (("focus", a.focus), ("tier", a.tier)) if v})
            code, out = check(a.node, event, tracker, nodes, config)
            if a.node == "implement" and code == GO:
                save_check_state(event["ticket"], out["context"].get("head"))
        else:
            if a.handoff:
                try:
                    h = json.loads(Path(a.handoff).read_text())
                except (OSError, ValueError) as e:
                    raise Bad(f"malformed handoff: {e}")
                if not isinstance(h, dict):
                    raise Bad("malformed handoff: needs an object")
                ev = read(a.event, "event", raw=True) if a.event else {}
                if "ticket" not in {**h, **ev}:
                    raise Bad("apply --handoff needs `ticket` in the handoff or --event")
                payload = {**intake_fields(h), **h, **{k: v for k, v in ev.items() if v is not None and k != "id"},
                           **({"event": ev["id"]} if "id" in ev else {})}
                out_name = a.outcome or outcome_of(h)
            else:
                payload, out_name = read(a.payload or "", "payload"), a.outcome or ""
            code, out = apply(a.node, out_name, payload, tracker, nodes, a.focus, a.tier,
                              checked_head(payload["ticket"]) if a.node == "implement" else None)
            path and Path(path).write_text(json.dumps(tracker.tickets))
    except NoTicket as e:
        print(json.dumps({"go": False, "reason": str(e)}))
        return NO_GO
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

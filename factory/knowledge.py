#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""The data steps of the knowledge routines (ADR-0015); the `retro` and `distill` roles add the judgment.

`retro_entries(tracker, tickets, inbox)` writes one entry per `done` ticket not yet in the inbox.
`distill_plan(entries)` sorts the lessons of inbox entries into one plan.
Paths in the plan are relative to the team root (`FACTORY_TEAM`), never the engine checkout.
An entry line is `- [project|factory|engine|noise] <path>: <text>`; `<path>` is the file a project fact or a
factory lesson belongs in, and is absent for `engine` and `noise`.
"""
import re

KINDS = ("project", "factory", "engine", "noise")
LESSON = re.compile(r"^- \[(\w+)\] (?:([\w./-]+): )?(.+)$")


def entry_name(ticket):
    return f"retro-{ticket}.md"


def retro_entries(tracker, tickets, inbox):
    """Write `retro-<ticket>.md` into `inbox` (a dict name -> text) for each done ticket; return the new names."""
    written = []
    for t in tickets:
        f = tracker.facts(t)
        if f["stage"] != "done" or entry_name(t) in inbox:
            continue
        agent = [r for r in f.get("reviews", []) if r.get("agent")]
        inbox[entry_name(t)] = (f"# Retro #{t}: {f.get('title', '')}\n\n"
                                f"fix rounds {f['fix_rounds']}; agent runs {f['agent_runs']}; reviews {len(agent)} "
                                f"({sum(r['verdict'] == 'approve' for r in agent)} approve, "
                                f"{sum(r['verdict'] == 'changes' for r in agent)} changes)\n")
        written.append(entry_name(t))
    return written


def inbox_pr_action(open_prs, branch):
    """One inbox PR a night, from `<branch>-pr` to `branch`: push the night's entries to the source and update
    its open PR; open one only when none is."""
    source = f"{branch}-pr"
    mine = sorted(p["number"] for p in open_prs if p["head"]["ref"] == source and p["base"]["ref"] == branch)
    return {"branch": branch, "source": source, "action": "update" if mine else "create", "pr": mine[0] if mine else None}


def lessons(text):
    return [(m[1], m[2], m[3]) for m in map(LESSON.match, text.splitlines()) if m and m[1] in KINDS]


def distill_plan(entries):
    """`entries`: name -> text. Returns the PRs (one per target, with their files), the engine tickets and the
    consumed entry names. Project facts go to `docs/agents/*.md` and a link in `AGENTS.md`; factory lessons go
    to the team repo's role or skill overrides; noise is dropped but its entry is consumed."""
    prs = {"project": {"files": set()}, "team": {"files": set()}}
    tickets = []
    for text in entries.values():
        for kind, path, text_ in lessons(text):
            if kind == "project" and path and path.startswith("docs/agents/"):
                prs["project"]["files"] |= {path, "AGENTS.md"}
            elif kind == "factory" and path and path.startswith(("roles/", "skills/")):
                prs["team"]["files"].add(path)
            elif kind == "engine":
                tickets.append(text_)
    return {"prs": {k: {"files": sorted(v["files"])} for k, v in prs.items() if v["files"]},
            "tickets": tickets, "consumed": sorted(entries)}

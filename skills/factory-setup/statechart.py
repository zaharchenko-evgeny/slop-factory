#!/usr/bin/env python3
"""Check a workflow.yaml with the pinned `statechart` skill (skills.lock; TLC runs the spec).

`model(workflow)` turns the stages and node outcomes into a TLA+ spec, `check(workflow)` runs the skill on
it and returns `(broken rules, Mermaid chart)`. Python only builds the spec; TLC and the chart come from the skill.
Needs Node 22.18+ and Java 11+; TLC's jar (`TLA2TOOLS` overrides) is downloaded once and checked by sha256.

Model: one variable, the ticket's `stage`. A node outcome that sets a stage is a step from the stages its `when` and
`if` allow (an outcome that needs a PR starts only in the stages after a `create-pr` node). Nodes that only a person starts (every trigger `manual`) are not steps. Rules: OneStageLabel (every stage
an outcome sets is declared, so a ticket has exactly one stage label), MovingNode (every non-final, non-waiting
stage has a step out), NoDeadlock (TLC's own check), DoneReachable (`done` is reachable from `triage`).
"""
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "engine/template-v1/skills/statechart"
TLC_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"
TLC_SHA = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
RULES = ("OneStageLabel", "MovingNode", "NoDeadlock", "DoneReachable")


def tlc_jar():
    if os.environ.get("TLA2TOOLS"):
        return os.environ["TLA2TOOLS"]
    jar = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "factory" / f"tla2tools-{TLC_SHA[:12]}.jar"
    if not jar.is_file():
        jar.parent.mkdir(parents=True, exist_ok=True)
        data = urllib.request.urlopen(TLC_URL, timeout=120).read()
        if hashlib.sha256(data).hexdigest() != TLC_SHA:
            raise RuntimeError(f"{TLC_URL}: sha256 differs from the pin")
        jar.write_bytes(data)
    return str(jar)


def _names(value):
    return [value] if isinstance(value, str) else list(value or [])


def _stages(cond, all_stages):
    """Stages a `stage:` condition allows: names, or `!name` for every other stage."""
    names = _names(cond)
    if not names:
        return set(all_stages)
    if all(n.startswith("!") for n in names):
        return set(all_stages) - {n[1:] for n in names}
    return {n for n in names if not n.startswith("!")}


def steps(workflow):
    """[(node, outcome, guard stages, target stage)] and the declared stages. A target outside the declared stages is kept."""
    st = workflow.get("stages") or {}
    final, waiting = _names(st.get("final")), _names(st.get("waiting"))
    nodes = {n: d for n, d in workflow["nodes"].items()
             if any(t != "manual" for t in d.get("triggers") or [])}
    declared = set(final) | set(waiting) | {"triage"}
    for d in nodes.values():
        declared |= {s.lstrip("!") for s in _names((d.get("when") or {}).get("stage"))}
        for t in d.get("triggers") or []:
            declared |= set(_names(t.get("stage"))) if isinstance(t, dict) else set()
        for o in (d.get("outcomes") or {}).values():
            declared |= {s.lstrip("!") for s in _names(((o or {}).get("if") or {}).get("stage"))}
    out = []  # (node, outcome, guard, target, needs a PR)
    for n, d in nodes.items():
        for name, o in (d.get("outcomes") or {}).items():
            cond = (o or {}).get("if") or {}
            targets = [e["stage"] for e in (o or {}).get("effects") or [] if isinstance(e, dict) and "stage" in e]
            if targets:
                guard = _stages((d.get("when") or {}).get("stage"), declared) & _stages(cond.get("stage"), declared)
                out.append((n, name, guard, targets[0], "pr" in cond))
    # A PR exists from the stage a `create-pr` node sets, onward; outcomes that need a PR start only there.
    pr = {t for n, _, _, t, _ in out if nodes[n].get("output") == "create-pr"}
    while True:
        more = {t for _, _, g, t, needs in out if not needs and g & pr} - pr
        if not more:
            break
        pr |= more
    moves = [(n, o, (g & pr if needs else g) - {t}, t) for n, o, g, t, needs in out]
    return [m for m in moves if m[2]], declared, final, waiting


def _set(items):
    return "{" + ", ".join(f'"{s}"' for s in sorted(items)) + "}"


def _id(node, outcome):
    return re.sub(r"\W", "_", f"{node}__{outcome}")


def model(workflow):
    """(spec text, config text, view module text) for a parsed workflow.yaml."""
    moves, declared, final, waiting = steps(workflow)
    stages = declared | {t for *_, t in moves}
    guards = {_id(n, o): g for n, o, g, _ in moves}
    spec = ["---- MODULE Workflow ----", "EXTENDS FiniteSets, Naturals", "VARIABLE stage", "vars == <<stage>>",
            f"Declared == {_set(declared)}", f"Final == {_set(final)}", f"Waiting == {_set(waiting)}", 'Init == stage = "triage"']
    spec += [f'{_id(n, o)} == stage \\in {_set(g)} /\\ stage\' = "{t}"' for n, o, g, t in moves]
    spec += ["Done == stage \\in Final \\cup Waiting /\\ UNCHANGED vars",
             "Next == " + " \\/ ".join([*guards, "Done"]), "Spec == Init /\\ [][Next]_vars",
             "OneStageLabel == stage \\in Declared",
             "MovingNode == stage \\in Final \\cup Waiting \\/ " + (" \\/ ".join(f"stage \\in {_set(g)}" for g in guards.values()) or "FALSE"),
             "Edges == {" + ", ".join(sorted({f'<<"{s}", "{t}">>' for _, _, g, t in moves for s in g})) + "}",
             "Reach(S) == S \\cup {t \\in " + _set(stages) + ": \\E s \\in S: <<s, t>> \\in Edges}",
             "RECURSIVE Iter(_, _)", "Iter(S, n) == IF n = 0 THEN S ELSE Iter(Reach(S), n - 1)",
             # Not false in the first state: the skill cannot draw a rule that fails there. A later state shows it.
             f'DoneReachable == stage = "triage" \\/ "done" \\in Iter({{"triage"}}, {len(stages)})', "===="]
    cfg = "SPECIFICATION Spec\nINVARIANT OneStageLabel MovingNode DoneReachable\nCHECK_DEADLOCK TRUE\n"
    labels = {_id(n, o): n for n, o, *_ in moves}  # one arrow per node, whatever its outcome
    view = ("export const title = 'Ticket stage';\nexport const state = (v) => v.stage;\n"
            f"const labels = {labels!r};\nexport const step = (a) => (a === 'Done' ? null : labels[a] ?? a);\n")
    return "\n".join(spec) + "\n", cfg, view


def check(workflow):
    """Run the statechart skill on `workflow` (a parsed workflow.yaml): ([broken rule names], chart text)."""
    spec, cfg, view = model(workflow)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "Workflow.tla").write_text(spec)
        (tmp / "Workflow.cfg").write_text(cfg)
        (tmp / "view.mjs").write_text(view)
        run = subprocess.run([str(SKILL / "scripts/statechart"), "Workflow.tla", "--config", "Workflow.cfg",
                              "--view", "view.mjs", "--out", "out"], cwd=tmp, capture_output=True, text=True,
                             env={**os.environ, "TLA2TOOLS": tlc_jar()})
        report = (tmp / "out/report.md")
        text = report.read_text() if report.is_file() else ""
        # The skill cannot draw a rule that is already false in the first state; TLC reports it as an error.
        first = re.search(r"The invariant of (\w+) is equal to FALSE", run.stdout + run.stderr)
        if first and not text:
            return [first[1]], ""
        if run.returncode not in (0, 1) or not text:
            raise RuntimeError(f"statechart failed ({run.returncode}): {run.stderr or run.stdout}")
        hold = re.search(r"## Rules that hold\n(.*)", text)
        broken = [r for r in RULES if r not in (hold[1] if hold else "")]  # a rule not checked counts as broken
        chart = (tmp / "out/chart.mmd")
        return broken, chart.read_text() if chart.is_file() else ""


if __name__ == "__main__":
    broken, chart = check(yaml.safe_load(Path(sys.argv[1]).read_text()))
    print(chart)
    print("broken rules: " + (", ".join(broken) or "none"), file=sys.stderr)
    sys.exit(1 if broken else 0)

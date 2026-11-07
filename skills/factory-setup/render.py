#!/usr/bin/env python3
"""Fill the setup skill's templates: render.py ANSWERS.json OUT_DIR.
Fills `template/` (a team definition in the layout of the engine's ADR-0021: factory.yaml, workflow.yaml,
roles/, .air/cloud/startup.sh, README.md). The README gets the workflow's state chart, and a rule broken in
the workflow (statechart.py, the pinned statechart skill) stops the run before any file is written. With --extras, fill `templates/` (the AGENTS.md workflow
section, docs) instead.

ANSWERS.json maps each `{{NAME}}` placeholder to a string (or a list of lines). Jinja's `{{ name }}` (lowercase,
with spaces) in role bodies is not a placeholder: setup renders it. A placeholder without an answer is an error.
"""
import json
import re
import sys
from pathlib import Path

import yaml

SKILL = Path(__file__).resolve().parent
T = SKILL / "templates"
PROJECT_T = SKILL / "template"
PH = re.compile(r"\{\{([A-Z_]+)\}\}")


def fill(text, a):
    def one(m):
        v = a[m[1]]  # KeyError: missing answer
        return "\n".join(v) if isinstance(v, list) else v
    return PH.sub(one, text)


def _render(root, a):
    return {f.relative_to(root).as_posix(): fill(f.read_text(), a)
            for f in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)}


def render(a):
    """{relative path: text} of the extras (`templates/`)."""
    return _render(T, a)


def render_project(a):
    """{relative path: text} of a new team's definition (`template/`)."""
    engine = a["ENGINE"]
    match = re.fullmatch(r"git\+https://github\.com/([^/]+/[^/@]+)@(.+)", engine)
    if not match:
        raise ValueError("ENGINE must be a pinned GitHub URL to include factory-deploy")
    return _render(PROJECT_T, {**a, "ENGINE_SKILL_SOURCE": f"github:{match[1]}@{match[2]}"})


def with_chart(files):
    """`files` of a new team with its workflow's state chart in README.md. The pinned statechart skill checks the
    workflow first (statechart.py); a broken rule raises, so no file is written."""
    import statechart
    broken, chart = statechart.check(yaml.safe_load(files["workflow.yaml"]))
    if broken:
        raise SystemExit("workflow.yaml breaks statechart rules: " + ", ".join(broken)
                         + ". Fix the answers or the template; ask the user when the rule needs a decision.")
    return {**files, "README.md": files["README.md"].rstrip("\n") + f"\n\n## Workflow state chart\n\n```mermaid\n{chart.rstrip()}\n```\n"}


if __name__ == "__main__":
    args = sys.argv[1:]
    extras = args[:1] == ["--extras"]
    answers = json.loads(Path(args[extras]).read_text())
    for rel, text in (render if extras else lambda a: with_chart(render_project(a)))(answers).items():
        dest = Path(args[extras + 1]) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        if rel.endswith(".sh"):
            dest.chmod(0o755)

#!/usr/bin/env python3
"""Fill the setup skill templates: render.py ANSWERS.json OUT_DIR.
With --project, fill the engine's template/ instead: render.py --project ANSWERS.json OUT_DIR (ADR-0016).

ANSWERS.json maps each `{{NAME}}` placeholder to a string (or a list of lines).
Every template file is written to OUT_DIR at the same relative path;
`roles/setup.sh` is written once per role in answers["ROLES"] ({role: tools}).
A placeholder without an answer is an error.
"""
import json
import re
import sys
from pathlib import Path

T = Path(__file__).resolve().parent / "templates"
PROJECT_T = Path(__file__).resolve().parents[2] / "template"
PH = re.compile(r"\{\{([A-Z_]+)\}\}")


def fill(text, a):
    def one(m):
        v = a[m[1]]  # KeyError: missing answer
        return "\n".join(v) if isinstance(v, list) else v
    return PH.sub(one, text)


def render(a):
    """Return {relative path: text}."""
    out = {}
    for f in sorted(p for p in T.rglob("*") if p.is_file()):
        rel = f.relative_to(T).as_posix()
        if rel == "roles/setup.sh.tmpl":
            for role, tools in a["ROLES"].items():
                out[f"roles/{role}/setup.sh"] = fill(f.read_text(), {**a, "ROLE": role, "TOOLS": tools})
        else:
            out[rel] = fill(f.read_text(), a)
    return out


def render_project(a):
    """Return {relative path: text} of a new project's factory repository, from the engine's template/."""
    return {f.relative_to(PROJECT_T).as_posix(): fill(f.read_text(), a)
            for f in sorted(p for p in PROJECT_T.rglob("*") if p.is_file())}


if __name__ == "__main__":
    args = sys.argv[1:]
    project = args[:1] == ["--project"]
    answers = json.loads(Path(args[project]).read_text())
    for rel, text in (render_project if project else render)(answers).items():
        dest = Path(args[project + 1]) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)

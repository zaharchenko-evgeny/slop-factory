#!/usr/bin/env python3
"""Fill the setup skill templates: render.py ANSWERS.json OUT_DIR.

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


if __name__ == "__main__":
    answers = json.loads(Path(sys.argv[1]).read_text())
    for rel, text in render(answers).items():
        dest = Path(sys.argv[2]) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)

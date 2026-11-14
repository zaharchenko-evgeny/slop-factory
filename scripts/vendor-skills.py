#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# ///
"""Pinned skills (ADR-0008). `skills.lock` (JSON) pins each skill by source, commit or version, and tree hash.

The engine keeps no copy of a skill: `fetch_pinned(name, dest)` fetches it at its pin when a step needs it
and checks the hash.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "skills.lock"
if not LOCK.is_file():  # installed: skills.lock sits beside this file
    LOCK = Path(__file__).resolve().parent / "skills.lock"


def tree_hash(d):
    h = hashlib.sha256()
    for f in sorted(p for p in Path(d).rglob("*") if p.is_file()):
        h.update(f.relative_to(d).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def load():
    return json.loads(LOCK.read_text())["skills"]


def fetch(e, tmp):
    """The pinned source tree of one skill, in `tmp`; returns the skill directory."""
    if e["source"].startswith("npm:"):
        pkg = f"{e['source'][4:]}@{e['version']}"
        subprocess.run(["npm", "pack", pkg, "--pack-destination", tmp], check=True, capture_output=True)
        subprocess.run(["tar", "-xzf", next(Path(tmp).glob("*.tgz")), "-C", tmp], check=True)
        return Path(tmp) / "package" / e["path"]
    subprocess.run(["git", "clone", "--quiet", f"https://github.com/{e['source']}.git", tmp + "/src"], check=True)
    subprocess.run(["git", "-C", tmp + "/src", "checkout", "--quiet", e["commit"]], check=True)
    return Path(tmp) / "src" / e["path"]


def fetch_pinned(name, dest):
    """Fetch the not-vendored skill `name` at its pin into the empty directory `dest`; returns its skill directory.
    Raises RuntimeError when the fetch fails or the tree hash differs from skills.lock."""
    e = load()[name]
    try:
        src = fetch(e, str(dest))
    except (subprocess.CalledProcessError, OSError) as err:
        raise RuntimeError(f"cannot fetch skill {name} at its pin ({e['source']}@{e.get('commit') or e.get('version')}): {err}")
    if tree_hash(src) != e["sha256"]:
        raise RuntimeError(f"skill {name} fetched at its pin differs from the hash in skills.lock")
    return src


if __name__ == "__main__":
    print(__doc__)
    sys.exit(2)

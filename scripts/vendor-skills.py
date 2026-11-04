#!/usr/bin/env -S uv run --quiet
# /// script
# requires-python = ">=3.11"
# ///
"""Vendored skills (ADR-0008). `skills.lock` (JSON) pins each skill; `template/skills/<name>/` holds the copy.

  vendor-skills.py --check      fail when a copy differs from its hash or a skills.txt name has no copy
  vendor-skills.py              refresh every copy from its pinned source, then verify the hash
  vendor-skills.py --rehash     record the current hashes in the lock (after a deliberate change)
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "skills.lock"
VENDOR = ROOT / "template" / "skills"


def tree_hash(d):
    h = hashlib.sha256()
    for f in sorted(p for p in Path(d).rglob("*") if p.is_file()):
        h.update(f.relative_to(d).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def load():
    return json.loads(LOCK.read_text())["skills"]


def check(root=ROOT):
    lock = json.loads((root / "skills.lock").read_text())["skills"]
    vendor = root / "template" / "skills"
    errors = []
    for name, e in lock.items():
        d = vendor / name
        if not (d / "SKILL.md").is_file():
            errors.append(f"{name}: no vendored copy")
        elif tree_hash(d) != e["sha256"]:
            errors.append(f"{name}: differs from the hash in skills.lock")
    for f in sorted((root / "template" / "roles").glob("*/skills.txt")):
        for name in f.read_text().split():
            if name not in lock:
                errors.append(f"{f.parent.name}: skill {name} is not in skills.lock")
            elif not (vendor / name / "SKILL.md").is_file():
                errors.append(f"{f.parent.name}: skill {name} has no vendored directory")
    return errors


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


def refresh():
    for name, e in load().items():
        with tempfile.TemporaryDirectory() as tmp:
            src = fetch(e, tmp)
            shutil.rmtree(VENDOR / name, ignore_errors=True)
            shutil.copytree(src, VENDOR / name)


def rehash():
    data = json.loads(LOCK.read_text())
    for name, e in data["skills"].items():
        e["sha256"] = tree_hash(VENDOR / name)
    LOCK.write_text(json.dumps(data, indent=2) + "\n")


def main(argv):
    if "--rehash" in argv:
        return rehash() or 0
    if "--check" not in argv:
        refresh()
    errors = check()
    for m in errors:
        print(m, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

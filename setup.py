"""Ship the engine and template assets beside the code.

An installed `factory` command finds them under `<prefix>/share/factory-engine` (config.ENGINE_ROOT).
"""
import os

from setuptools import setup

ROOTS = ["engine", "skills"]  # skills/: factory-step and factory-troubleshoot, which `factory setup` installs, and factory-setup


def engine_files():
    out = []
    for root in ROOTS:
        for dirpath, _, names in os.walk(root):
            files = [os.path.join(dirpath, n) for n in names if not n.endswith(".pyc")]
            if files:
                out.append((os.path.join("share/factory-engine", dirpath), sorted(files)))
    # `factory check-workflow` runs statechart.py, which loads vendor-skills.py and skills.lock from its own directory.
    # Keep every data file here: a `[tool.setuptools.data-files]` table in pyproject.toml replaces this list.
    out.append(("share/factory", ["skills.lock", "scripts/vendor-skills.py", "skills/factory-setup/statechart.py"]))
    return out


setup(data_files=engine_files())

"""Ship the engine and template assets beside the code.

An installed `factory` command finds them under `<prefix>/share/factory-engine` (config.ENGINE_ROOT).
"""
import os

from setuptools import setup

ROOTS = ["engine", "skills"]  # skills/: factory-step, which `factory setup` installs, and factory-setup


def engine_files():
    out = []
    for root in ROOTS:
        for dirpath, _, names in os.walk(root):
            files = [os.path.join(dirpath, n) for n in names if not n.endswith(".pyc")]
            if files:
                out.append((os.path.join("share/factory-engine", dirpath), sorted(files)))
    return out


setup(data_files=engine_files())

#!/usr/bin/env python3
"""Packaging checks. Run: python3 -m unittest discover -s factory/tests -k packag"""
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class PackagingTest(unittest.TestCase):
    def test_every_plugin_package_is_listed(self):
        cfg = tomllib.loads((ROOT / "pyproject.toml").read_text())
        packages = set(cfg["tool"]["setuptools"]["packages"])
        on_disk = {f"factory_plugins.{p.parent.name}"
                   for p in (ROOT / "factory/factory_plugins").glob("*/__init__.py")}
        self.assertEqual(on_disk - packages, set())


if __name__ == "__main__":
    unittest.main()
